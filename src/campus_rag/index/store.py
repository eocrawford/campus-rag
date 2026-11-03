"""Store documents and their chunks in PostgreSQL, idempotently."""

import hashlib
import math
from collections.abc import Collection, Sequence
from typing import Literal

import psycopg

from campus_rag.ingest.models import Chunk, ExtractedDocument, FetchedPage

UpsertResult = Literal["inserted", "updated", "unchanged"]


def document_hash(chunks: Sequence[Chunk]) -> str:
    """sha256 over the chunk content hashes, in order. Changes when any chunk changes."""
    digest = hashlib.sha256()
    for chunk in chunks:
        digest.update(chunk.content_hash.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def upsert_document(
    conn: psycopg.Connection,
    source: str,
    doc: ExtractedDocument,
    page: FetchedPage,
    chunks: Sequence[Chunk],
) -> UpsertResult:
    """Insert or update one document and its chunks in a single transaction.

    When the stored document has the same source and content hash, only its fetch metadata
    (etag, last_modified, fetched_at) is updated and its chunks are left untouched.
    """
    content_hash = document_hash(chunks)
    with conn.transaction():
        row = conn.execute(
            "SELECT id, source, content_hash FROM documents WHERE url = %s FOR UPDATE",
            (doc.url,),
        ).fetchone()
        if row is not None and row[1] == source and row[2] == content_hash:
            conn.execute(
                "UPDATE documents SET etag = %s, last_modified = %s, fetched_at = %s WHERE id = %s",
                (page.etag, page.last_modified, page.fetched_at, row[0]),
            )
            return "unchanged"

        values = (
            source,
            doc.title,
            page.etag,
            page.last_modified,
            page.fetched_at,
            content_hash,
            doc.word_count,
        )
        if row is None:
            inserted = conn.execute(
                "INSERT INTO documents (source, title, etag, last_modified, fetched_at, "
                "content_hash, word_count, url) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                "RETURNING id",
                (*values, doc.url),
            ).fetchone()
            assert inserted is not None
            document_id = inserted[0]
            result: UpsertResult = "inserted"
        else:
            document_id = row[0]
            conn.execute(
                "UPDATE documents SET source = %s, title = %s, etag = %s, last_modified = %s, "
                "fetched_at = %s, content_hash = %s, word_count = %s, ingested_at = now() "
                "WHERE id = %s",
                (*values, document_id),
            )
            conn.execute("DELETE FROM chunks WHERE document_id = %s", (document_id,))
            result = "updated"

        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO chunks (document_id, ordinal, heading_path, content, token_count, "
                "content_hash) VALUES (%s, %s, %s, %s, %s, %s)",
                [
                    (
                        document_id,
                        chunk.ordinal,
                        chunk.heading_path,
                        chunk.content,
                        chunk.token_count,
                        chunk.content_hash,
                    )
                    for chunk in chunks
                ],
            )
        return result


def remove_missing_documents(
    conn: psycopg.Connection, source: str, keep_urls: Collection[str]
) -> int:
    """Delete documents of `source` whose URL is not in `keep_urls`; chunks cascade.

    Returns the number of documents deleted.
    """
    with conn.transaction():
        cur = conn.execute(
            "DELETE FROM documents WHERE source = %s AND NOT (url = ANY(%s::text[]))",
            (source, list(keep_urls)),
        )
        return cur.rowcount


def schema_version_applied(conn: psycopg.Connection, version: int) -> bool:
    """True when migration `version` is recorded in schema_migrations."""
    row = conn.execute("SELECT to_regclass('schema_migrations') IS NOT NULL").fetchone()
    if row is None or not row[0]:
        return False
    found = conn.execute(
        "SELECT 1 FROM schema_migrations WHERE version = %s", (version,)
    ).fetchone()
    return found is not None


def vector_literal(vector: Sequence[float]) -> str:
    """Format `vector` as a pgvector text literal such as '[0.1,0.2]'; cast it with ::vector."""
    if not all(math.isfinite(x) for x in vector):
        raise ValueError("vector contains NaN or infinity")
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


def chunks_needing_embeddings(conn: psycopg.Connection, model: str) -> list[tuple[int, str, str]]:
    """Return (chunk_id, content, content_hash) for chunks with no embedding from `model`,
    or whose stored embedding was made from different content. Ordered by chunk id."""
    return [
        (row[0], row[1], row[2])
        for row in conn.execute(
            "SELECT c.id, c.content, c.content_hash FROM chunks c "
            "LEFT JOIN chunk_embeddings e ON e.chunk_id = c.id AND e.model = %s "
            "WHERE e.chunk_id IS NULL OR e.content_hash <> c.content_hash "
            "ORDER BY c.id",
            (model,),
        )
    ]


def save_embeddings(
    conn: psycopg.Connection,
    model: str,
    dimensions: int,
    rows: Sequence[tuple[int, str, Sequence[float]]],
) -> None:
    """Upsert (chunk_id, content_hash, vector) rows for `model` in one transaction."""
    params = []
    for chunk_id, content_hash, vector in rows:
        if len(vector) != dimensions:
            raise ValueError(
                f"chunk {chunk_id}: vector has {len(vector)} dimensions, expected {dimensions}"
            )
        params.append((chunk_id, model, dimensions, content_hash, vector_literal(vector)))
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO chunk_embeddings (chunk_id, model, dimensions, content_hash, embedding) "
            "VALUES (%s, %s, %s, %s, %s::vector) "
            "ON CONFLICT (chunk_id, model) DO UPDATE SET dimensions = EXCLUDED.dimensions, "
            "content_hash = EXCLUDED.content_hash, embedding = EXCLUDED.embedding, "
            "embedded_at = now()",
            params,
        )


def count_chunks(conn: psycopg.Connection) -> int:
    row = conn.execute("SELECT count(*) FROM chunks").fetchone()
    assert row is not None
    return row[0]


def count_embeddings(conn: psycopg.Connection, model: str) -> int:
    """Number of chunks that have an embedding from `model`."""
    row = conn.execute(
        "SELECT count(*) FROM chunk_embeddings WHERE model = %s", (model,)
    ).fetchone()
    assert row is not None
    return row[0]
