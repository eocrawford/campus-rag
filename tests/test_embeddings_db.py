import re
from collections.abc import Iterator
from datetime import UTC, datetime

import psycopg
import pytest
from fakes import FakeEmbedder

from campus_rag.index.embed import EmbeddingError
from campus_rag.index.migrate import apply_migrations, discover_migrations
from campus_rag.index.store import (
    chunks_needing_embeddings,
    count_embeddings,
    save_embeddings,
    upsert_document,
    vector_literal,
)
from campus_rag.ingest.chunk import chunk_document
from campus_rag.ingest.models import ExtractedDocument, FetchedPage, Section
from campus_rag.ingest.pipeline import run_embedding

pytestmark = pytest.mark.db

MODEL = "nomic-embed-text"
DIMS = 768
FETCHED_AT = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


@pytest.fixture
def conn(db_url: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(db_url, autocommit=True) as connection:
        apply_migrations(connection, discover_migrations())
        yield connection


FILLER = "Staff submit requests to the office and the committee reviews each request. " * 12


def add_document(conn: psycopg.Connection, url: str, *sections: tuple[str, str]) -> list[int]:
    """Store a document with one section per (heading, text) and return its chunk ids.

    Each section is padded so the chunker keeps it as its own chunk instead of merging it.
    """
    doc = ExtractedDocument(
        url=url,
        title="Policy",
        sections=tuple(
            Section(("Policy", heading), f"{text}. {FILLER}") for heading, text in sections
        ),
        word_count=sum(len(text.split()) for _, text in sections),
    )
    page = FetchedPage(
        url=url, status="ok", html="<html/>", etag=None, last_modified=None, fetched_at=FETCHED_AT
    )
    upsert_document(conn, "src", doc, page, chunk_document(doc))
    return [
        row[0]
        for row in conn.execute(
            "SELECT c.id FROM chunks c JOIN documents d ON d.id = c.document_id "
            "WHERE d.url = %s ORDER BY c.ordinal",
            (url,),
        )
    ]


def unit_vector(position: int, dims: int = DIMS) -> list[float]:
    vector = [0.0] * dims
    vector[position % dims] = 1.0
    return vector


def one_chunk(conn: psycopg.Connection, text: str = "Alpha beta gamma.") -> tuple[int, str]:
    (chunk_id,) = add_document(conn, "https://example.test/one", ("1. Part", text))
    (digest,) = conn.execute(
        "SELECT content_hash FROM chunks WHERE id = %s", (chunk_id,)
    ).fetchone()
    return chunk_id, digest


# --- migration 003 ---------------------------------------------------------------------------


def test_migration_003_creates_table_columns_and_indexes(conn: psycopg.Connection) -> None:
    columns = dict(
        conn.execute(
            "SELECT column_name, is_nullable FROM information_schema.columns "
            "WHERE table_name = 'chunk_embeddings'"
        ).fetchall()
    )
    assert columns == {
        "chunk_id": "NO",
        "model": "NO",
        "dimensions": "NO",
        "content_hash": "NO",
        "embedding": "NO",
        "embedded_at": "NO",
    }
    indexes = {r[0]: r[1] for r in conn.execute("SELECT indexname, indexdef FROM pg_indexes")}
    hnsw = indexes["chunk_embeddings_nomic_embed_text_hnsw_idx"]
    assert "USING hnsw" in hnsw and "vector_cosine_ops" in hnsw
    assert "m='16'" in hnsw and "ef_construction='64'" in hnsw
    assert "gin (search_tsv)" in indexes["chunks_search_tsv_idx"]


def test_dimensions_check_rejects_mismatched_vector(conn: psycopg.Connection) -> None:
    chunk_id, digest = one_chunk(conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO chunk_embeddings (chunk_id, model, dimensions, content_hash, embedding) "
            "VALUES (%s, 'other', 4, %s, '[1,2,3]'::vector)",
            (chunk_id, digest),
        )
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO chunk_embeddings (chunk_id, model, dimensions, content_hash, embedding) "
            "VALUES (%s, 'other', 0, %s, '[1,2,3]'::vector)",
            (chunk_id, digest),
        )


def test_embeddings_cascade_when_chunks_are_deleted(conn: psycopg.Connection) -> None:
    chunk_id, digest = one_chunk(conn)
    save_embeddings(conn, MODEL, DIMS, [(chunk_id, digest, unit_vector(0))])
    assert count_embeddings(conn, MODEL) == 1
    conn.execute("DELETE FROM documents")
    assert count_embeddings(conn, MODEL) == 0


def test_several_models_coexist_for_one_chunk(conn: psycopg.Connection) -> None:
    chunk_id, digest = one_chunk(conn)
    save_embeddings(conn, MODEL, DIMS, [(chunk_id, digest, unit_vector(0))])
    save_embeddings(conn, "bge-m3", 1024, [(chunk_id, digest, unit_vector(0, 1024))])
    assert count_embeddings(conn, MODEL) == count_embeddings(conn, "bge-m3") == 1
    assert chunks_needing_embeddings(conn, MODEL) == []
    assert chunks_needing_embeddings(conn, "missing-model") != []


def test_search_tsv_is_populated_with_heading_weighted_a(conn: psycopg.Connection) -> None:
    add_document(
        conn,
        "https://example.test/tsv",
        ("1. Retention schedule", "Records are archived after seven years."),
    )
    (tsv,) = conn.execute("SELECT search_tsv::text FROM chunks").fetchone()
    heading_word = re.search(r"'retent':([0-9A-D,]+)", tsv)
    body_word = re.search(r"'archiv':([0-9A-D,]+)", tsv)
    assert heading_word and body_word, tsv
    # The heading line is stripped from the body, so a heading word is weighted A only.
    assert heading_word.group(1).endswith("A") and "B" not in heading_word.group(1)
    assert body_word.group(1).endswith("B")


def test_keyword_search_ranks_heading_matches_above_body_matches(conn: psycopg.Connection) -> None:
    add_document(conn, "https://example.test/h", ("1. Parking permits", "Rules for the campus."))
    add_document(
        conn, "https://example.test/b", ("1. Overview", "Parking permits are issued yearly.")
    )
    rows = conn.execute(
        "SELECT d.url FROM chunks c JOIN documents d ON d.id = c.document_id "
        "WHERE c.search_tsv @@ websearch_to_tsquery('english', 'parking permits') "
        "ORDER BY ts_rank_cd(c.search_tsv, websearch_to_tsquery('english', 'parking permits')) "
        "DESC"
    ).fetchall()
    assert [r[0] for r in rows] == ["https://example.test/h", "https://example.test/b"]


def test_hnsw_index_serves_the_documented_query_shape(conn: psycopg.Connection) -> None:
    ids = []
    for i in range(6):
        ids.extend(add_document(conn, f"https://example.test/{i}", ("1. Part", f"Text {i} here.")))
    rows = [(cid, f"hash{cid}", unit_vector(n)) for n, cid in enumerate(ids)]
    # Rows carry arbitrary hashes: only the index and vector math are under test here.
    save_embeddings(conn, MODEL, DIMS, rows)
    conn.execute("ANALYZE chunk_embeddings")

    query = (
        "SELECT chunk_id FROM chunk_embeddings WHERE model = 'nomic-embed-text' "
        "ORDER BY embedding::vector(768) <=> %s::vector(768) LIMIT 3"
    )
    literal = vector_literal(unit_vector(2))
    with conn.transaction():
        conn.execute("SET LOCAL enable_seqscan = off")
        conn.execute("SET LOCAL enable_sort = off")  # a six-row table otherwise sorts the pkey scan
        plan = "\n".join(r[0] for r in conn.execute("EXPLAIN " + query, (literal,)))
        assert "chunk_embeddings_nomic_embed_text_hnsw_idx" in plan, plan
        nearest = conn.execute(query, (literal,)).fetchall()
    assert nearest[0] == (ids[2],)


# --- store helpers ---------------------------------------------------------------------------


def test_missing_stale_and_current_chunks_are_detected(conn: psycopg.Connection) -> None:
    a, b, c = (add_document(conn, f"https://example.test/{n}", ("1. P", n))[0] for n in "abc")
    hashes = dict(conn.execute("SELECT id, content_hash FROM chunks").fetchall())

    assert [row[0] for row in chunks_needing_embeddings(conn, MODEL)] == [a, b, c]
    save_embeddings(
        conn,
        MODEL,
        DIMS,
        [(a, hashes[a], unit_vector(0)), (b, "stale-hash", unit_vector(1))],
    )
    needing = chunks_needing_embeddings(conn, MODEL)
    assert [row[0] for row in needing] == [b, c]
    assert needing[0][1:] == (
        conn.execute("SELECT content, content_hash FROM chunks WHERE id = %s", (b,)).fetchone()
    )
    assert count_embeddings(conn, MODEL) == 2


def test_save_embeddings_upserts(conn: psycopg.Connection) -> None:
    chunk_id, digest = one_chunk(conn)
    save_embeddings(conn, MODEL, DIMS, [(chunk_id, "old", unit_vector(0))])
    save_embeddings(conn, MODEL, DIMS, [(chunk_id, digest, unit_vector(5))])
    assert count_embeddings(conn, MODEL) == 1
    row = conn.execute(
        "SELECT content_hash, embedding::text FROM chunk_embeddings WHERE chunk_id = %s",
        (chunk_id,),
    ).fetchone()
    assert row is not None and row[0] == digest
    assert row[1] == vector_literal(unit_vector(5)).replace(".0", "")
    assert chunks_needing_embeddings(conn, MODEL) == []


def test_save_embeddings_is_atomic_and_validates_length(conn: psycopg.Connection) -> None:
    ids = add_document(conn, "https://example.test/a", ("1. P", "one"), ("2. Q", "two"))
    rows = [(ids[0], "h", unit_vector(0)), (ids[1], "h", [1.0, 2.0])]
    with pytest.raises(ValueError, match="2 dimensions, expected 768"):
        save_embeddings(conn, MODEL, DIMS, rows)
    assert count_embeddings(conn, MODEL) == 0


def test_vector_literal_formats_and_rejects_non_finite() -> None:
    assert vector_literal([0.5, -1.0, 1e-07]) == "[0.5,-1.0,1e-07]"
    with pytest.raises(ValueError, match="NaN"):
        vector_literal([float("nan")])


# --- embedding stage -------------------------------------------------------------------------


def test_run_embedding_embeds_all_then_nothing_then_only_the_changed_chunk(
    conn: psycopg.Connection,
) -> None:
    for name in ("a", "b"):
        add_document(
            conn, f"https://example.test/{name}", ("1. P", f"{name} one"), ("2. Q", f"{name} two")
        )
    lines: list[str] = []
    embedder = FakeEmbedder()

    first = run_embedding(conn, embedder, log=lines.append, batch_size=3)
    assert (first.embedded, first.already_current) == (4, 0)
    assert [len(batch) for batch in embedder.document_batches] == [3, 1]
    assert "embedded 3/4 chunks" in lines and "embedded 4/4 chunks" in lines
    assert count_embeddings(conn, MODEL) == 4
    assert chunks_needing_embeddings(conn, MODEL) == []

    calls = len(embedder.document_batches)
    second = run_embedding(conn, embedder, log=lines.append)
    assert (second.embedded, second.already_current) == (0, 4)
    assert len(embedder.document_batches) == calls  # no embedding calls at all

    add_document(conn, "https://example.test/b", ("1. P", "b one"), ("2. Q", "b two changed"))
    third = run_embedding(conn, embedder, log=lines.append)
    # A changed document replaces all its chunks (and their embeddings); document "a" is untouched.
    assert third.embedded == 2 and third.already_current == 2
    assert [t.split("\n\n")[1][:5] for t in embedder.document_batches[-1]] == ["b one", "b two"]
    assert count_embeddings(conn, MODEL) == 4

    # A single chunk whose content hash changed is the only one re-embedded.
    conn.execute(
        "UPDATE chunks SET content = content || ' edited', content_hash = 'edited-hash' "
        "WHERE id = (SELECT min(id) FROM chunks)"
    )
    fourth = run_embedding(conn, embedder, log=lines.append)
    assert (fourth.embedded, fourth.already_current) == (1, 3)
    assert embedder.document_batches[-1][0].endswith(" edited")


def test_run_embedding_stores_documents_with_the_chunk_content(conn: psycopg.Connection) -> None:
    (chunk_id,) = add_document(conn, "https://example.test/a", ("1. P", "alpha"))
    embedder = FakeEmbedder()
    run_embedding(conn, embedder, log=lambda line: None)
    (content,) = conn.execute("SELECT content FROM chunks WHERE id = %s", (chunk_id,)).fetchone()
    stored = conn.execute(
        "SELECT embedding::text FROM chunk_embeddings WHERE chunk_id = %s", (chunk_id,)
    ).fetchone()
    assert embedder.document_batches == [[content]]
    assert stored is not None and stored[0].startswith("[") and len(stored[0].split(",")) == DIMS


def test_run_embedding_failure_keeps_finished_batches_and_rerun_resumes(
    conn: psycopg.Connection,
) -> None:
    for n in range(5):
        add_document(conn, f"https://example.test/{n}", ("1. P", f"text {n}"))
    embedder = FakeEmbedder()
    embedder.fail_after_batches = 1
    with pytest.raises(EmbeddingError):
        run_embedding(conn, embedder, log=lambda line: None, batch_size=2)
    assert count_embeddings(conn, MODEL) == 2

    resumed = FakeEmbedder()
    report = run_embedding(conn, resumed, log=lambda line: None, batch_size=2)
    assert (report.embedded, report.already_current) == (3, 2)
    assert count_embeddings(conn, MODEL) == 5


def test_run_embedding_with_no_chunks_does_nothing(conn: psycopg.Connection) -> None:
    embedder = FakeEmbedder()
    report = run_embedding(conn, embedder, log=lambda line: None)
    assert (report.embedded, report.already_current) == (0, 0)
    assert embedder.document_batches == []
