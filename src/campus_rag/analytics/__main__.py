"""Print a usage report from the request log.

Run with: python -m campus_rag.analytics [--days 30]. See runbooks/run-api-and-mcp.md.
"""

import argparse
import sys
from collections.abc import Sequence

import psycopg

from campus_rag.analytics.log import SCHEMA_VERSION
from campus_rag.analytics.report import build_report, format_report
from campus_rag.config import load_settings
from campus_rag.index.migrate import _describe_target, _first_line
from campus_rag.index.store import schema_version_applied

EXIT_CONNECTION_FAILED = 2
EXIT_SCHEMA_OUT_OF_DATE = 3
EXIT_CONFIG_ERROR = 5
EXIT_DATABASE_ERROR = 6


def _days(value: str) -> int:
    try:
        days = int(value)
    except ValueError:
        days = 0
    if days < 1:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {value!r}")
    return days


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m campus_rag.analytics",
        description="Summarize API and MCP usage from the request log: volume, latency, "
        "refusals, citations, errors, and ratings.",
    )
    parser.add_argument(
        "--days", type=_days, default=30, metavar="N", help="period to report (default: 30)"
    )
    args = parser.parse_args(argv)

    try:
        settings = load_settings()
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    try:
        conn = psycopg.connect(settings.database_url, autocommit=True, connect_timeout=5)
    except psycopg.OperationalError as exc:
        print(
            f"ERROR: cannot connect to PostgreSQL at {_describe_target(settings.database_url)} "
            f"({_first_line(exc)}). Start it with: docker compose up -d --wait db",
            file=sys.stderr,
        )
        return EXIT_CONNECTION_FAILED

    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(errors="replace")  # questions can hold characters the console cannot show

    try:
        with conn:
            if not schema_version_applied(conn, SCHEMA_VERSION):
                print(
                    "ERROR: database schema is out of date. "
                    "Run: uv run python -m campus_rag.index.migrate",
                    file=sys.stderr,
                )
                return EXIT_SCHEMA_OUT_OF_DATE
            print(format_report(build_report(conn, args.days)))
    except psycopg.Error as exc:
        print(f"ERROR: database error: {_first_line(exc)}", file=sys.stderr)
        return EXIT_DATABASE_ERROR
    return 0


if __name__ == "__main__":
    sys.exit(main())
