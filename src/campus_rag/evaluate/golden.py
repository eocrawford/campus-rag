"""Golden question set: loading, validation, and matching retrieved chunks to expected targets."""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

import psycopg

from campus_rag.config import find_repo_root
from campus_rag.retrieve.models import RetrievedChunk

QuestionType = Literal["paraphrase", "exact_term", "unanswerable"]
QUESTION_TYPES: tuple[QuestionType, ...] = ("paraphrase", "exact_term", "unanswerable")
HEADING_SEPARATOR = " > "

_REQUIRED_FIELDS = ("id", "question", "type", "expected", "reference_answer")
_OPTIONAL_FIELDS = ("follow_up_of", "notes")
_TARGET_FIELDS = ("url", "heading_prefix")


class GoldenSetError(Exception):
    """Raised when the golden set file is missing or malformed."""


@dataclass(frozen=True)
class GoldenTarget:
    """A passage that answers a question: a document URL and a heading path or its prefix."""

    url: str
    heading_prefix: str


@dataclass(frozen=True)
class GoldenQuestion:
    id: str
    question: str
    type: QuestionType
    expected: tuple[GoldenTarget, ...]  # empty only for unanswerable questions
    reference_answer: str
    follow_up_of: str | None = None  # id of the earlier question this one continues
    notes: str | None = None


def default_golden_set_path() -> Path:
    """Return <repo root>/eval/golden_set.jsonl."""
    root = find_repo_root() or find_repo_root(Path(__file__).parent)
    if root is None:
        raise GoldenSetError("cannot find the repository root (no pyproject.toml)")
    return root / "eval" / "golden_set.jsonl"


def load_golden_set(path: Path | None = None) -> list[GoldenQuestion]:
    """Load and validate the golden set. Blank lines are ignored."""
    path = path or default_golden_set_path()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise GoldenSetError(f"{path}: cannot read file: {exc}") from exc

    questions: list[GoldenQuestion] = []
    seen_ids: set[str] = set()
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        where = f"{path}:{line_number}"
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            raise GoldenSetError(f"{where}: invalid JSON: {exc.msg}") from exc
        question = _parse_question(where, entry)
        where = f"{where} ({question.id})"
        if question.id in seen_ids:
            raise GoldenSetError(f"{where}: duplicate id {question.id!r}")
        if question.follow_up_of is not None and question.follow_up_of not in seen_ids:
            raise GoldenSetError(
                f"{where}: follow_up_of {question.follow_up_of!r} must name an earlier question"
            )
        seen_ids.add(question.id)
        questions.append(question)

    if not questions:
        raise GoldenSetError(f"{path}: the golden set has no questions")
    return questions


def _parse_question(where: str, entry: Any) -> GoldenQuestion:
    if not isinstance(entry, dict):
        raise GoldenSetError(f"{where}: each line must be a JSON object")
    missing = [field for field in _REQUIRED_FIELDS if field not in entry]
    if missing:
        raise GoldenSetError(f"{where}: missing required field(s): {', '.join(missing)}")
    unknown = sorted(set(entry) - set(_REQUIRED_FIELDS) - set(_OPTIONAL_FIELDS))
    if unknown:
        raise GoldenSetError(f"{where}: unknown field(s): {', '.join(unknown)}")

    question_id = _text(where, entry, "id")
    where = f"{where} ({question_id})"
    question_type = entry["type"]
    if question_type not in QUESTION_TYPES:
        raise GoldenSetError(
            f"{where}: type must be one of {', '.join(QUESTION_TYPES)}, got {question_type!r}"
        )

    raw_expected = entry["expected"]
    if not isinstance(raw_expected, list):
        raise GoldenSetError(f"{where}: expected must be a list")
    expected = tuple(
        _parse_target(f"{where}: expected[{i}]", t) for i, t in enumerate(raw_expected)
    )
    if question_type == "unanswerable" and expected:
        raise GoldenSetError(f"{where}: unanswerable questions must have an empty expected list")
    if question_type != "unanswerable" and not expected:
        raise GoldenSetError(f"{where}: answerable questions need at least one expected target")

    follow_up_of = _optional_text(where, entry, "follow_up_of")
    if follow_up_of == question_id:
        raise GoldenSetError(f"{where}: a question cannot follow up on itself")

    return GoldenQuestion(
        id=question_id,
        question=_text(where, entry, "question"),
        type=cast(QuestionType, question_type),
        expected=expected,
        reference_answer=_text(where, entry, "reference_answer"),
        follow_up_of=follow_up_of,
        notes=_optional_text(where, entry, "notes"),
    )


def _parse_target(where: str, entry: Any) -> GoldenTarget:
    if not isinstance(entry, dict):
        raise GoldenSetError(f"{where}: must be an object with url and heading_prefix")
    if set(entry) != set(_TARGET_FIELDS):
        raise GoldenSetError(f"{where}: must have exactly the fields url and heading_prefix")
    return GoldenTarget(
        url=_text(where, entry, "url"), heading_prefix=_text(where, entry, "heading_prefix")
    )


def _text(where: str, entry: dict[str, Any], field: str) -> str:
    value = entry[field]
    if not isinstance(value, str) or not value.strip():
        raise GoldenSetError(f"{where}: {field} must be a non-empty string")
    return value


def _optional_text(where: str, entry: dict[str, Any], field: str) -> str | None:
    if entry.get(field) is None:
        return None
    return _text(where, entry, field)


def _normalize_url(url: str) -> str:
    return url.strip().rstrip("/")


def _segments(heading_path: str) -> list[str]:
    return [segment.strip() for segment in heading_path.strip().split(HEADING_SEPARATOR)]


def _is_segment_prefix(prefix: list[str], path: list[str]) -> bool:
    return len(prefix) <= len(path) and path[: len(prefix)] == prefix


def chunk_matches(chunk_url: str, chunk_heading_path: str, target: GoldenTarget) -> bool:
    """True when the chunk is in the target's document and either heading path is a prefix of
    the other, compared whole segment by whole segment."""
    if _normalize_url(chunk_url) != _normalize_url(target.url):
        return False
    chunk_segments = _segments(chunk_heading_path)
    target_segments = _segments(target.heading_prefix)
    return _is_segment_prefix(target_segments, chunk_segments) or _is_segment_prefix(
        chunk_segments, target_segments
    )


def first_relevant_rank(
    question: GoldenQuestion, retrieved: Sequence[RetrievedChunk]
) -> int | None:
    """1-based position of the first retrieved chunk that matches any expected target."""
    for position, chunk in enumerate(retrieved, start=1):
        if any(chunk_matches(chunk.url, chunk.heading_path, t) for t in question.expected):
            return position
    return None


def validate_against_db(conn: psycopg.Connection, questions: Sequence[GoldenQuestion]) -> list[str]:
    """Check every target against the stored corpus and return one message per problem.

    Reports a target whose URL is not in `documents`, whose heading matches no stored chunk, or
    whose heading appears in no stored heading path. The last check catches typos that a coarse
    chunk (for example one headed only by the document title) would otherwise match.
    """
    headings_by_url: dict[str, list[str]] = {}
    rows = conn.execute(
        "SELECT d.url, c.heading_path FROM documents d LEFT JOIN chunks c ON c.document_id = d.id"
    ).fetchall()
    for url, heading_path in rows:
        headings = headings_by_url.setdefault(_normalize_url(url), [])
        if heading_path is not None:
            headings.append(heading_path)

    problems: list[str] = []
    for question in questions:
        for target in question.expected:
            label = f"{question.id}: {target.url} | {target.heading_prefix!r}"
            headings = headings_by_url.get(_normalize_url(target.url))
            if headings is None:
                problems.append(f"{label}: URL is not in documents")
                continue
            if not any(chunk_matches(target.url, h, target) for h in headings):
                problems.append(f"{label}: no stored chunk matches")
                continue
            target_segments = _segments(target.heading_prefix)
            if not any(_is_segment_prefix(target_segments, _segments(h)) for h in headings):
                problems.append(f"{label}: heading does not appear in any stored heading path")
    return problems
