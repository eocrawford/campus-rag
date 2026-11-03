"""Search the policy corpus from the command line.

Run with: python -m campus_rag.retrieve "<query>" [--mode keyword|vector|hybrid] [-k N]
[--source NAME] [--url-prefix PREFIX]. See runbooks/evaluate-retrieval.md.
"""

import argparse
import sys
from collections.abc import Sequence

import psycopg

from campus_rag.config import load_settings
from campus_rag.index.embed import EmbeddingError, OpenAICompatibleEmbedder
from campus_rag.index.migrate import _first_line
from campus_rag.retrieve.connect import (
    EXIT_EMBEDDING_UNAVAILABLE,
    SetupError,
    open_connection,
)
from campus_rag.retrieve.models import SEARCH_MODES, RetrievedChunk, SearchFilters
from campus_rag.retrieve.search import MAX_K, MIN_K, PolicySearcher

EXIT_USAGE = 1
EXIT_DATABASE_ERROR = 6
PREVIEW_CHARS = 160


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m campus_rag.retrieve",
        description="Search the policy corpus and print the ranked passages.",
    )
    parser.add_argument("query", help="the question or search terms")
    parser.add_argument(
        "--mode", choices=SEARCH_MODES, default="hybrid", help="search mode (default: hybrid)"
    )
    parser.add_argument(
        "-k",
        type=int,
        default=8,
        metavar="N",
        help=f"number of results, {MIN_K}-{MAX_K} (default: 8)",
    )
    parser.add_argument(
        "--source",
        action="append",
        default=[],
        metavar="NAME",
        help="only documents from this source (repeatable)",
    )
    parser.add_argument(
        "--url-prefix",
        action="append",
        default=[],
        metavar="PREFIX",
        help="only documents whose URL starts with PREFIX (repeatable)",
    )
    return parser.parse_args(argv)


def _error(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)


def preview(chunk: RetrievedChunk, limit: int = PREVIEW_CHARS) -> str:
    """The chunk's first `limit` characters after its heading path, on one line."""
    body = chunk.content
    if body.startswith(chunk.heading_path):
        body = body[len(chunk.heading_path) :]
    text = " ".join(body.split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def format_result(chunk: RetrievedChunk) -> str:
    keyword = chunk.keyword_rank if chunk.keyword_rank is not None else "-"
    vector = chunk.vector_rank if chunk.vector_rank is not None else "-"
    return (
        f"{chunk.rank:>2}. score={chunk.score:.4f}  keyword rank={keyword}  vector rank={vector}\n"
        f"    {chunk.title}\n"
        f"    {chunk.heading_path}\n"
        f"    {chunk.url}\n"
        f"    {preview(chunk)}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if not args.query.strip():
        _error("the query must not be empty")
        return EXIT_USAGE
    if not MIN_K <= args.k <= MAX_K:
        _error(f"-k must be between {MIN_K} and {MAX_K}, got {args.k}")
        return EXIT_USAGE
    # Policy text can hold characters the console encoding cannot show.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(errors="replace")

    try:
        settings = load_settings()
    except ValueError as exc:
        _error(str(exc))
        return EXIT_USAGE
    filters = SearchFilters(sources=tuple(args.source), url_prefixes=tuple(args.url_prefix))

    try:
        conn = open_connection(settings)
    except SetupError as exc:
        _error(str(exc))
        return exc.exit_code

    try:
        with conn, OpenAICompatibleEmbedder(settings.embedding) as embedder:
            searcher = PolicySearcher(conn, embedder)
            results = searcher.search(args.query, mode=args.mode, k=args.k, filters=filters)
    except EmbeddingError as exc:
        _error(f"{exc}\nKeyword search needs no embedding server: use --mode keyword.")
        return EXIT_EMBEDDING_UNAVAILABLE
    except psycopg.Error as exc:
        _error(f"database error: {_first_line(exc)}")
        return EXIT_DATABASE_ERROR

    if not results:
        print("No results.")
        return 0
    print(f"{len(results)} result(s) for {args.query!r} ({args.mode} search)\n")
    print("\n\n".join(format_result(chunk) for chunk in results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
