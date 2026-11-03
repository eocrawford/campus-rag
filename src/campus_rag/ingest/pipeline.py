"""Run the ingestion stages for configured sources: select, fetch, extract, chunk, and store.

Stages are passed in as functions so the pipeline can run against fakes in tests.
"""

import statistics
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

import psycopg

from campus_rag.index.embed import Embedder
from campus_rag.index.store import (
    chunks_needing_embeddings,
    count_chunks,
    remove_missing_documents,
    save_embeddings,
    upsert_document,
)
from campus_rag.ingest.models import Chunk, ExtractedDocument, FetchedPage, SourceConfig


class Fetcher(Protocol):
    def fetch(self, url: str) -> FetchedPage: ...


@dataclass
class IngestReport:
    selected: int = 0
    fetched_ok: int = 0
    not_modified: int = 0
    disallowed: int = 0
    failed: int = 0
    skipped_short: int = 0
    extraction_failed: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    removed: int = 0
    chunks: int = 0
    token_counts: list[int] = field(default_factory=list)  # tokens per chunk, in run order
    failures: list[tuple[str, str]] = field(default_factory=list)  # (url, reason)

    @property
    def token_min(self) -> int | None:
        return min(self.token_counts) if self.token_counts else None

    @property
    def token_median(self) -> float | None:
        return statistics.median(self.token_counts) if self.token_counts else None

    @property
    def token_max(self) -> int | None:
        return max(self.token_counts) if self.token_counts else None


def run_ingest(
    *,
    configs: Sequence[SourceConfig],
    fetcher: Fetcher,
    select_urls: Callable[[SourceConfig, Fetcher], list[str]],
    extract_document: Callable[[str, str], ExtractedDocument],
    chunker: Callable[[ExtractedDocument], list[Chunk]],
    conn: psycopg.Connection | None,
    limit: int | None,
    dry_run: bool,
    log: Callable[[str], None],
    extraction_error: type[Exception] = Exception,
) -> IngestReport:
    """Ingest every selected page and return counts.

    Page selection runs for all sources before any page is fetched, so a selection error stops
    the run before anything is written. A failed page is reported and never deletes stored data.
    Deselected documents are removed only on a full run: not a dry run, no `limit`, and a
    non-empty selection that the source's `max_pages` cap did not truncate.
    """
    if not dry_run and conn is None:
        raise ValueError("a database connection is required unless dry_run is set")
    if limit is not None and limit < 0:
        raise ValueError("limit must be >= 0")

    plan: list[tuple[SourceConfig, list[str], list[str]]] = []
    remaining = limit
    for config in configs:
        urls = list(select_urls(config, fetcher))
        todo = urls if remaining is None else urls[:remaining]
        if remaining is not None:
            remaining -= len(todo)
        plan.append((config, urls, todo))

    report = IngestReport(selected=sum(len(todo) for _, _, todo in plan))
    stages = _Stages(fetcher, extract_document, chunker, conn, dry_run, log, extraction_error)
    position = 0
    for config, urls, todo in plan:
        short_urls: set[str] = set()
        for url in todo:
            position += 1
            prefix = f"[{position}/{report.selected}]"
            if _ingest_page(url, prefix, config, stages, report):
                short_urls.add(url)

        skip_reason = _removal_skip_reason(config, urls, limit, dry_run)
        if skip_reason is None:
            assert conn is not None
            removed = remove_missing_documents(conn, config.name, set(urls) - short_urls)
            report.removed += removed
            log(f"{config.name}: removed {removed} document(s) that are no longer selected")
        elif not dry_run:
            log(f"{config.name}: kept documents that are no longer selected ({skip_reason})")
    return report


@dataclass(frozen=True)
class _Stages:
    fetcher: Fetcher
    extract_document: Callable[[str, str], ExtractedDocument]
    chunker: Callable[[ExtractedDocument], list[Chunk]]
    conn: psycopg.Connection | None
    dry_run: bool
    log: Callable[[str], None]
    extraction_error: type[Exception]


def _ingest_page(
    url: str, prefix: str, config: SourceConfig, stages: _Stages, report: IngestReport
) -> bool:
    """Process one page and update `report`. Returns True when the page was too short."""
    log = stages.log
    page = stages.fetcher.fetch(url)
    if page.status == "disallowed":
        report.disallowed += 1
        log(f"{prefix} disallowed {url} ({page.error or 'robots.txt'})")
        return False
    if page.status == "failed" or page.html is None:
        report.failed += 1
        reason = page.error or f"fetch status {page.status}"
        report.failures.append((url, reason))
        log(f"{prefix} failed {url}: {reason}")
        return False
    if page.status == "ok":
        report.fetched_ok += 1
    else:
        report.not_modified += 1

    try:
        doc = stages.extract_document(url, page.html)
    except stages.extraction_error as exc:
        report.extraction_failed += 1
        reason = f"extraction failed: {exc}"
        report.failures.append((url, reason))
        log(f"{prefix} failed {url}: {reason}")
        return False
    if doc.word_count < config.min_words:
        report.skipped_short += 1
        log(f"{prefix} skipped {url} ({doc.word_count} words < {config.min_words} minimum)")
        return True
    chunks = stages.chunker(doc)
    if not chunks:
        report.extraction_failed += 1
        reason = "extraction failed: no body text to chunk"
        report.failures.append((url, reason))
        log(f"{prefix} failed {url}: {reason}")
        return False

    tokens = sum(chunk.token_count for chunk in chunks)
    report.chunks += len(chunks)
    report.token_counts.extend(chunk.token_count for chunk in chunks)
    if stages.dry_run:
        status: str = page.status
    else:
        assert stages.conn is not None
        status = upsert_document(stages.conn, config.name, doc, page, chunks)
        if status == "inserted":
            report.inserted += 1
        elif status == "updated":
            report.updated += 1
        else:
            report.unchanged += 1
    log(f"{prefix} {status} {url} ({len(chunks)} chunks, {tokens} tokens)")
    return False


def _removal_skip_reason(
    config: SourceConfig, urls: Sequence[str], limit: int | None, dry_run: bool
) -> str | None:
    if dry_run:
        return "dry run"
    if limit is not None:
        return "--limit run"
    if not urls:
        return "selection is empty"
    if config.max_pages is not None and len(urls) >= config.max_pages:
        return f"selection may be truncated by max_pages = {config.max_pages}"
    return None


@dataclass(frozen=True)
class EmbedReport:
    embedded: int
    already_current: int
    elapsed_seconds: float

    @property
    def chunks_per_second(self) -> float | None:
        if self.embedded == 0 or self.elapsed_seconds <= 0:
            return None
        return self.embedded / self.elapsed_seconds

    def describe(self) -> str:
        text = f"{self.embedded} embedded, {self.already_current} already current"
        rate = self.chunks_per_second
        if rate is not None:
            text += f" ({self.elapsed_seconds:.1f}s, {rate:.1f} chunks/s)"
        return text


def run_embedding(
    conn: psycopg.Connection,
    embedder: Embedder,
    *,
    log: Callable[[str], None],
    batch_size: int = 32,
) -> EmbedReport:
    """Embed every stored chunk that lacks a current embedding from `embedder.model`.

    Each batch is saved before the next is requested, so a failure keeps the finished batches and
    a rerun resumes. Chunks that already have an embedding for their current content are not sent.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    started = time.monotonic()
    pending = chunks_needing_embeddings(conn, embedder.model)
    already_current = count_chunks(conn) - len(pending)
    if not pending:
        log(f"embeddings: all {already_current} chunks already have {embedder.model} embeddings")
        return EmbedReport(0, already_current, time.monotonic() - started)

    log(f"embeddings: {len(pending)} chunks to embed with {embedder.model}")
    done = 0
    for start in range(0, len(pending), batch_size):
        batch = pending[start : start + batch_size]
        vectors = embedder.embed_documents([content for _, content, _ in batch])
        if len(vectors) != len(batch):
            raise ValueError(f"embedder returned {len(vectors)} vectors for {len(batch)} chunks")
        save_embeddings(
            conn,
            embedder.model,
            embedder.dimensions,
            [
                (chunk_id, digest, vec)
                for (chunk_id, _, digest), vec in zip(batch, vectors, strict=True)
            ],
        )
        done += len(batch)
        log(f"embedded {done}/{len(pending)} chunks")
    return EmbedReport(done, already_current, time.monotonic() - started)


def format_summary(
    report: IngestReport,
    *,
    requests_made: int,
    elapsed_seconds: float,
    embeddings: str | None = None,
) -> str:
    """Multi-line run summary for the console.

    `embeddings` is the text of the Embeddings line; the line is omitted when it is None.
    """

    def stat(value: float | None) -> str:
        return "-" if value is None else f"{value:g}"

    embeddings_line = [] if embeddings is None else [f"  Embeddings: {embeddings}"]
    return "\n".join(
        [
            "Summary",
            f"  Pages:     {report.selected} selected, {report.fetched_ok} fetched, "
            f"{report.not_modified} not modified, {report.disallowed} disallowed, "
            f"{report.failed} failed, {report.skipped_short} too short, "
            f"{report.extraction_failed} extraction failed",
            f"  Documents: {report.inserted} inserted, {report.updated} updated, "
            f"{report.unchanged} unchanged, {report.removed} removed",
            f"  Chunks:    {report.chunks} (tokens per chunk: min {stat(report.token_min)}, "
            f"median {stat(report.token_median)}, max {stat(report.token_max)})",
            *embeddings_line,
            f"  Requests:  {requests_made} HTTP requests",
            f"  Elapsed:   {elapsed_seconds:.1f}s",
        ]
    )
