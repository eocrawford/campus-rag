"""The loopback-only rule the API and ticketing servers share."""

import argparse
import ipaddress
import sys


def is_loopback_host(host: str) -> bool:
    """True for `localhost` and loopback IP addresses; other names are not resolved."""
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def port_argument(value: str) -> int:
    """An argparse `type` for a TCP port."""
    try:
        port = int(value)
    except ValueError:
        port = 0
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"expected a port from 1 to 65535, got {value!r}")
    return port


def check_bind_host(
    parser: argparse.ArgumentParser, host: str, port: int, *, allow_remote: bool, reason: str
) -> None:
    """Exit with a usage error for a non-loopback `host` unless `allow_remote`, else warn."""
    if is_loopback_host(host):
        return
    if not allow_remote:
        parser.error(f"refusing to bind {host!r}: {reason}")
    print(f"WARNING: serving on {host}:{port}; the server has no authentication.", file=sys.stderr)
