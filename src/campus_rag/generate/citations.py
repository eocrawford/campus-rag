"""Validate the model's citations and enforce refusals. Pure functions; no I/O.

The model cites passages with markers such as `[1]`, `[1, 3]`, `[1][2]`, or `[2-3]`. Numbers that
do not match a supplied passage are removed, and an answer left with no valid citation becomes a
refusal, so no uncited claim reaches the user.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass

from campus_rag.generate.models import REFUSAL_TEXT, Citation
from campus_rag.retrieve.models import RetrievedChunk

MAX_RANGE_SPAN = 10  # a wider range such as [1-500] is read as its two endpoints

_GROUP = r"\[\s*\d+(?:\s*[,\-\u2013\u2014]\s*\d+)*\s*\]"
_MARKER_RUN = re.compile(rf"{_GROUP}(?:[ \t]*{_GROUP})*")
_GROUP_RE = re.compile(_GROUP)
_ITEM = re.compile(r"(\d+)(?:\s*[\-\u2013\u2014]\s*(\d+))?")


@dataclass(frozen=True)
class CheckedAnswer:
    text: str  # answer with normalized valid markers, or REFUSAL_TEXT
    refused: bool
    citations: tuple[Citation, ...]  # valid citations in order of first use; empty when refused
    invalid_citations: tuple[int, ...]  # cited numbers with no supplied passage, in first-use order


def check_answer(raw_text: str, passages: Sequence[RetrievedChunk]) -> CheckedAnswer:
    """Remove invalid citations from `raw_text` and decide whether the answer is a refusal."""
    text, used, invalid = normalize_citations(raw_text, len(passages))
    if is_refusal(raw_text) or not used or not text:
        return CheckedAnswer(REFUSAL_TEXT, True, (), invalid)
    citations = tuple(_citation(number, passages[number - 1]) for number in used)
    return CheckedAnswer(text, False, citations, invalid)


def normalize_citations(
    text: str, passage_count: int
) -> tuple[str, tuple[int, ...], tuple[int, ...]]:
    """Rewrite citation markers as `[1][3]`, keeping only numbers 1..`passage_count`.

    Returns the cleaned text, the valid numbers in order of first use, and the invalid numbers in
    order of first use. Adjacent markers merge; markers with no valid number are removed.
    """
    used: list[int] = []
    invalid: list[int] = []

    def replace_run(match: re.Match[str]) -> str:
        valid_here: list[int] = []
        for number in parse_marker_run(match.group(0)):
            if 1 <= number <= passage_count:
                if number not in valid_here:
                    valid_here.append(number)
                if number not in used:
                    used.append(number)
            elif number not in invalid:
                invalid.append(number)
        return "".join(f"[{number}]" for number in valid_here)

    cleaned = _MARKER_RUN.sub(replace_run, text)
    return _normalize_spacing(cleaned), tuple(used), tuple(invalid)


def parse_marker_run(marker_text: str) -> list[int]:
    """Numbers cited in one or more adjacent markers, in written order, ranges expanded."""
    numbers: list[int] = []
    for group in _GROUP_RE.findall(marker_text):
        for start_text, end_text in _ITEM.findall(group):
            start = int(start_text)
            if not end_text:
                numbers.append(start)
                continue
            end = int(end_text)
            if start <= end and end - start <= MAX_RANGE_SPAN:
                numbers.extend(range(start, end + 1))
            else:
                numbers.extend((start, end))
    return numbers


def is_refusal(text: str) -> bool:
    """True when `text` is, or begins with, the refusal sentence (ignoring case and spacing)."""
    refusal = _comparable(REFUSAL_TEXT).rstrip(".")
    return _comparable(text).startswith(refusal)


def _comparable(text: str) -> str:
    text = text.replace("\u2019", "'").replace("\u2018", "'").strip().strip("\"'*` ")
    return " ".join(text.split()).casefold()


def _normalize_spacing(text: str) -> str:
    text = re.sub(r"(?<=[^\s(\[\]])(?=\[\d)", " ", text)  # "word[1]" -> "word [1]"
    text = re.sub(r"[ \t]+([.,;:!?)])", r"\1", text)  # space left before punctuation
    text = re.sub(r"[ \t]{2,}", " ", text)
    lines = [line.rstrip() for line in text.strip().splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines))


def _citation(number: int, chunk: RetrievedChunk) -> Citation:
    return Citation(
        number=number,
        chunk_id=chunk.chunk_id,
        url=chunk.url,
        title=chunk.title,
        heading_path=chunk.heading_path,
    )
