"""Split extracted documents into heading-prefixed chunks sized for embedding and retrieval.

Sections become chunks in document order. Short sections merge with adjacent sections, and long
sections split at paragraph, then sentence, then word boundaries, with a small overlap between the
pieces of a split section. Every chunk starts with its heading path and fits within `max_tokens`.
"""

import hashlib
import re
from collections.abc import Callable, Sequence
from functools import cache

import tiktoken

from campus_rag.ingest.models import Chunk, ExtractedDocument

HEADING_SEPARATOR = " > "
ELLIPSIS = "\u2026"

_PARAGRAPH_BREAK = re.compile(r"\n[ \t]*\n\s*")
# A sentence ends with . ! or ? plus any closing quotes or brackets, followed by whitespace.
_SENTENCE_END = re.compile(r"[.!?]+[\"'\u201d\u2019)\]]*(?=\s)")
_WORD = re.compile(r"\S+")

Span = tuple[int, int]  # [start, end) offsets into a body string


@cache
def _encoding() -> tiktoken.Encoding:
    return tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    """Number of cl100k_base tokens in `text`; special-token markers count as plain text."""
    return len(_encoding().encode(text, disallowed_special=()))


def chunk_document(
    doc: ExtractedDocument,
    *,
    target_tokens: int = 400,
    max_tokens: int = 800,
    overlap_tokens: int = 50,
    min_tokens: int = 100,
) -> list[Chunk]:
    """Split `doc` into chunks in document order. Deterministic for the same input."""
    if not 0 < target_tokens <= max_tokens:
        raise ValueError("require 0 < target_tokens <= max_tokens")
    if not 0 <= min_tokens <= target_tokens:
        raise ValueError("require 0 <= min_tokens <= target_tokens")
    if overlap_tokens < 0:
        raise ValueError("require overlap_tokens >= 0")

    heading_limit = max(1, target_tokens // 2)
    sections = [
        (tuple(section.heading_path) or (doc.title,), section.text.strip())
        for section in doc.sections
        if section.text.strip()
    ]

    chunks: list[Chunk] = []
    groups = _group_sections(
        sections, doc.title, heading_limit, min_tokens, target_tokens, max_tokens
    )
    for path, body in groups:
        heading = _heading_line(path, heading_limit)
        pieces = _split_body(heading, body, target_tokens, max_tokens, overlap_tokens, min_tokens)
        for piece in pieces:
            content = _content(heading, piece)
            chunks.append(
                Chunk(
                    ordinal=len(chunks),
                    heading_path=heading,
                    content=content,
                    token_count=count_tokens(content),
                    content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                )
            )
    return chunks


def _content(heading: str, body: str) -> str:
    return f"{heading}\n\n{body}"


def _heading_line(path: Sequence[str], limit: int) -> str:
    """Join `path`; truncate with an ellipsis when it exceeds `limit` tokens."""
    line = HEADING_SEPARATOR.join(path)
    if count_tokens(line) <= limit:
        return line

    def fits(k: int) -> bool:
        return count_tokens(line[:k].rstrip() + ELLIPSIS) <= limit

    lo, hi = 0, len(line)  # line[:lo] fits; line[:hi] does not
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if fits(mid):
            lo = mid
        else:
            hi = mid
    return line[:lo].rstrip() + ELLIPSIS


# --- Merging short sections -------------------------------------------------------------------


def _group_sections(
    sections: Sequence[tuple[tuple[str, ...], str]],
    title: str,
    heading_limit: int,
    min_tokens: int,
    target_tokens: int,
    max_tokens: int,
) -> list[tuple[tuple[str, ...], str]]:
    """Merge short sections (content under `min_tokens`) with their neighbors.

    Pass 1 merges runs of adjacent short sections while the result stays within `target_tokens`.
    Pass 2 merges each remaining short group into the following group, or else the previous one,
    while the result stays within `max_tokens`, and repeats until no merge is possible.
    Groups are contiguous ranges of sections, so document order is preserved.
    """

    @cache
    def tokens(i: int, j: int) -> int:
        path, body = _merge(sections[i:j], title)
        return count_tokens(_content(_heading_line(path, heading_limit), body))

    def short(group: Span) -> bool:
        return tokens(*group) < min_tokens

    groups: list[Span] = []
    i = 0
    while i < len(sections):
        j = i + 1
        if short((i, j)):
            while j < len(sections) and short((j, j + 1)) and tokens(i, j + 1) <= target_tokens:
                j += 1
        groups.append((i, j))
        i = j

    k = 0
    while k < len(groups):
        if short(groups[k]):
            start, end = groups[k]
            if k + 1 < len(groups) and tokens(start, groups[k + 1][1]) <= max_tokens:
                groups[k : k + 2] = [(start, groups[k + 1][1])]
                continue  # the merged group may still be short
            if k > 0 and tokens(groups[k - 1][0], end) <= max_tokens:
                groups[k - 1 : k + 1] = [(groups[k - 1][0], end)]
                k -= 1
                continue
        k += 1
    return [_merge(sections[i:j], title) for i, j in groups]


def _merge(
    members: Sequence[tuple[tuple[str, ...], str]], title: str
) -> tuple[tuple[str, ...], str]:
    """Combine sections under their longest common heading prefix (at least the title).

    Each section's headings below the prefix become a line in the body so no heading is lost.
    """
    if len(members) == 1:
        return members[0]
    prefix = _common_prefix([path for path, _ in members]) or (title,)
    parts: list[str] = []
    for path, text in members:
        below = path[len(prefix) :] if path[: len(prefix)] == prefix else path
        if below:
            parts.append(HEADING_SEPARATOR.join(below))
        parts.append(text)
    return prefix, "\n\n".join(parts)


def _common_prefix(paths: Sequence[tuple[str, ...]]) -> tuple[str, ...]:
    prefix = paths[0]
    for path in paths[1:]:
        n = 0
        while n < min(len(prefix), len(path)) and prefix[n] == path[n]:
            n += 1
        prefix = prefix[:n]
    return prefix


# --- Splitting long sections ------------------------------------------------------------------


def _split_body(
    heading: str,
    body: str,
    target_tokens: int,
    max_tokens: int,
    overlap_tokens: int,
    min_tokens: int,
) -> list[str]:
    """Return the body text of each chunk for one section (or merged group)."""

    def fits(span: Span) -> bool:
        return count_tokens(_content(heading, body[span[0] : span[1]])) <= max_tokens

    if fits((0, len(body))):
        return [body]

    heading_cost = count_tokens(_content(heading, ""))
    max_budget = max(1, max_tokens - heading_cost)
    target_budget = max(1, min(target_tokens - heading_cost, max_budget))
    overlap = min(overlap_tokens, max_tokens - target_tokens, target_budget // 2)

    units = _units(body, target_budget)
    pieces: list[Span] = []
    previous_start = 0  # where the previous piece's own text (without its overlap) starts
    i = 0
    while i < len(units):
        own_start = units[i][0]
        start = own_start
        if i > 0 and overlap > 0:
            previous_end = units[i - 1][1]
            tail = _tail_start(body, previous_start, previous_end, overlap)
            if tail < previous_end:
                start = tail
        j = _fit(body, start, units, i, target_budget)
        end = units[j - 1][1]
        pieces.extend(_within_max(body, start, own_start, end, fits, max_budget))
        previous_start = own_start
        i = j

    # A short trailing piece rejoins its predecessor when the result still fits.
    if len(pieces) > 1:
        last = pieces[-1]
        merged = (pieces[-2][0], last[1])
        if count_tokens(_content(heading, body[last[0] : last[1]])) < min_tokens and fits(merged):
            pieces[-2:] = [merged]
    return [body[s:e] for s, e in pieces]


def _within_max(
    body: str,
    start: int,
    own_start: int,
    end: int,
    fits: Callable[[Span], bool],
    max_budget: int,
) -> list[Span]:
    """Guarantee the max-token invariant: drop the overlap, then split by words if needed."""
    if fits((start, end)):
        return [(start, end)]
    if fits((own_start, end)):
        return [(own_start, end)]
    budget = max_budget
    while True:
        spans = _pack_words(body, own_start, end, budget)
        if all(fits(span) for span in spans) or budget == 1:
            return spans
        budget = max(1, budget // 2)


def _units(text: str, budget: int) -> list[Span]:
    """Atomic spans for packing: paragraphs, or sentences and word runs of oversized paragraphs."""
    units: list[Span] = []
    for paragraph in _paragraph_spans(text, 0, len(text)):
        if _span_tokens(text, paragraph) <= budget:
            units.append(paragraph)
            continue
        for sentence in _sentence_spans(text, *paragraph):
            if _span_tokens(text, sentence) <= budget:
                units.append(sentence)
            else:
                units.extend(_pack_words(text, *sentence, budget))
    return units


def _span_tokens(text: str, span: Span) -> int:
    return count_tokens(text[span[0] : span[1]])


def _trimmed(text: str, start: int, end: int) -> list[Span]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return [(start, end)] if start < end else []


def _paragraph_spans(text: str, start: int, end: int) -> list[Span]:
    spans: list[Span] = []
    pos = start
    for match in _PARAGRAPH_BREAK.finditer(text, start, end):
        spans.extend(_trimmed(text, pos, match.start()))
        pos = match.end()
    spans.extend(_trimmed(text, pos, end))
    return spans


def _sentence_spans(text: str, start: int, end: int) -> list[Span]:
    spans: list[Span] = []
    pos = start
    for match in _SENTENCE_END.finditer(text, start, end):
        spans.extend(_trimmed(text, pos, match.end()))
        pos = match.end()
    spans.extend(_trimmed(text, pos, end))
    return spans


def _word_spans(text: str, start: int, end: int) -> list[Span]:
    return [(m.start(), m.end()) for m in _WORD.finditer(text, start, end)]


def _fit(text: str, start: int, spans: Sequence[Span], i: int, budget: int) -> int:
    """Largest j > i such that text[start:spans[j-1].end] fits in `budget`; at least i + 1."""

    def fits(j: int) -> bool:
        return count_tokens(text[start : spans[j - 1][1]]) <= budget

    n = len(spans)
    lo = i + 1
    if not fits(lo):
        return lo
    step = 1
    while True:  # exponential search keeps the cost proportional to the piece size
        probe = min(lo + step, n)
        if probe == lo:
            return lo
        if not fits(probe):
            hi = probe
            break
        lo = probe
        step *= 2
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if fits(mid):
            lo = mid
        else:
            hi = mid
    return lo


def _pack_words(text: str, start: int, end: int, budget: int) -> list[Span]:
    """Split text[start:end] into contiguous word runs of at most `budget` tokens."""
    words = _word_spans(text, start, end)
    spans: list[Span] = []
    i = 0
    while i < len(words):
        word_start, word_end = words[i]
        if count_tokens(text[word_start:word_end]) > budget:
            spans.extend(_split_chars(text, word_start, word_end, budget))
            i += 1
            continue
        j = _fit(text, word_start, words, i, budget)
        spans.append((word_start, words[j - 1][1]))
        i = j
    return spans


def _split_chars(text: str, start: int, end: int, budget: int) -> list[Span]:
    """Last resort for a single word longer than the budget: split it by characters."""
    spans: list[Span] = []
    pos = start
    while pos < end:
        if count_tokens(text[pos:end]) <= budget:
            spans.append((pos, end))
            break
        lo, hi = pos + 1, end  # take at least one character; text[pos:hi] is too long
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if count_tokens(text[pos:mid]) <= budget:
                lo = mid
            else:
                hi = mid
        spans.append((pos, lo))
        pos = lo
    return spans


def _tail_start(text: str, lo: int, hi: int, budget: int) -> int:
    """Start offset of the overlap taken from the end of text[lo:hi].

    Prefers whole trailing sentences; falls back to trailing words when whole sentences would give
    less than half the budget. Returns `hi` when no overlap fits.
    """
    sentences = [s for p in _paragraph_spans(text, lo, hi) for s in _sentence_spans(text, *p)]
    by_sentence = _longest_suffix(text, [s for s, _ in sentences], hi, budget)
    if by_sentence < hi and count_tokens(text[by_sentence:hi]) >= budget // 2:
        return by_sentence
    return _longest_suffix(text, [s for s, _ in _word_spans(text, lo, hi)], hi, budget)


def _longest_suffix(text: str, starts: Sequence[int], hi: int, budget: int) -> int:
    best = hi
    for start in reversed(starts):
        if count_tokens(text[start:hi]) > budget:
            break
        best = start
    return best
