"""Keyword, vector, and hybrid search against a throwaway database with hand-written chunks."""

import math
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime

import psycopg
import pytest
from fakes import FakeEmbedder

from campus_rag.config import Settings
from campus_rag.evaluate.retrieval import corpus_counts, measure_hnsw_recall
from campus_rag.index.migrate import apply_migrations, discover_migrations
from campus_rag.index.store import save_embeddings, upsert_document
from campus_rag.ingest.chunk import chunk_document
from campus_rag.ingest.models import ExtractedDocument, FetchedPage, Section
from campus_rag.ingest.pipeline import run_embedding
from campus_rag.retrieve.connect import SetupError, open_connection
from campus_rag.retrieve.keyword import keyword_search
from campus_rag.retrieve.models import RetrievedChunk, SearchFilters
from campus_rag.retrieve.search import PolicySearcher
from campus_rag.retrieve.vector import exact_vector_search, vector_search

pytestmark = pytest.mark.db

MODEL = "nomic-embed-text"
DIMS = 768
FETCHED_AT = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
FILLER = "Staff submit requests to the office and the committee reviews each request. " * 12


@pytest.fixture
def conn(db_url: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(db_url, autocommit=True) as connection:
        apply_migrations(connection, discover_migrations())
        yield connection


def add_chunk(
    conn: psycopg.Connection,
    url: str,
    heading: str,
    text: str,
    *,
    source: str = "src",
    title: str = "Policy",
) -> int:
    """Store a one-chunk document (padded so the chunker keeps it whole) and return its chunk id."""
    doc = ExtractedDocument(
        url=url,
        title=title,
        sections=(Section(("Policy", heading), f"{text}. {FILLER}"),),
        word_count=len(text.split()),
    )
    page = FetchedPage(
        url=url, status="ok", html="<html/>", etag=None, last_modified=None, fetched_at=FETCHED_AT
    )
    upsert_document(conn, source, doc, page, chunk_document(doc))
    row = conn.execute(
        "SELECT c.id FROM chunks c JOIN documents d ON d.id = c.document_id WHERE d.url = %s",
        (url,),
    ).fetchone()
    assert row is not None
    return row[0]


def ids(results: Sequence[RetrievedChunk]) -> list[int]:
    return [chunk.chunk_id for chunk in results]


def unit_vector(position: int) -> list[float]:
    vector = [0.0] * DIMS
    vector[position % DIMS] = 1.0
    return vector


def embed_all(conn: psycopg.Connection) -> FakeEmbedder:
    embedder = FakeEmbedder()
    run_embedding(conn, embedder, log=lambda line: None)
    return embedder


# --- keyword search ----------------------------------------------------------------------------


def test_keyword_search_uses_or_semantics_and_ranks_by_matched_terms(
    conn: psycopg.Connection,
) -> None:
    both = add_chunk(conn, "https://example.test/both", "1. A", "Parking permits for visitors.")
    one = add_chunk(conn, "https://example.test/one", "1. B", "Permits are issued yearly.")
    none = add_chunk(conn, "https://example.test/none", "1. C", "Library hours are posted.")
    results = keyword_search(conn, "parking permits", limit=10)
    assert ids(results) == [both, one]
    assert none not in ids(results)
    assert [r.rank for r in results] == [1, 2]
    assert [r.keyword_rank for r in results] == [1, 2]
    assert all(r.vector_rank is None for r in results)
    assert results[0].score > results[1].score > 0


def test_keyword_search_matches_a_natural_language_question_with_one_shared_term(
    conn: psycopg.Connection,
) -> None:
    refund = add_chunk(conn, "https://example.test/r", "1. Fees", "Tuition is refunded in part.")
    # An AND query would need every word of the question; OR needs one shared stem.
    results = keyword_search(conn, "Can I get my tuition refunded if I move away?", limit=5)
    assert ids(results) == [refund]


def test_keyword_search_returns_rows_with_document_fields(conn: psycopg.Connection) -> None:
    chunk_id = add_chunk(
        conn, "https://example.test/doc", "1. Parking", "Permits expire.", title="Parking Policy"
    )
    (result,) = keyword_search(conn, "permits", limit=3)
    assert result.chunk_id == chunk_id
    assert result.url == "https://example.test/doc"
    assert result.title == "Parking Policy"
    assert result.heading_path == "Policy > 1. Parking"
    assert result.content.startswith("Policy > 1. Parking\n\n")
    assert result.token_count > 0
    assert result.document_id > 0


@pytest.mark.parametrize(
    "query",
    [
        "",
        "   ",
        "the of and to",
        "?!.,;:",
        "& | ! ( ) < >",
        "'' \"\" '",
        "\\",
        "\x00",
    ],
)
def test_keyword_search_without_searchable_terms_returns_nothing(
    conn: psycopg.Connection, query: str
) -> None:
    add_chunk(conn, "https://example.test/a", "1. A", "Parking permits.")
    assert keyword_search(conn, query, limit=5) == []


@pytest.mark.parametrize(
    "query",
    [
        "parking & permits | (visitors) !",
        'parking\'s "permits" <-> visitors:*',
        "parking\\permits",
        "parking' OR 1=1 --",
        "100% parking_permits",
        "parking\x00permits",
    ],
)
def test_keyword_search_tolerates_punctuation_and_quotes(
    conn: psycopg.Connection, query: str
) -> None:
    chunk_id = add_chunk(conn, "https://example.test/a", "1. A", "Parking permits.")
    assert chunk_id in ids(keyword_search(conn, query, limit=5))


def test_keyword_search_finds_numbers_and_section_codes(conn: psycopg.Connection) -> None:
    target = add_chunk(conn, "https://example.test/t", "APS 2.4 Security", "Roles are assigned.")
    add_chunk(conn, "https://example.test/u", "APS 3.1 Records", "Roles are archived.")
    assert ids(keyword_search(conn, "APS 2.4", limit=5))[0] == target
    assert ids(keyword_search(conn, "2.4", limit=5)) == [target]


def test_keyword_search_ties_break_by_chunk_id_and_limit_applies(conn: psycopg.Connection) -> None:
    first = add_chunk(conn, "https://example.test/1", "1. Same", "Identical wording here.")
    second = add_chunk(conn, "https://example.test/2", "1. Same", "Identical wording here.")
    third = add_chunk(conn, "https://example.test/3", "1. Same", "Identical wording here.")
    assert ids(keyword_search(conn, "identical wording", limit=10)) == [first, second, third]
    assert ids(keyword_search(conn, "identical wording", limit=2)) == [first, second]
    with pytest.raises(ValueError, match="limit"):
        keyword_search(conn, "identical", limit=0)


def test_keyword_search_filters_by_source_and_url_prefix(conn: psycopg.Connection) -> None:
    a = add_chunk(conn, "https://example.test/a/x", "1. P", "Parking permits.", source="one")
    b = add_chunk(conn, "https://example.test/b/x", "1. P", "Parking permits.", source="two")
    assert set(ids(keyword_search(conn, "parking", limit=5))) == {a, b}
    assert ids(
        keyword_search(conn, "parking", limit=5, filters=SearchFilters(sources=("two",)))
    ) == [b]
    both_sources = SearchFilters(sources=("one", "two"))
    assert set(ids(keyword_search(conn, "parking", limit=5, filters=both_sources))) == {a, b}
    prefix = SearchFilters(url_prefixes=("https://example.test/a/",))
    assert ids(keyword_search(conn, "parking", limit=5, filters=prefix)) == [a]
    either = SearchFilters(url_prefixes=("https://example.test/a/", "https://example.test/b/"))
    assert set(ids(keyword_search(conn, "parking", limit=5, filters=either))) == {a, b}
    both = SearchFilters(sources=("two",), url_prefixes=("https://example.test/a/",))
    assert keyword_search(conn, "parking", limit=5, filters=both) == []
    assert set(ids(keyword_search(conn, "parking", limit=5, filters=SearchFilters()))) == {a, b}


def test_url_prefixes_match_literally_not_as_like_patterns(conn: psycopg.Connection) -> None:
    underscore = add_chunk(conn, "https://example.test/a_b/x", "1. P", "Parking permits.")
    other = add_chunk(conn, "https://example.test/axb/x", "1. P", "Parking permits.")
    percent = add_chunk(conn, "https://example.test/50%/x", "1. P", "Parking permits.")

    def found(prefix: str) -> list[int]:
        return ids(
            keyword_search(conn, "parking", limit=5, filters=SearchFilters(url_prefixes=(prefix,)))
        )

    assert found("https://example.test/a_b") == [underscore]
    assert found("https://example.test/a%") == []
    assert found("https://example.test/50%") == [percent]
    assert found("https://example.test/") == [underscore, other, percent]


def test_bm25_ranks_a_rare_term_above_repeated_common_terms(conn: psycopg.Connection) -> None:
    common = [
        add_chunk(
            conn,
            f"https://example.test/c{n}",
            f"1. Rule {n}",
            "The policy policy policy applies. The policy covers everything.",
        )
        for n in range(6)
    ]
    rare = add_chunk(conn, "https://example.test/r", "1. Other", "A zebra appears once.")
    # Every common chunk matches 'policy' several times; only one chunk has the rare 'zebra'.
    assert keyword_search(conn, "policy zebra", limit=10)[0].chunk_id == rare
    assert set(common) <= set(ids(keyword_search(conn, "policy zebra", limit=10)))


def test_bm25_prefers_shorter_chunks_and_heading_matches(conn: psycopg.Connection) -> None:
    long_text = "Permit rules apply. " + "Unrelated filler words fill this space. " * 60
    add_chunk(conn, "https://example.test/long", "1. Plain", long_text)
    short = add_chunk(conn, "https://example.test/short", "1. Plain", "Permit rules apply.")
    assert keyword_search(conn, "permit", limit=5)[0].chunk_id == short

    # The chunks that should lose are added first, so a tie would not explain the order.
    add_chunk(conn, "https://example.test/b", "1. Plain", "Zebra rules apply here.")
    in_heading = add_chunk(conn, "https://example.test/h", "1. Zebra", "Rules apply here.")
    assert keyword_search(conn, "zebra", limit=5)[0].chunk_id == in_heading


@pytest.mark.parametrize("ranking", ["bm25", "ts_rank_cd"])
def test_every_ranking_handles_odd_queries_and_filters(
    conn: psycopg.Connection, ranking: str
) -> None:
    a = add_chunk(conn, "https://example.test/a/x", "1. P", "Parking permits.", source="one")
    b = add_chunk(conn, "https://example.test/b/x", "1. P", "Parking permits.", source="two")
    for query in ("", "the of and", "& | ! ( ) < > ' \\", "\x00"):
        assert keyword_search(conn, query, limit=5, ranking=ranking) == []  # type: ignore[arg-type]
    odd = 'parking\'s "permits" <-> \\ & | ! 100%'
    results = keyword_search(conn, odd, limit=5, ranking=ranking)  # type: ignore[arg-type]
    assert ids(results) == [a, b]
    only_b = keyword_search(
        conn,
        "parking",
        limit=5,
        filters=SearchFilters(sources=("two",)),
        ranking=ranking,  # type: ignore[arg-type]
    )
    assert ids(only_b) == [b]


def test_unknown_keyword_ranking_is_rejected(conn: psycopg.Connection) -> None:
    with pytest.raises(ValueError, match="ranking"):
        keyword_search(conn, "parking", limit=5, ranking="tf-idf")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="ranking"):
        PolicySearcher(conn, FakeEmbedder(), keyword_ranking="tf-idf")  # type: ignore[arg-type]


def test_searcher_can_use_the_ts_rank_cd_keyword_ranking(conn: psycopg.Connection) -> None:
    chunk_id = add_chunk(conn, "https://example.test/a", "1. P", "Parking permits.")
    searcher = PolicySearcher(conn, FakeEmbedder(), keyword_ranking="ts_rank_cd")
    (result,) = searcher.search("parking", mode="keyword", k=3)
    assert result.chunk_id == chunk_id
    assert 0 < result.score < 1  # ts_rank_cd with normalization 32 is rank / (rank + 1)


# --- vector search -----------------------------------------------------------------------------


def seed_documents(conn: psycopg.Connection, count: int) -> list[int]:
    return [
        add_chunk(conn, f"https://example.test/{n}", f"1. Part {n}", f"Distinct text {n} here.")
        for n in range(count)
    ]


def cosine_ranking(conn: psycopg.Connection, query: Sequence[float]) -> list[tuple[int, float]]:
    """Brute force in Python: (chunk_id, cosine similarity) best first, from stored vectors."""
    rows = conn.execute("SELECT chunk_id, embedding::text FROM chunk_embeddings").fetchall()
    scored = []
    for chunk_id, text in rows:
        vector = [float(x) for x in text.strip("[]").split(",")]
        dot = sum(a * b for a, b in zip(query, vector, strict=True))
        norm = math.sqrt(sum(a * a for a in vector)) * math.sqrt(sum(a * a for a in query))
        scored.append((chunk_id, dot / norm))
    return sorted(scored, key=lambda pair: (-pair[1], pair[0]))


def test_vector_search_returns_the_nearest_chunks_with_scores(conn: psycopg.Connection) -> None:
    chunk_ids = seed_documents(conn, 12)
    embedder = embed_all(conn)
    content = conn.execute("SELECT content FROM chunks WHERE id = %s", (chunk_ids[4],)).fetchone()
    assert content is not None
    query = embedder.embed_query(content[0])  # FakeEmbedder embeds queries and chunks alike

    results = vector_search(conn, query, model=MODEL, dimensions=DIMS, limit=5)
    assert results[0].chunk_id == chunk_ids[4]
    assert results[0].score == pytest.approx(1.0, abs=1e-6)
    assert [r.rank for r in results] == [1, 2, 3, 4, 5]
    assert [r.vector_rank for r in results] == [1, 2, 3, 4, 5]
    assert all(r.keyword_rank is None for r in results)
    assert [r.score for r in results] == sorted((r.score for r in results), reverse=True)


def test_vector_and_exact_search_match_a_brute_force_ranking(conn: psycopg.Connection) -> None:
    seed_documents(conn, 25)
    embedder = embed_all(conn)
    query = embedder.embed_query("a question that matches no chunk exactly")
    expected = cosine_ranking(conn, query)[:10]

    for search in (vector_search, exact_vector_search):
        results = search(conn, query, model=MODEL, dimensions=DIMS, limit=10)
        assert ids(results) == [chunk_id for chunk_id, _ in expected]
        assert [r.score for r in results] == pytest.approx([s for _, s in expected], abs=1e-6)


class RecordingConnection:
    """Wraps a connection and records each statement, so tests can inspect what a search ran."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn
        self.statements: list[tuple[str, tuple]] = []

    def execute(self, query: str, params: tuple = ()) -> psycopg.Cursor:
        self.statements.append((query, tuple(params)))
        return self._conn.execute(query, params)  # type: ignore[arg-type]

    def transaction(self):  # noqa: ANN201
        return self._conn.transaction()


def explain(conn: psycopg.Connection, statement: tuple[str, tuple]) -> str:
    query, params = statement
    with conn.transaction():
        # Tiny test tables make the planner prefer a scan plus a sort; disable both so the
        # plan shows whether the query shape *can* use the HNSW index.
        conn.execute("SET LOCAL enable_seqscan = off")
        conn.execute("SET LOCAL enable_sort = off")
        return "\n".join(row[0] for row in conn.execute("EXPLAIN " + query, params))  # type: ignore[arg-type]


def test_vector_search_runs_a_query_the_hnsw_index_serves(conn: psycopg.Connection) -> None:
    seed_documents(conn, 20)
    embedder = embed_all(conn)
    conn.execute("ANALYZE chunk_embeddings")
    recorder = RecordingConnection(conn)
    query = embedder.embed_query("anything")

    vector_search(recorder, query, model=MODEL, dimensions=DIMS, limit=5)  # type: ignore[arg-type]
    settings = [s for s, _ in recorder.statements[:-1]]
    assert "SET LOCAL hnsw.iterative_scan = relaxed_order" in settings
    assert "SET LOCAL enable_seqscan = off" in settings
    assert recorder.statements[0] == ("SELECT set_config('hnsw.ef_search', %s, true)", ("100",))
    assert "chunk_embeddings_nomic_embed_text_hnsw_idx" in explain(conn, recorder.statements[-1])

    exact = RecordingConnection(conn)
    exact_vector_search(exact, query, model=MODEL, dimensions=DIMS, limit=5)  # type: ignore[arg-type]
    assert "chunk_embeddings_nomic_embed_text_hnsw_idx" not in explain(conn, exact.statements[-1])


def test_ef_search_is_passed_to_the_index_and_validated(conn: psycopg.Connection) -> None:
    seed_documents(conn, 3)
    embedder = embed_all(conn)
    recorder = RecordingConnection(conn)
    vector_search(
        recorder,  # type: ignore[arg-type]
        embedder.embed_query("x"),
        model=MODEL,
        dimensions=DIMS,
        limit=2,
        ef_search=37,
    )
    assert recorder.statements[0][1] == ("37",)
    for bad in (0, 1001):
        with pytest.raises(ValueError, match="ef_search"):
            vector_search(
                conn,
                embedder.embed_query("x"),
                model=MODEL,
                dimensions=DIMS,
                limit=2,
                ef_search=bad,
            )


def test_vector_search_fills_the_limit_when_the_nearest_chunks_are_filtered_out(
    conn: psycopg.Connection,
) -> None:
    keep = [
        add_chunk(conn, f"https://example.test/keep/{n}", f"1. P {n}", f"Text {n}.", source="keep")
        for n in range(30)
    ]
    skip = [
        add_chunk(conn, f"https://example.test/skip/{n}", f"1. P {n}", f"Text {n}.", source="skip")
        for n in range(2)
    ]
    # The skipped chunks are nearest to the query, so an index scan that filters its candidates
    # afterward could return nothing with ef_search = 1. The planner may also choose to filter
    # first; either way the result is complete.
    rows = [(cid, f"h{cid}", unit_vector(1)) for cid in skip]
    rows += [(cid, f"h{cid}", unit_vector(2 + n)) for n, cid in enumerate(keep)]
    save_embeddings(conn, MODEL, DIMS, rows)
    conn.execute("ANALYZE")

    only_keep = SearchFilters(sources=("keep",))
    results = vector_search(
        conn, unit_vector(1), model=MODEL, dimensions=DIMS, limit=5, filters=only_keep, ef_search=1
    )
    exact = exact_vector_search(
        conn, unit_vector(1), model=MODEL, dimensions=DIMS, limit=5, filters=only_keep
    )
    assert len(results) == 5 and set(ids(results)) <= set(keep)
    assert ids(results) == ids(exact)


def test_vector_search_with_a_selective_filter_returns_every_match(
    conn: psycopg.Connection,
) -> None:
    ids_by_group = {
        group: [
            add_chunk(conn, f"https://example.test/{group}/{n}", f"1. P {n}", f"T {n}.")
            for n in range(count)
        ]
        for group, count in (("rare", 3), ("common", 25))
    }
    rows = [
        (cid, f"h{cid}", unit_vector(n))
        for n, cid in enumerate(ids_by_group["common"] + ids_by_group["rare"])
    ]
    save_embeddings(conn, MODEL, DIMS, rows)
    conn.execute("ANALYZE")

    rare = SearchFilters(url_prefixes=("https://example.test/rare/",))
    results = vector_search(
        conn, unit_vector(0), model=MODEL, dimensions=DIMS, limit=10, filters=rare, ef_search=1
    )
    assert sorted(ids(results)) == sorted(ids_by_group["rare"])


def test_vector_search_filters_by_source(conn: psycopg.Connection) -> None:
    a = add_chunk(conn, "https://example.test/a", "1. P", "Alpha.", source="one")
    b = add_chunk(conn, "https://example.test/b", "1. P", "Beta.", source="two")
    save_embeddings(conn, MODEL, DIMS, [(a, "h", unit_vector(0)), (b, "h", unit_vector(1))])
    only_two = SearchFilters(sources=("two",))
    for search in (vector_search, exact_vector_search):
        results = search(
            conn, unit_vector(0), model=MODEL, dimensions=DIMS, limit=5, filters=only_two
        )
        assert ids(results) == [b]


def test_vector_search_only_considers_the_requested_model(conn: psycopg.Connection) -> None:
    a = add_chunk(conn, "https://example.test/a", "1. P", "Alpha.")
    b = add_chunk(conn, "https://example.test/b", "1. P", "Beta.")
    save_embeddings(conn, MODEL, DIMS, [(a, "h", unit_vector(0))])
    save_embeddings(conn, "other-model", 4, [(b, "h", [1.0, 0.0, 0.0, 0.0])])
    assert ids(vector_search(conn, unit_vector(0), model=MODEL, dimensions=DIMS, limit=5)) == [a]
    other = vector_search(conn, [1.0, 0.0, 0.0, 0.0], model="other-model", dimensions=4, limit=5)
    assert ids(other) == [b]  # no index for this model: falls back to an exact scan


def test_vector_search_validates_arguments(conn: psycopg.Connection) -> None:
    with pytest.raises(ValueError, match="dimensions"):
        vector_search(conn, [1.0, 0.0], model=MODEL, dimensions=DIMS, limit=3)
    with pytest.raises(ValueError, match="limit"):
        vector_search(conn, unit_vector(0), model=MODEL, dimensions=DIMS, limit=0)
    with pytest.raises(ValueError, match="dimensions"):
        exact_vector_search(conn, unit_vector(0), model=MODEL, dimensions=0, limit=3)
    with pytest.raises(ValueError, match="integer"):
        vector_search(conn, unit_vector(0), model=MODEL, dimensions="768); DROP", limit=3)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="NaN"):
        vector_search(conn, [float("nan")] * DIMS, model=MODEL, dimensions=DIMS, limit=3)


def test_vector_search_with_no_embeddings_returns_nothing(conn: psycopg.Connection) -> None:
    seed_documents(conn, 2)
    assert vector_search(conn, unit_vector(0), model=MODEL, dimensions=DIMS, limit=3) == []


# --- PolicySearcher ----------------------------------------------------------------------------


class AlignedEmbedder(FakeEmbedder):
    """Embeds every query as one chosen unit vector, so a test controls which chunk is nearest."""

    def __init__(self, position: int) -> None:
        super().__init__()
        self.position = position

    def embed_query(self, text: str) -> list[float]:
        self.queries.append(text)
        return unit_vector(self.position)


@pytest.fixture
def seeded(conn: psycopg.Connection) -> dict[str, int]:
    """Three chunks for the query 'zebra crossing' with disagreeing keyword and vector rankings.

    Keyword list: keyword_best, both. Vector list (query aligned with axis 5): vector_best, both.
    The keyword-only chunk has no embedding.
    """
    keyword_best = add_chunk(
        conn, "https://example.test/k", "1. Zebra crossing", "Zebra crossing rules.", source="one"
    )
    both = add_chunk(
        conn, "https://example.test/b", "1. Crossing", "Crossing guards help.", source="two"
    )
    vector_best = add_chunk(
        conn, "https://example.test/v", "1. Elsewhere", "Unrelated wording.", source="two"
    )
    save_embeddings(
        conn,
        MODEL,
        DIMS,
        [
            (vector_best, "h", unit_vector(5)),
            (both, "h", [x + y for x, y in zip(unit_vector(5), unit_vector(6), strict=True)]),
        ],
    )
    return {"keyword_best": keyword_best, "both": both, "vector_best": vector_best}


def test_searcher_keyword_mode_needs_no_embedding(
    conn: psycopg.Connection, seeded: dict[str, int]
) -> None:
    embedder = AlignedEmbedder(5)
    results = PolicySearcher(conn, embedder).search("zebra crossing", mode="keyword", k=5)
    assert ids(results) == [seeded["keyword_best"], seeded["both"]]
    assert embedder.queries == []
    assert [(r.keyword_rank, r.vector_rank) for r in results] == [(1, None), (2, None)]


def test_searcher_vector_mode(conn: psycopg.Connection, seeded: dict[str, int]) -> None:
    embedder = AlignedEmbedder(5)
    results = PolicySearcher(conn, embedder).search("zebra crossing", mode="vector", k=2)
    assert ids(results) == [seeded["vector_best"], seeded["both"]]
    assert embedder.queries == ["zebra crossing"]
    assert [(r.keyword_rank, r.vector_rank) for r in results] == [(None, 1), (None, 2)]


def test_searcher_hybrid_fuses_both_lists_and_embeds_once(
    conn: psycopg.Connection, seeded: dict[str, int]
) -> None:
    embedder = AlignedEmbedder(5)
    searcher = PolicySearcher(conn, embedder)
    results = searcher.search("zebra crossing", k=5)  # hybrid is the default mode
    by_id = {r.chunk_id: r for r in results}
    # 'both' is second in each list (2/62), which beats first place in one list (1/61); the
    # one-list chunks tie, and the lower chunk id comes first.
    assert ids(results) == [seeded["both"], seeded["keyword_best"], seeded["vector_best"]]
    assert [r.rank for r in results] == [1, 2, 3]
    both = by_id[seeded["both"]]
    assert (both.keyword_rank, both.vector_rank) == (2, 2)
    assert both.score == pytest.approx(2 / 62)
    keyword_best = by_id[seeded["keyword_best"]]
    assert (keyword_best.keyword_rank, keyword_best.vector_rank) == (1, None)
    vector_best = by_id[seeded["vector_best"]]
    assert (vector_best.keyword_rank, vector_best.vector_rank) == (None, 1)
    assert embedder.queries == ["zebra crossing"]
    assert len(searcher.search("zebra crossing", k=2)) == 2


def test_searcher_applies_filters_in_every_mode(
    conn: psycopg.Connection, seeded: dict[str, int]
) -> None:
    searcher = PolicySearcher(conn, AlignedEmbedder(5))
    only_two = SearchFilters(sources=("two",))
    expected = {
        "keyword": [seeded["both"]],
        "vector": [seeded["vector_best"], seeded["both"]],
        "hybrid": [seeded["both"], seeded["vector_best"]],
    }
    for mode, want in expected.items():
        results = searcher.search("zebra crossing", mode=mode, k=5, filters=only_two)  # type: ignore[arg-type]
        assert ids(results) == want, mode


def test_searcher_rejects_bad_arguments(conn: psycopg.Connection) -> None:
    searcher = PolicySearcher(conn, AlignedEmbedder(0))
    for query in ("", "   ", "\n\t"):
        with pytest.raises(ValueError, match="empty"):
            searcher.search(query)
    for k in (0, -1, 51):
        with pytest.raises(ValueError, match="between 1 and 50"):
            searcher.search("parking", k=k)
    with pytest.raises(ValueError, match="mode"):
        searcher.search("parking", mode="semantic")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="candidate_limit"):
        PolicySearcher(conn, AlignedEmbedder(0), candidate_limit=0)
    assert searcher.search("parking", k=1) == []  # an empty corpus is not an error
    assert searcher.search("parking", k=50) == []


# --- evaluation helpers and connection setup ---------------------------------------------------


def test_hnsw_recall_compares_the_index_with_an_exact_scan(conn: psycopg.Connection) -> None:
    seed_documents(conn, 20)
    embedder = embed_all(conn)
    summary = measure_hnsw_recall(
        conn, embedder, ["first question", "second question", "third"], k=5, ef_searches=(10, 100)
    )
    assert (summary.queries, summary.k) == (3, 5)
    assert [row.ef_search for row in summary.rows] == [10, 100]
    # With 20 vectors the index returns every neighbor, so it matches the exact scan.
    assert all(row.mean_recall == 1.0 and row.min_recall == 1.0 for row in summary.rows)
    assert summary.exact_median_ms > 0 and all(row.median_ms > 0 for row in summary.rows)


def test_corpus_counts(conn: psycopg.Connection) -> None:
    seed_documents(conn, 3)
    embed_all(conn)
    assert corpus_counts(conn, MODEL) == (3, 3, 3)
    assert corpus_counts(conn, "other-model") == (3, 3, 0)


def test_open_connection_checks_the_schema_and_reports_exit_codes(db_url: str) -> None:
    settings = Settings(database_url=db_url)
    with pytest.raises(SetupError, match="out of date") as outdated:
        open_connection(settings)  # the throwaway database has no migrations yet
    assert outdated.value.exit_code == 3

    with psycopg.connect(db_url, autocommit=True) as admin:
        apply_migrations(admin, discover_migrations())
    with open_connection(settings) as connection:
        assert connection.autocommit

    unreachable = Settings(database_url="postgresql://nobody@127.0.0.1:1/none")
    with pytest.raises(SetupError, match="cannot connect") as refused:
        open_connection(unreachable)
    assert refused.value.exit_code == 2
