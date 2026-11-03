"""Ingest the configured corpus: select, fetch, extract, chunk, store, and embed.

Run with: python -m campus_rag.ingest [--source NAME] [--limit N] [--dry-run] [--no-embed]
See runbooks/ingest-corpus.md.
"""

import argparse
import sys
import time
from collections.abc import Sequence
from contextlib import ExitStack
from pathlib import Path

import psycopg

from campus_rag.config import find_repo_root, load_settings
from campus_rag.index.embed import EmbeddingError, OpenAICompatibleEmbedder
from campus_rag.index.migrate import _describe_target, _first_line
from campus_rag.index.store import schema_version_applied
from campus_rag.ingest.chunk import chunk_document
from campus_rag.ingest.extract import ExtractionError, extract_document
from campus_rag.ingest.fetch import PoliteFetcher
from campus_rag.ingest.models import SourceConfig
from campus_rag.ingest.pipeline import (
    EmbedReport,
    IngestReport,
    format_summary,
    run_embedding,
    run_ingest,
)
from campus_rag.ingest.sources import SourceConfigError, load_source_configs, select_urls

REQUIRED_SCHEMA_VERSION = 3

EXIT_CONNECTION_FAILED = 2
EXIT_SCHEMA_OUT_OF_DATE = 3
EXIT_PAGES_FAILED = 4
EXIT_CONFIG_ERROR = 5
EXIT_DATABASE_ERROR = 6  # also returned when embedding fails after chunks were stored
EXIT_EMBEDDING_UNAVAILABLE = 7


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m campus_rag.ingest",
        description="Fetch, extract, chunk, and store the configured corpus "
        "(config/sources.toml). See runbooks/ingest-corpus.md.",
    )
    parser.add_argument("--source", metavar="NAME", help="ingest only this source (default: all)")
    parser.add_argument(
        "--limit",
        type=_non_negative_int,
        metavar="N",
        help="process at most N pages; never removes stored documents",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch, extract, and chunk, then report without writing to the database",
    )
    parser.add_argument(
        "--no-embed",
        action="store_true",
        help="store chunks without embedding them (embedding needs the model server)",
    )
    return parser.parse_args(argv)


def _non_negative_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        number = -1
    if number < 0:
        raise argparse.ArgumentTypeError(f"expected a non-negative integer, got {value!r}")
    return number


def _error(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    started = time.monotonic()

    try:
        configs = load_source_configs()
    except SourceConfigError as exc:
        _error(str(exc))
        return EXIT_CONFIG_ERROR
    if args.source is not None:
        names = ", ".join(config.name for config in configs)
        configs = [config for config in configs if config.name == args.source]
        if not configs:
            _error(f"unknown source {args.source!r}; configured sources: {names}")
            return EXIT_CONFIG_ERROR

    embed = not args.dry_run and not args.no_embed
    try:
        settings = load_settings()
    except ValueError as exc:
        _error(str(exc))
        return EXIT_CONFIG_ERROR

    conn: psycopg.Connection | None = None
    if not args.dry_run:
        target = _describe_target(settings.database_url)
        try:
            conn = psycopg.connect(settings.database_url, autocommit=True, connect_timeout=5)
        except psycopg.OperationalError as exc:
            _error(
                f"cannot connect to PostgreSQL at {target} ({_first_line(exc)}). "
                "Start it with: docker compose up -d --wait db"
            )
            return EXIT_CONNECTION_FAILED

    embed_report: EmbedReport | None = None
    embed_error: str | None = None
    try:
        if conn is not None and not schema_version_applied(conn, REQUIRED_SCHEMA_VERSION):
            _error("database schema is out of date. Run: uv run python -m campus_rag.index.migrate")
            return EXIT_SCHEMA_OUT_OF_DATE

        with ExitStack() as stack:
            embedder = None
            if embed:
                embedder = stack.enter_context(OpenAICompatibleEmbedder(settings.embedding))
                try:
                    embedder.check()
                except EmbeddingError as exc:
                    _error(
                        f"{exc}\nNothing was fetched or stored. To store chunks without "
                        "embeddings, rerun with --no-embed."
                    )
                    return EXIT_EMBEDDING_UNAVAILABLE
            try:
                report, requests_made, robots_errors = _ingest(conn, configs, args)
            except SourceConfigError as exc:
                _error(f"cannot read the source sitemap; nothing was stored or removed. {exc}")
                return EXIT_CONFIG_ERROR
            if embedder is not None:
                assert conn is not None
                try:
                    embed_report = run_embedding(
                        conn, embedder, log=lambda line: print(line, flush=True)
                    )
                except EmbeddingError as exc:
                    embed_error = str(exc)
    except psycopg.Error as exc:
        _error(f"database error: {_first_line(exc)}")
        return EXIT_DATABASE_ERROR
    finally:
        if conn is not None:
            conn.close()

    print()
    if args.dry_run:
        print("Dry run: nothing was written to the database.")
    if args.dry_run:
        embeddings = None
    elif args.no_embed:
        embeddings = "skipped (--no-embed)"
    elif embed_report is None:
        embeddings = "failed (see the error below)"
    else:
        embeddings = embed_report.describe()
    print(
        format_summary(
            report,
            requests_made=requests_made,
            elapsed_seconds=time.monotonic() - started,
            embeddings=embeddings,
        )
    )
    for origin, reason in sorted(robots_errors.items()):
        print(f"WARNING: {origin}: {reason}; all its pages were treated as disallowed.")
    if report.failures:
        stored = "" if args.dry_run else "; the other pages were processed normally"
        print(f"\n{len(report.failures)} page(s) failed{stored}:", file=sys.stderr)
        for url, reason in report.failures:
            print(f"  {url}: {reason}", file=sys.stderr)
    if embed_error is not None:
        _error(
            f"embedding failed: {embed_error}\nStored chunks and finished batches are kept; "
            "rerun to embed the rest."
        )
        return EXIT_DATABASE_ERROR
    return EXIT_PAGES_FAILED if report.failures else 0


def _ingest(
    conn: psycopg.Connection | None, configs: Sequence[SourceConfig], args: argparse.Namespace
) -> tuple[IngestReport, int, dict[str, str]]:
    cache_dir = (find_repo_root() or Path.cwd()) / "data" / "raw"
    with PoliteFetcher(cache_dir=cache_dir) as fetcher:
        report = run_ingest(
            configs=configs,
            fetcher=fetcher,
            select_urls=select_urls,
            extract_document=extract_document,
            chunker=chunk_document,
            conn=conn,
            limit=args.limit,
            dry_run=args.dry_run,
            log=lambda line: print(line, flush=True),
            extraction_error=ExtractionError,
        )
        return report, fetcher.requests_made, dict(getattr(fetcher, "robots_errors", {}))


if __name__ == "__main__":
    sys.exit(main())
