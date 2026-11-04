"""Talk to the campus-rag MCP server the way an AI agent does, and print what happens.

The script starts `python -m campus_rag.mcp_server` over stdio with the MCP SDK, lists the tools
the server offers, and calls `search_policies`. With `--ticket` it also searches for a question
the policies do not cover and files a help-desk ticket with `draft_ticket`; the flag stands in for
the user's consent, which an agent must get before it files a ticket. Filing a ticket needs the
mock ticketing API (`uv run python -m campus_rag.ticketing`).

Run from the repository root:

    uv run python scripts/demo_mcp_client.py [--question TEXT] [--ticket [--ticket-question TEXT]]

See runbooks/run-demo.md.
"""

import argparse
import asyncio
import os
import sys
import textwrap
from collections.abc import Iterator, Sequence
from contextlib import ExitStack
from pathlib import Path
from typing import Any, TextIO

from mcp.client import Client
from mcp.client.stdio import StdioServerParameters, stdio_client

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_QUESTION = "Can I keep the old office chairs my department is throwing out?"
DEFAULT_TICKET_QUESTION = "How do I reserve a group study room in the library during finals week?"
PREVIEW_CHARS = 200
TOOL_TIMEOUT_SECONDS = 120.0

EXIT_OK = 0
EXIT_SERVER_FAILED = 1  # the server did not start, or a tool returned an error
EXIT_TICKET_FAILED = 2  # the ticketing API is not running or refused the ticket


class DemoError(Exception):
    """A step failed; the message says what to do."""

    def __init__(self, message: str, exit_code: int) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python scripts/demo_mcp_client.py",
        description="Start the campus-rag MCP server over stdio, list its tools, and call them "
        "the way an AI agent does. See runbooks/run-demo.md.",
    )
    parser.add_argument("--question", default=DEFAULT_QUESTION, help="question for search_policies")
    parser.add_argument("-k", type=int, default=3, help="passages to return (default: 3)")
    parser.add_argument(
        "--ticket",
        action="store_true",
        help="also file a ticket with draft_ticket (stands in for the user's consent)",
    )
    parser.add_argument(
        "--ticket-question",
        default=DEFAULT_TICKET_QUESTION,
        help="question the policies do not answer, used with --ticket",
    )
    parser.add_argument(
        "--server-log",
        type=Path,
        metavar="PATH",
        help="append the server's log lines (stderr) to this file instead of showing them",
    )
    return parser.parse_args(argv)


def _print_header(text: str) -> None:
    print(f"\n--- {text}", flush=True)


def _one_line(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 3].rstrip() + "..."


def _tool_error_text(result: Any) -> str:
    parts = [getattr(item, "text", "") for item in result.content]
    return " ".join(part for part in parts if part) or "no details"


async def _call(client: Client, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    print(f"call_tool({name!r}, {arguments!r})", flush=True)
    result = await client.call_tool(name, arguments, read_timeout_seconds=TOOL_TIMEOUT_SECONDS)
    if result.is_error:
        message = _tool_error_text(result)
        exit_code = EXIT_TICKET_FAILED if name == "draft_ticket" else EXIT_SERVER_FAILED
        raise DemoError(f"{name} returned an error: {message}", exit_code)
    if result.structured_content is None:
        raise DemoError(f"{name} returned no structured result", EXIT_SERVER_FAILED)
    return result.structured_content


def _print_passages(found: dict[str, Any]) -> None:
    results = found["results"]
    print(f"{len(results)} passage(s) for {found['query']!r} ({found['mode']} search)")
    for item in results:
        print(f"\n{item['rank']:>2}. score={item['score']:.4f}  {item['title']}")
        print(f"    {item['heading_path']}")
        print(f"    {item['url']}")
        preview = _one_line(item["text"], PREVIEW_CHARS)
        print(textwrap.fill(preview, width=96, initial_indent="    ", subsequent_indent="    "))


async def _run(args: argparse.Namespace, errlog: TextIO) -> None:
    # An agent's MCP configuration holds the same command, arguments, and working directory. The SDK
    # passes only a few system variables to the server, so forward the campus-rag settings, as an
    # MCP configuration's "env" object would.
    settings = {name: value for name, value in os.environ.items() if name.startswith("CAMPUS_RAG_")}
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "campus_rag.mcp_server"],
        cwd=str(REPO_ROOT),
        env=settings or None,
    )
    print(f"Starting the MCP server over stdio: {sys.executable} -m campus_rag.mcp_server")
    transport = stdio_client(server, errlog=errlog)
    async with Client(transport, read_timeout_seconds=TOOL_TIMEOUT_SECONDS) as client:
        info = client.server_info
        name = f"{info.name} {info.version or ''}".strip() if info else "unknown"
        print(f"Connected to server: {name}")
        if client.instructions:
            print(textwrap.fill(f"Instructions: {client.instructions}", width=100))

        _print_header("Tools the server offers (an agent reads these descriptions)")
        listing = await client.list_tools()
        for tool in listing.tools:
            parameters = ", ".join((tool.input_schema or {}).get("properties", {}))
            print(f"\n{tool.name}({parameters})")
            print(
                textwrap.fill(
                    tool.description or "",
                    width=96,
                    initial_indent="    ",
                    subsequent_indent="    ",
                )
            )

        _print_header("The agent searches the policies")
        found = await _call(client, "search_policies", {"query": args.question, "k": args.k})
        _print_passages(found)

        if not args.ticket:
            return

        _print_header("A question the policies do not cover")
        found = await _call(client, "search_policies", {"query": args.ticket_question, "k": args.k})
        _print_passages(found)
        print(
            "\nNone of these passages answers the question, so the agent offers a ticket and "
            "asks first. --ticket stands in for the user's yes."
        )

        _print_header("The user agreed: the agent files a ticket")
        summary = _one_line(f"Policy question not answered: {args.ticket_question}", 200)
        details = (
            "The policy assistant found no policy that answers this question.\n\n"
            f"Question: {args.ticket_question}"
        )
        ticket = await _call(
            client,
            "draft_ticket",
            {"question": args.ticket_question, "summary": summary, "details": details},
        )
        print(f"Ticket id: {ticket['ticket_id']}")
        print(f"Ticket URL: {ticket['url']}")
        print(f"Status: {ticket['status']}, category: {ticket['category']}")


def _leaves(exc: BaseException) -> Iterator[BaseException]:
    """The exception itself, or every exception inside a (nested) exception group."""
    if isinstance(exc, BaseExceptionGroup):
        for inner in exc.exceptions:
            yield from _leaves(inner)
    else:
        yield exc


def _report(exc: Exception) -> int:
    """Print one clear error and its fix; return the exit code."""
    leaves = list(_leaves(exc))
    demo_error = next((leaf for leaf in leaves if isinstance(leaf, DemoError)), None)
    if demo_error is not None:
        print(f"ERROR: {demo_error}", file=sys.stderr)
        if demo_error.exit_code == EXIT_TICKET_FAILED:
            fix = "start the ticketing API with: uv run python -m campus_rag.ticketing"
        else:
            fix = "check that the database and model server run: docker compose ps"
        print(f"Fix: {fix}", file=sys.stderr)
        return demo_error.exit_code
    first = leaves[0] if leaves else exc
    if isinstance(first, OSError):
        print(f"ERROR: cannot start the MCP server ({first}).", file=sys.stderr)
    else:
        print(f"ERROR: the MCP session failed: {type(first).__name__}: {first}", file=sys.stderr)
    print(
        "Fix: run 'uv run python -m campus_rag.mcp_server' from the repository to see its error; "
        "it should wait silently for input. Check the database and model server with: "
        "docker compose ps",
        file=sys.stderr,
    )
    return EXIT_SERVER_FAILED


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(errors="replace")  # policy text can hold characters the console cannot show
    with ExitStack() as stack:
        errlog: TextIO = sys.stderr
        if args.server_log is not None:
            args.server_log.parent.mkdir(parents=True, exist_ok=True)
            errlog = stack.enter_context(args.server_log.open("a", encoding="utf-8"))
        try:
            asyncio.run(_run(args, errlog))
        except Exception as exc:
            return _report(exc)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
