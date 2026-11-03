"""Store documents and their chunks in PostgreSQL, idempotently."""

import hashlib
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
