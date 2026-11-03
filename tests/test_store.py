from collections.abc import Iterator
from datetime import UTC, datetime

import psycopg
import pytest

from campus_rag.index.migrate import apply_migrations, discover_migrations
from campus_rag.index.store import (
    document_hash,
    remove_missing_documents,
    schema_version_applied,
    upsert_document,
)
from campus_rag.ingest.chunk import chunk_document
from campus_rag.ingest.models import Chunk, ExtractedDocument, FetchedPage, Section

FETCHED_AT = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


def make_doc(url: str, text: str, title: str = "Example Policy") -> ExtractedDocument:
    sections = tuple(
        Section((title, f"{i}. Part"), paragraph) for i, paragraph in enumerate(text.split("|"))
    )
    return ExtractedDocument(url=url, title=title, sections=sections, word_count=len(text.split()))


def make_page(url: str, etag: str | None = '"v1"', when: datetime = FETCHED_AT) -> FetchedPage:
    return FetchedPage(
        url=url, status="ok", html="<html></html>", etag=etag, last_modified=None, fetched_at=when
    )


@pytest.fixture
def conn(db_url: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(db_url, autocommit=True) as connection:
        apply_migrations(connection, discover_migrations())
        yield connection


def stored_chunks(conn: psycopg.Connection, url: str) -> list[tuple]:
    return conn.execute(
        "SELECT c.id, c.ordinal, c.heading_path, c.content, c.token_count, c.content_hash "
        "FROM chunks c JOIN documents d ON d.id = c.document_id WHERE d.url = %s "
        "ORDER BY c.ordinal",
        (url,),
    ).fetchall()


def test_document_hash_depends_on_chunk_hashes_in_order() -> None:
    a = Chunk(0, "T", "T\n\na", 3, "aa")
    b = Chunk(1, "T", "T\n\nb", 3, "bb")
    assert document_hash([a, b]) == document_hash([a, b])
    assert document_hash([a, b]) != document_hash([b, a])
    assert document_hash([a]) != document_hash([a, b])


@pytest.mark.db
def test_schema_version_applied(db_url: str) -> None:
    with psycopg.connect(db_url, autocommit=True) as connection:
        assert not schema_version_applied(connection, 2)
        apply_migrations(connection, discover_migrations())
        assert schema_version_applied(connection, 1)
        assert schema_version_applied(connection, 2)
        assert not schema_version_applied(connection, 999)


@pytest.mark.db
def test_insert_stores_document_and_chunks(conn: psycopg.Connection) -> None:
    doc = make_doc("https://example.test/a", "Alpha text here.|Beta text here.")
    chunks = chunk_document(doc)
    assert upsert_document(conn, "src", doc, make_page(doc.url), chunks) == "inserted"

    row = conn.execute(
        "SELECT source, title, etag, fetched_at, content_hash, word_count FROM documents"
    ).fetchone()
    assert row == ("src", doc.title, '"v1"', FETCHED_AT, document_hash(chunks), doc.word_count)
    assert [r[1:] for r in stored_chunks(conn, doc.url)] == [
        (c.ordinal, c.heading_path, c.content, c.token_count, c.content_hash) for c in chunks
    ]


@pytest.mark.db
def test_unchanged_updates_only_fetch_metadata(conn: psycopg.Connection) -> None:
    doc = make_doc("https://example.test/a", "Alpha text here.")
    chunks = chunk_document(doc)
    upsert_document(conn, "src", doc, make_page(doc.url), chunks)
    before = stored_chunks(conn, doc.url)
    (ingested_before,) = conn.execute("SELECT ingested_at FROM documents").fetchone()

    later = datetime(2026, 11, 4, 8, 30, tzinfo=UTC)
    result = upsert_document(conn, "src", doc, make_page(doc.url, etag='"v2"', when=later), chunks)

    assert result == "unchanged"
    assert stored_chunks(conn, doc.url) == before  # same rows, same ids
    row = conn.execute("SELECT etag, fetched_at, ingested_at FROM documents").fetchone()
    assert row == ('"v2"', later, ingested_before)


@pytest.mark.db
def test_changed_content_replaces_chunks(conn: psycopg.Connection) -> None:
    url = "https://example.test/a"
    old = make_doc(url, "Alpha text here.|Beta text here.")
    upsert_document(conn, "src", old, make_page(url), chunk_document(old))
    old_ids = {r[0] for r in stored_chunks(conn, url)}

    new = make_doc(url, "Gamma text here.", title="Renamed Policy")
    new_chunks = chunk_document(new)
    assert upsert_document(conn, "src", new, make_page(url), new_chunks) == "updated"

    rows = stored_chunks(conn, url)
    assert [r[3] for r in rows] == [c.content for c in new_chunks]
    assert old_ids.isdisjoint(r[0] for r in rows)
    assert conn.execute("SELECT count(*) FROM documents").fetchone() == (1,)
    assert conn.execute("SELECT title, content_hash FROM documents").fetchone() == (
        "Renamed Policy",
        document_hash(new_chunks),
    )


@pytest.mark.db
def test_same_content_under_new_source_is_updated(conn: psycopg.Connection) -> None:
    doc = make_doc("https://example.test/a", "Alpha text here.")
    chunks = chunk_document(doc)
    upsert_document(conn, "old-src", doc, make_page(doc.url), chunks)
    assert upsert_document(conn, "new-src", doc, make_page(doc.url), chunks) == "updated"
    assert conn.execute("SELECT source FROM documents").fetchone() == ("new-src",)


@pytest.mark.db
def test_remove_missing_documents_cascades_and_is_scoped_to_source(
    conn: psycopg.Connection,
) -> None:
    for source, url in [
        ("src", "https://example.test/keep"),
        ("src", "https://example.test/drop"),
        ("other", "https://example.test/other"),
    ]:
        doc = make_doc(url, "Some text here.|More text here.")
        upsert_document(conn, source, doc, make_page(url), chunk_document(doc))

    assert remove_missing_documents(conn, "src", {"https://example.test/keep"}) == 1
    urls = {r[0] for r in conn.execute("SELECT url FROM documents")}
    assert urls == {"https://example.test/keep", "https://example.test/other"}
    orphaned = conn.execute(
        "SELECT count(*) FROM chunks c LEFT JOIN documents d ON d.id = c.document_id "
        "WHERE d.id IS NULL"
    ).fetchone()
    assert orphaned == (0,)
    assert stored_chunks(conn, "https://example.test/drop") == []

    assert remove_missing_documents(conn, "src", []) == 1
    assert {r[0] for r in conn.execute("SELECT url FROM documents")} == {
        "https://example.test/other"
    }
