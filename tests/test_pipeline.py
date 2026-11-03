from collections.abc import Iterator
from datetime import UTC, datetime

import psycopg
import pytest

from campus_rag.index.migrate import apply_migrations, discover_migrations
from campus_rag.ingest import pipeline
from campus_rag.ingest.chunk import chunk_document
from campus_rag.ingest.models import ExtractedDocument, FetchedPage, Section, SourceConfig
from campus_rag.ingest.pipeline import EmbedReport, IngestReport, format_summary, run_ingest

BASE = "https://example.test/"
LONG_TEXT = "Staff submit requests to the office. The committee reviews each request. " * 5
SHORT_TEXT = "Index of policies."


class FakeExtractionError(Exception):
    pass


class FakeFetcher:
    """Serves pages from a dict of url -> html; special values select other statuses."""

    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.requests_made = 0
        self.fetched: list[str] = []

    def fetch(self, url: str) -> FetchedPage:
        self.requests_made += 1
        self.fetched.append(url)
        html = self.pages.get(url, "FAILED")
        status = {"FAILED": "failed", "DISALLOWED": "disallowed"}.get(html, "ok")
        if html.startswith("CACHED:"):
            status, html = "not_modified", html.removeprefix("CACHED:")
        return FetchedPage(
            url=url,
            status=status,  # type: ignore[arg-type]
            html=html if status in ("ok", "not_modified") else None,
            etag=None,
            last_modified=None,
            fetched_at=datetime(2026, 11, 3, tzinfo=UTC),
            error="HTTP 503 after 4 attempts" if status == "failed" else None,
        )


def fake_extract(url: str, html: str) -> ExtractedDocument:
    if html == "BROKEN":
        raise FakeExtractionError("no <main> element")
    title = url.removeprefix(BASE)
    return ExtractedDocument(
        url=url,
        title=title,
        sections=(Section((title, "1. Body"), html),) if html.strip() else (),
        word_count=len(html.split()) if html.strip() else 10_000,
    )


def config(name: str = "src", max_pages: int | None = None) -> SourceConfig:
    return SourceConfig(
        name=name,
        sitemap_url=f"{BASE}sitemap.xml",
        include_prefixes=(BASE,),
        exclude_patterns=(),
        min_words=20,
        max_pages=max_pages,
    )


def selector(urls_by_source: dict[str, list[str]]):
    return lambda cfg, fetcher: list(urls_by_source[cfg.name])


def run(pages: dict[str, str], *, conn=None, dry_run=True, limit=None, configs=None, lines=None):
    configs = configs or [config()]
    urls = {cfg.name: sorted(pages) for cfg in configs}
    return run_ingest(
        configs=configs,
        fetcher=FakeFetcher(pages),
        select_urls=selector(urls),
        extract_document=fake_extract,
        chunker=chunk_document,
        conn=conn,
        limit=limit,
        dry_run=dry_run,
        log=(lines.append if lines is not None else lambda line: None),
        extraction_error=FakeExtractionError,
    )


class RecordingStore:
    """Stands in for the store functions so unit tests need no database."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.upserts: list[str] = []
        self.removals: list[tuple[str, set[str]]] = []
        monkeypatch.setattr(pipeline, "upsert_document", self.upsert)
        monkeypatch.setattr(pipeline, "remove_missing_documents", self.remove)

    def upsert(self, conn, source, doc, page, chunks) -> str:
        self.upserts.append(doc.url)
        return "inserted"

    def remove(self, conn, source, keep_urls) -> int:
        self.removals.append((source, set(keep_urls)))
        return 0


def test_dry_run_needs_no_connection_and_reports_chunks() -> None:
    lines: list[str] = []
    report = run({BASE + "a": LONG_TEXT, BASE + "b": "CACHED:" + LONG_TEXT}, lines=lines)
    assert (report.selected, report.fetched_ok, report.not_modified) == (2, 1, 1)
    assert (report.inserted, report.updated, report.unchanged, report.removed) == (0, 0, 0, 0)
    assert report.chunks == 2
    assert report.token_min == report.token_max == report.token_median
    assert report.failures == []
    assert lines[0].startswith(f"[1/2] ok {BASE}a (1 chunks, ")
    assert lines[1].startswith(f"[2/2] not_modified {BASE}b (1 chunks, ")


def test_store_run_requires_connection() -> None:
    with pytest.raises(ValueError, match="connection"):
        run({BASE + "a": LONG_TEXT}, dry_run=False)


def test_failures_are_counted_and_listed() -> None:
    lines: list[str] = []
    report = run(
        {
            BASE + "a": LONG_TEXT,
            BASE + "b": "FAILED",
            BASE + "c": "DISALLOWED",
            BASE + "d": "BROKEN",
            BASE + "e": "",
        },
        lines=lines,
    )
    assert report.selected == 5
    assert (report.fetched_ok, report.failed, report.disallowed) == (3, 1, 1)
    assert report.extraction_failed == 2
    assert report.failures == [
        (BASE + "b", "HTTP 503 after 4 attempts"),
        (BASE + "d", "extraction failed: no <main> element"),
        (BASE + "e", "extraction failed: no body text to chunk"),
    ]
    assert f"[3/5] disallowed {BASE}c" in lines[2]
    assert lines[1] == f"[2/5] failed {BASE}b: HTTP 503 after 4 attempts"


def test_short_pages_are_skipped() -> None:
    lines: list[str] = []
    report = run({BASE + "a": LONG_TEXT, BASE + "index": SHORT_TEXT}, lines=lines)
    assert report.skipped_short == 1
    assert report.chunks == 1
    assert lines[1] == f"[2/2] skipped {BASE}index (3 words < 20 minimum)"


def test_full_run_removes_deselected_but_keeps_failed_pages(monkeypatch) -> None:
    store = RecordingStore(monkeypatch)
    pages = {
        BASE + "a": LONG_TEXT,
        BASE + "failed": "FAILED",
        BASE + "broken": "BROKEN",
        BASE + "disallowed": "DISALLOWED",
        BASE + "index": SHORT_TEXT,
    }
    report = run(pages, conn=object(), dry_run=False)
    assert store.upserts == [BASE + "a"]
    assert report.inserted == 1
    keep = {BASE + "a", BASE + "failed", BASE + "broken", BASE + "disallowed"}
    assert store.removals == [("src", keep)]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"limit": 1}, "--limit run"),
        ({"configs": [config(max_pages=2)]}, "max_pages = 2"),
    ],
)
def test_partial_runs_never_remove(monkeypatch, kwargs, message) -> None:
    store = RecordingStore(monkeypatch)
    lines: list[str] = []
    run(
        {BASE + "a": LONG_TEXT, BASE + "b": LONG_TEXT},
        conn=object(),
        dry_run=False,
        lines=lines,
        **kwargs,
    )
    assert store.removals == []
    assert message in lines[-1]


def test_empty_selection_never_removes(monkeypatch) -> None:
    store = RecordingStore(monkeypatch)
    report = run({}, conn=object(), dry_run=False)
    assert report.selected == 0
    assert store.removals == []


def test_limit_applies_across_sources() -> None:
    fetcher = FakeFetcher({BASE + p: LONG_TEXT for p in ["a", "b", "c", "d"]})
    report = run_ingest(
        configs=[config("one"), config("two")],
        fetcher=fetcher,
        select_urls=selector({"one": [BASE + "a", BASE + "b"], "two": [BASE + "c", BASE + "d"]}),
        extract_document=fake_extract,
        chunker=chunk_document,
        conn=None,
        limit=3,
        dry_run=True,
        log=lambda line: None,
    )
    assert report.selected == 3
    assert fetcher.fetched == [BASE + "a", BASE + "b", BASE + "c"]


def test_selection_error_stops_before_any_page_is_fetched() -> None:
    fetcher = FakeFetcher({BASE + "a": LONG_TEXT})

    def select(cfg, _fetcher):
        if cfg.name == "two":
            raise RuntimeError("cannot fetch sitemap")
        return [BASE + "a"]

    with pytest.raises(RuntimeError):
        run_ingest(
            configs=[config("one"), config("two")],
            fetcher=fetcher,
            select_urls=select,
            extract_document=fake_extract,
            chunker=chunk_document,
            conn=None,
            limit=None,
            dry_run=True,
            log=lambda line: None,
        )
    assert fetcher.fetched == []


def test_format_summary() -> None:
    report = IngestReport(
        selected=3, fetched_ok=2, inserted=2, chunks=3, token_counts=[100, 300, 200]
    )
    text = format_summary(report, requests_made=5, elapsed_seconds=12.34)
    assert "3 selected, 2 fetched" in text
    assert "2 inserted" in text
    assert "min 100, median 200, max 300" in text
    assert "5 HTTP requests" in text
    assert "12.3s" in text
    assert "min -, median -, max -" in format_summary(
        IngestReport(), requests_made=0, elapsed_seconds=0
    )


def test_format_summary_embeddings_line() -> None:
    plain = format_summary(IngestReport(), requests_made=0, elapsed_seconds=0)
    assert "Embeddings" not in plain
    text = format_summary(
        IngestReport(), requests_made=0, elapsed_seconds=0, embeddings="skipped (--no-embed)"
    )
    assert "  Embeddings: skipped (--no-embed)" in text


def test_embed_report_describe() -> None:
    assert EmbedReport(0, 10, 0.1).describe() == "0 embedded, 10 already current"
    report = EmbedReport(64, 0, 16.0)
    assert report.chunks_per_second == 4.0
    assert report.describe() == "64 embedded, 0 already current (16.0s, 4.0 chunks/s)"


@pytest.fixture
def conn(db_url: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(db_url, autocommit=True) as connection:
        apply_migrations(connection, discover_migrations())
        yield connection


@pytest.mark.db
def test_end_to_end_against_database(conn: psycopg.Connection) -> None:
    def ingest(pages: dict[str, str]) -> IngestReport:
        return run(pages, conn=conn, dry_run=False)

    pages = {
        BASE + "a": LONG_TEXT,
        BASE + "b": LONG_TEXT + " Extra sentence.",
        BASE + "i": SHORT_TEXT,
    }
    first = ingest(pages)
    assert (first.inserted, first.updated, first.unchanged, first.removed) == (2, 0, 0, 0)
    assert first.skipped_short == 1
    assert conn.execute("SELECT count(*) FROM chunks").fetchone() == (2,)

    second = ingest(pages)
    assert (second.inserted, second.updated, second.unchanged, second.removed) == (0, 0, 2, 0)

    pages[BASE + "b"] = LONG_TEXT + " A changed sentence."
    third = ingest(pages)
    assert (third.inserted, third.updated, third.unchanged) == (0, 1, 1)
    (content,) = conn.execute(
        "SELECT c.content FROM chunks c JOIN documents d ON d.id = c.document_id WHERE d.url = %s",
        (BASE + "b",),
    ).fetchone()
    assert content.endswith("A changed sentence.")

    del pages[BASE + "b"]
    fourth = ingest(pages)
    assert (fourth.unchanged, fourth.removed) == (1, 1)
    assert {r[0] for r in conn.execute("SELECT url FROM documents")} == {BASE + "a"}
    assert conn.execute("SELECT count(*) FROM chunks").fetchone() == (1,)
