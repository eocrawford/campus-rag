"""Verify links in the repository's Markdown files.

Checks two kinds of links in every tracked-area ``*.md`` file:

* GitHub permalinks to this repository, such as
  ``https://github.com/eocrawford/campus-rag/blob/<sha>/<path>#L10-L20``. Each must point to a file
  that exists at that commit, the commit must be in the current branch's history, and any line
  range must lie inside the file. The glossary and walkthrough pin code links this way.
* Relative links between files, including ``#anchor`` fragments, which must match a heading.

Run from anywhere:

    uv run python scripts/verify_glossary.py            # check everything
    uv run python scripts/verify_glossary.py --verbose  # also print the first line of each range
    uv run python scripts/verify_glossary.py --pin <sha>  # require every permalink to use <sha>

Exit code 0 means no problems; 1 means at least one problem was printed.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote

REPO = Path(__file__).resolve().parent.parent
PERMALINK = re.compile(
    r"https://github\.com/eocrawford/campus-rag/blob/([0-9a-f]{7,40})/([^\s)#]+)"
    r"(?:#L(\d+)(?:-L(\d+))?)?"
)
RELATIVE = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
SKIP_DIRS = {
    ".git",
    ".venv",
    "node_modules",
    "data",
    "eval-results",
    ".pytest_cache",
    ".ruff_cache",
}


def git(*args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True)


def markdown_files() -> list[Path]:
    return sorted(
        p for p in REPO.rglob("*.md") if not SKIP_DIRS.intersection(p.relative_to(REPO).parts)
    )


def slug(heading: str) -> str:
    text = re.sub(r"[`*_]", "", heading.strip().lower())
    return re.sub(r"[^\w\- ]", "", text).replace(" ", "-")


def outside_code_blocks(path: Path) -> list[str]:
    lines, in_code = [], False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("```"):
            in_code = not in_code
        elif not in_code:
            lines.append(line)
    return lines


def anchors(path: Path) -> set[str]:
    return {slug(line.lstrip("#")) for line in outside_code_blocks(path) if line.startswith("#")}


class PermalinkChecker:
    def __init__(self, pin: str | None, verbose: bool) -> None:
        self.pin = pin
        self.verbose = verbose
        self._files: dict[tuple[str, str], list[str] | None] = {}
        self._commits: dict[str, bool] = {}

    def _in_history(self, sha: str) -> bool:
        if sha not in self._commits:
            self._commits[sha] = git("merge-base", "--is-ancestor", sha, "HEAD").returncode == 0
        return self._commits[sha]

    def _lines(self, sha: str, path: str) -> list[str] | None:
        key = (sha, path)
        if key not in self._files:
            result = git("show", f"{sha}:{path}")
            self._files[key] = (
                result.stdout.decode("utf-8", errors="replace").splitlines()
                if result.returncode == 0
                else None
            )
        return self._files[key]

    def check(self, source: Path, url_match: re.Match[str]) -> list[str]:
        sha, path, start, end = url_match.groups()
        where = f"{source.relative_to(REPO)}: {url_match.group(0)}"
        if self.pin and not self.pin.startswith(sha) and not sha.startswith(self.pin):
            return [f"NOT PINNED TO {self.pin}  {where}"]
        if not self._in_history(sha):
            return [f"UNKNOWN COMMIT  {where}"]
        lines = self._lines(sha, unquote(path))
        if lines is None:
            return [f"MISSING FILE  {where}"]
        if start is None:
            return []
        first, last = int(start), int(end or start)
        if not 1 <= first <= last <= len(lines):
            return [f"RANGE OUTSIDE FILE ({len(lines)} lines)  {where}"]
        if self.verbose:
            print(f"  {path}#L{first}-L{last}: {lines[first - 1].strip()[:80]}")
        return []


def check_relative(source: Path, target: str) -> list[str]:
    if re.match(r"^(https?:|mailto:)", target):
        return []
    file_part, _, fragment = target.partition("#")
    destination = source if not file_part else (source.parent / unquote(file_part)).resolve()
    where = f"{source.relative_to(REPO)}: {target}"
    if not destination.exists():
        return [f"BROKEN LINK  {where}"]
    if fragment and destination.suffix == ".md" and fragment not in anchors(destination):
        return [f"BAD ANCHOR  {where}"]
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--pin", help="require every permalink to use this commit SHA")
    parser.add_argument("--verbose", action="store_true", help="print the first line of each range")
    args = parser.parse_args(argv)

    checker = PermalinkChecker(args.pin, args.verbose)
    problems: list[str] = []
    files = markdown_files()
    permalinks = relative = 0
    for source in files:
        for line in outside_code_blocks(source):
            for url_match in PERMALINK.finditer(line):
                permalinks += 1
                problems += checker.check(source, url_match)
            for target in RELATIVE.findall(line):
                if PERMALINK.match(target) or re.match(r"^(https?:|mailto:)", target):
                    continue
                relative += 1
                problems += check_relative(source, target)

    for problem in problems:
        print(problem)
    print(
        f"Checked {len(files)} Markdown files: {permalinks} permalinks, {relative} relative links, "
        f"{len(problems)} problem(s)."
    )
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
