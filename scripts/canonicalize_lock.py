"""Rewrite package-mirror URLs in uv.lock to their canonical PyPI URLs.

On a network that only reaches PyPI through a mirror, ``uv lock`` records the mirror's index and
download URLs. This script replaces them with the canonical ``https://pypi.org/simple`` registry
and ``files.pythonhosted.org`` download URLs, so the committed lock works for everyone. For each
file it looks up the package version on pypi.org and refuses to rewrite unless the sha256 (and the
size, when recorded) match exactly, so a mirror can never substitute different bytes.

Usage (normally run by ``scripts/update-lock-via-mirror.ps1``):

    python scripts/canonicalize_lock.py path/to/uv.lock

Exit code 0 means the file was rewritten (or was already canonical); 1 means a check failed and the
file was left unchanged.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from collections.abc import Callable
from pathlib import Path
from urllib.parse import unquote, urlsplit

PYPI_REGISTRY = "https://pypi.org/simple"
PYPI_FILES_HOST = "files.pythonhosted.org"

_PACKAGE_HEADER = re.compile(r'^name = "([^"]+)"\nversion = "([^"]+)"', re.MULTILINE)
_REGISTRY = re.compile(r'registry = "([^"]+)"')
_FILE_ENTRY = re.compile(
    r'url = "(?P<url>[^"]+)", hash = "sha256:(?P<sha>[0-9a-f]{64})"'
    r'(?:, size = (?P<size>\d+))?(?P<upload>, upload-time = "[^"]*")?'
)

PypiLookup = Callable[[str, str], dict[str, dict]]


class CanonicalizeError(Exception):
    pass


def pypi_files(name: str, version: str) -> dict[str, dict]:
    """Return pypi.org's file records for one release, keyed by filename."""
    with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/{version}/json", timeout=30) as r:
        data = json.load(r)
    return {record["filename"]: record for record in data["urls"]}


def canonicalize(text: str, lookup: PypiLookup = pypi_files) -> tuple[str, int]:
    """Return the canonical lock text and the number of file URLs rewritten."""
    cache: dict[tuple[str, str], dict[str, dict]] = {}
    rewritten = 0
    blocks = text.split("[[package]]\n")
    out = [blocks[0]]
    for block in blocks[1:]:
        header = _PACKAGE_HEADER.match(block)
        if not header:
            out.append(block)
            continue
        name, version = header.groups()

        def replace_file(match: re.Match[str], name: str = name, version: str = version) -> str:
            nonlocal rewritten
            url = match["url"]
            if urlsplit(url).hostname == PYPI_FILES_HOST:
                return match.group(0)
            filename = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
            key = (name, version)
            if key not in cache:
                cache[key] = lookup(name, version)
            record = cache[key].get(filename)
            if record is None:
                raise CanonicalizeError(
                    f"{name} {version}: {filename} is not published on pypi.org"
                )
            if record["digests"]["sha256"] != match["sha"]:
                raise CanonicalizeError(
                    f"{name} {version}: sha256 of {filename} differs from pypi.org"
                )
            if match["size"] and int(match["size"]) != record["size"]:
                raise CanonicalizeError(
                    f"{name} {version}: size of {filename} differs from pypi.org"
                )
            rewritten += 1
            result = f'url = "{record["url"]}", hash = "sha256:{match["sha"]}"'
            if match["size"]:
                result += f", size = {match['size']}"
            if match["upload"]:
                result += f', upload-time = "{record["upload_time_iso_8601"]}"'
            return result

        block = _FILE_ENTRY.sub(replace_file, block)
        out.append(block)

    result = "[[package]]\n".join(out)
    result = _REGISTRY.sub(f'registry = "{PYPI_REGISTRY}"', result)
    return result, rewritten


def non_canonical_urls(text: str) -> list[str]:
    """URLs in registry or file entries that do not point at canonical PyPI."""
    problems = [r for r in _REGISTRY.findall(text) if r.rstrip("/") != PYPI_REGISTRY]
    problems += [
        m["url"]
        for m in _FILE_ENTRY.finditer(text)
        if urlsplit(m["url"]).hostname != PYPI_FILES_HOST
    ]
    return problems


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python scripts/canonicalize_lock.py path/to/uv.lock", file=sys.stderr)
        return 2
    path = Path(args[0])
    text = path.read_text(encoding="utf-8")
    try:
        result, rewritten = canonicalize(text)
    except (CanonicalizeError, OSError) as exc:
        print(f"ERROR: {exc}. {path} is unchanged.", file=sys.stderr)
        return 1
    leftovers = non_canonical_urls(result)
    if leftovers:
        print(
            f"ERROR: non-canonical URL remains: {leftovers[0]}. {path} is unchanged.",
            file=sys.stderr,
        )
        return 1
    if result != text:
        path.write_text(result, encoding="utf-8", newline="\n")
    print(f"Rewrote {rewritten} file URL(s); every registry and file URL now points at PyPI.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
