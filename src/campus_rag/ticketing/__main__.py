"""Serve the mock ticketing API.

Run with: python -m campus_rag.ticketing [--host 127.0.0.1] [--port 8001].
See runbooks/run-api-and-mcp.md.
"""

import argparse
import sys
from collections.abc import Sequence

import uvicorn

from campus_rag.config import load_settings
from campus_rag.loopback import check_bind_host, port_argument
from campus_rag.ticketing.app import create_app

EXIT_CONFIG_ERROR = 5

_REMOTE_REFUSAL = (
    "the mock ticketing API has no authentication and stores the questions people typed, so it "
    "binds to loopback only. Pass --allow-remote only for a network you control."
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m campus_rag.ticketing",
        description="Serve the mock ticketing API. OpenAPI docs are at /docs.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="address to bind (default: 127.0.0.1)")
    parser.add_argument(
        "--port", type=port_argument, default=8001, help="port to bind (default: 8001)"
    )
    parser.add_argument("--allow-remote", action="store_true", help="bind a non-loopback address")
    args = parser.parse_args(argv)
    check_bind_host(
        parser, args.host, args.port, allow_remote=args.allow_remote, reason=_REMOTE_REFUSAL
    )

    try:
        load_settings()  # fail early on invalid configuration
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    print(f"Ticketing docs: http://{args.host}:{args.port}/docs", file=sys.stderr)
    uvicorn.run(create_app(), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
