"""Evaluation commands.

Run with: python -m campus_rag.evaluate <command> [options]

To add a command, write a module with `add_arguments(parser)` and `run(args) -> int`, and add one
`Command` to `COMMANDS`.
"""

import argparse
import importlib
import sys
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Command:
    name: str
    help: str
    module: str  # dotted path of a module with add_arguments(parser) and run(args)


COMMANDS: tuple[Command, ...] = (
    Command(
        "retrieval",
        "recall@k, MRR, latency, and HNSW recall for keyword, vector, and hybrid search",
        "campus_rag.evaluate.retrieval",
    ),
    Command(
        "answers",
        "answer every golden question; refusal, citation, retrieval, faithfulness, and relevance",
        "campus_rag.evaluate.answers",
    ),
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m campus_rag.evaluate",
        description="Evaluate retrieval and answer quality against the golden set.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="command")
    for command in COMMANDS:
        subparser = subparsers.add_parser(command.name, help=command.help, description=command.help)
        importlib.import_module(command.module).add_arguments(subparser)
        subparser.set_defaults(module=command.module)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return importlib.import_module(args.module).run(args)


if __name__ == "__main__":
    sys.exit(main())
