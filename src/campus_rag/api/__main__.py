"""Serve the HTTP API.

Run with: python -m campus_rag.api [--host 127.0.0.1] [--port 8000].
See runbooks/run-api-and-mcp.md.
"""

import argparse
import sys
from collections.abc import Sequence

import uvicorn

from campus_rag.api.app import create_app
from campus_rag.config import load_settings
from campus_rag.loopback import check_bind_host, port_argument
from campus_rag.services import LiveServices

EXIT_CONFIG_ERROR = 5

_REMOTE_REFUSAL = (
    "the API serves passages of University of Washington policies and binds to loopback only, "
    "because hosting a public service over that content needs UW's permission. Pass "
    "--allow-remote only when you have that permission."
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m campus_rag.api",
        description="Serve the search and answer API. OpenAPI docs are at /docs.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="address to bind (default: 127.0.0.1)")
    parser.add_argument(
        "--port", type=port_argument, default=8000, help="port to bind (default: 8000)"
    )
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="bind a non-loopback address; hosting UW policy content needs UW's permission",
    )
    args = parser.parse_args(argv)
    check_bind_host(
        parser, args.host, args.port, allow_remote=args.allow_remote, reason=_REMOTE_REFUSAL
    )

    try:
        settings = load_settings()
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    app = create_app(lambda: LiveServices(settings))
    print(f"API docs: http://{args.host}:{args.port}/docs", file=sys.stderr)
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
