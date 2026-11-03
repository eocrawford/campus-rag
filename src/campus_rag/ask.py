"""Answer a question about the indexed policies, with numbered sources.

Run with: python -m campus_rag.ask "<question>" [--mode MODE] [-k K] [--show-passages] [--json]
          python -m campus_rag.ask --chat       (follow-up questions keep the conversation)
See runbooks/ask-questions.md.
"""

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from contextlib import ExitStack
from dataclasses import asdict

import psycopg

from campus_rag.config import load_settings
from campus_rag.generate.answer import AnswerService
from campus_rag.generate.chat import ChatError, OpenAICompatibleChat
from campus_rag.generate.models import Answer, ChatTurn
from campus_rag.generate.prompt import render_passage
from campus_rag.index.embed import Embedder, EmbeddingError, OpenAICompatibleEmbedder
from campus_rag.index.migrate import _first_line
from campus_rag.retrieve.connect import (
    EXIT_CONNECTION_FAILED,
    EXIT_SCHEMA_OUT_OF_DATE,
    SetupError,
    open_connection,
)
from campus_rag.retrieve.models import SEARCH_MODES, Searcher
from campus_rag.retrieve.search import MAX_K, MIN_K, PolicySearcher

EXIT_CONFIG_ERROR = 5
EXIT_DATABASE_ERROR = 6
EXIT_MODEL_UNAVAILABLE = 7  # the embedding or chat server is unreachable or lacks a model

__all__ = ["EXIT_CONNECTION_FAILED", "EXIT_SCHEMA_OUT_OF_DATE", "main"]

_QUIT_WORDS = frozenset({"exit", "quit", ":q"})


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m campus_rag.ask",
        description="Answer a question from the indexed policies with cited sources. "
        "See runbooks/ask-questions.md.",
    )
    parser.add_argument("question", nargs="?", help="the question (optional with --chat)")
    parser.add_argument(
        "--mode", choices=SEARCH_MODES, default="hybrid", help="retrieval mode (default: hybrid)"
    )
    parser.add_argument(
        "-k",
        type=_k_value,
        default=8,
        metavar="K",
        help=f"passages to retrieve, {MIN_K}-{MAX_K} (default: 8); the prompt uses up to 6",
    )
    parser.add_argument(
        "--show-passages", action="store_true", help="also print the passages given to the model"
    )
    parser.add_argument("--json", action="store_true", help="print each answer as JSON")
    parser.add_argument(
        "--chat",
        action="store_true",
        help="keep asking follow-up questions; earlier turns are used to rewrite each follow-up",
    )
    args = parser.parse_args(argv)
    if not args.chat and not (args.question or "").strip():
        parser.error("a question is required unless --chat is given")
    return args


def _k_value(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        number = 0
    if not MIN_K <= number <= MAX_K:
        raise argparse.ArgumentTypeError(
            f"expected an integer from {MIN_K} to {MAX_K}, got {value!r}"
        )
    return number


def _error(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)


def _make_searcher(conn: psycopg.Connection, embedder: Embedder) -> Searcher:
    return PolicySearcher(conn, embedder)


def main(argv: Sequence[str] | None = None, *, read_line: Callable[[str], str] = input) -> int:
    args = _parse_args(argv)
    try:
        settings = load_settings()
    except ValueError as exc:
        _error(str(exc))
        return EXIT_CONFIG_ERROR

    # Policy text can hold characters the console encoding cannot show.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(errors="replace")

    try:
        conn = open_connection(settings)
    except SetupError as exc:
        _error(str(exc))
        return exc.exit_code

    try:
        with ExitStack() as stack:
            embedder = stack.enter_context(OpenAICompatibleEmbedder(settings.embedding))
            chat = stack.enter_context(OpenAICompatibleChat(settings.chat))
            models = [settings.chat.answer_model]
            if args.chat:
                models.append(settings.chat.rewrite_model)
            try:
                embedder.check()
                for model in models:
                    chat.check(model)
            except (EmbeddingError, ChatError) as exc:
                _error(str(exc))
                return EXIT_MODEL_UNAVAILABLE

            service = AnswerService(
                _make_searcher(conn, embedder), chat, settings.chat, k=args.k, search_mode=args.mode
            )
            if args.chat:
                return _chat_loop(service, args, read_line)
            return _ask_once(service, args.question, (), args)
    except psycopg.Error as exc:
        _error(f"database error: {_first_line(exc)}")
        return EXIT_DATABASE_ERROR
    finally:
        conn.close()


def _ask_once(
    service: AnswerService,
    question: str,
    history: Sequence[ChatTurn],
    args: argparse.Namespace,
    turns: list[ChatTurn] | None = None,
) -> int:
    try:
        answer = service.answer(question, history)
    except (EmbeddingError, ChatError) as exc:
        _error(str(exc))
        return EXIT_MODEL_UNAVAILABLE
    if turns is not None:
        turns.extend((ChatTurn("user", answer.question), ChatTurn("assistant", answer.text)))
    if args.json:
        print(json.dumps(asdict(answer), indent=2, ensure_ascii=False))
    else:
        print(format_answer(answer, show_passages=args.show_passages))
    return 0


def _chat_loop(
    service: AnswerService, args: argparse.Namespace, read_line: Callable[[str], str]
) -> int:
    print("Ask a question. Follow-up questions use the conversation. Enter a blank line to stop.")
    history: list[ChatTurn] = []
    question = (args.question or "").strip()
    while True:
        if not question:
            try:
                question = read_line("\n> ").strip()
            except EOFError:
                return 0
            if not question or question.casefold() in _QUIT_WORDS:
                return 0
        code = _ask_once(service, question, tuple(history), args, history)
        if code:
            return code
        question = ""


def format_answer(answer: Answer, *, show_passages: bool = False) -> str:
    """The answer, a numbered source list, and timings, for the terminal."""
    lines: list[str] = []
    if answer.search_query != answer.question:
        lines += [f"Search query: {answer.search_query}", ""]
    lines.append(answer.text)
    if answer.citations:
        lines += ["", "Sources"]
        for citation in sorted(answer.citations, key=lambda citation: citation.number):
            heading = citation.heading_path or citation.title
            if citation.title and not heading.startswith(citation.title):
                heading = f"{citation.title} > {heading}"
            lines.append(f"  [{citation.number}] {heading}")
            lines.append(f"      {citation.url}")
    if answer.invalid_citations:
        removed = ", ".join(str(number) for number in answer.invalid_citations)
        lines += ["", f"Removed citations to passages that were not supplied: {removed}"]
    if show_passages:
        lines += ["", f"Passages given to the model ({len(answer.passages)})"]
        for number, chunk in enumerate(answer.passages, start=1):
            lines += [
                "",
                f"--- rank {chunk.rank}, score {chunk.score:.4f}",
                render_passage(number, chunk),
            ]
    lines += ["", format_timings(answer)]
    return "\n".join(lines)


def format_timings(answer: Answer) -> str:
    parts = [
        f"{stage} {answer.timings_ms[stage] / 1000:.1f}s"
        for stage in ("rewrite", "retrieve", "generate")
        if stage in answer.timings_ms
    ]
    total = sum(answer.timings_ms.values()) / 1000
    return f"Timings: {', '.join(parts)}, total {total:.1f}s ({answer.model})"


if __name__ == "__main__":
    sys.exit(main())
