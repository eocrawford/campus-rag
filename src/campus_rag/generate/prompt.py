"""Assemble the grounded-answer prompt: instructions, numbered passages, and the question."""

from collections.abc import Sequence
from dataclasses import dataclass, replace

from campus_rag.generate.models import REFUSAL_TEXT
from campus_rag.ingest.chunk import count_tokens
from campus_rag.retrieve.models import RetrievedChunk

MAX_PASSAGES = 6
PASSAGE_TOKEN_BUDGET = 3000
MAX_ANSWER_WORDS = 150

SYSTEM_PROMPT = f"""\
You answer questions about university policies using only the numbered passages provided.

Rules:
1. Use only facts stated in the passages. Never use outside knowledge.
2. Cite the supporting passage numbers in square brackets after every claim, for example [1] or \
[2][3]. Every sentence of the answer needs at least one citation.
3. Keep the answer under {MAX_ANSWER_WORDS} words.
4. If the passages do not contain the answer, reply with exactly this sentence and nothing else:
{REFUSAL_TEXT}"""


@dataclass(frozen=True)
class Prompt:
    messages: tuple[dict[str, str], ...]
    passages: tuple[RetrievedChunk, ...]  # numbered from 1 in the prompt
    passage_tokens: int  # tokens in the rendered passage blocks


def build_prompt(
    question: str,
    chunks: Sequence[RetrievedChunk],
    *,
    max_passages: int = MAX_PASSAGES,
    token_budget: int = PASSAGE_TOKEN_BUDGET,
) -> Prompt:
    """Build the chat messages for `question` from retrieved `chunks`.

    Passages are taken in retrieval-rank order. A passage that would push the rendered passages
    past `token_budget` is skipped and smaller later passages are still considered. The top
    passage is always included, truncated to fit when it alone exceeds the budget.
    """
    passages = select_passages(chunks, max_passages=max_passages, token_budget=token_budget)
    blocks = [render_passage(number, chunk) for number, chunk in enumerate(passages, start=1)]
    passage_text = "\n\n".join(blocks)
    user = f"Passages:\n\n{passage_text}\n\nQuestion: {question.strip()}"
    return Prompt(
        messages=(
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ),
        passages=tuple(passages),
        passage_tokens=sum(count_tokens(block) for block in blocks),
    )


def select_passages(
    chunks: Sequence[RetrievedChunk],
    *,
    max_passages: int = MAX_PASSAGES,
    token_budget: int = PASSAGE_TOKEN_BUDGET,
) -> list[RetrievedChunk]:
    """Choose up to `max_passages` chunks, by rank, whose rendered blocks fit `token_budget`."""
    if max_passages < 1:
        raise ValueError("max_passages must be >= 1")
    if token_budget < 1:
        raise ValueError("token_budget must be >= 1")
    ordered = sorted(chunks, key=lambda chunk: chunk.rank)
    selected: list[RetrievedChunk] = []
    used = 0
    for chunk in ordered:
        if len(selected) == max_passages:
            break
        cost = count_tokens(render_passage(len(selected) + 1, chunk))
        if used + cost <= token_budget:
            selected.append(chunk)
            used += cost
        elif not selected:
            truncated = _truncate_to_fit(chunk, token_budget)
            selected.append(truncated)
            used += count_tokens(render_passage(1, truncated))
    return selected


def render_passage(number: int, chunk: RetrievedChunk) -> str:
    """One numbered passage: `[n] heading path`, a URL line, then the chunk body."""
    heading = chunk.heading_path or chunk.title
    if chunk.title and not heading.startswith(chunk.title):
        heading = f"{chunk.title} > {heading}"
    return f"[{number}] {heading}\nURL: {chunk.url}\n{_body(chunk)}"


def _body(chunk: RetrievedChunk) -> str:
    """Chunk text without its leading heading-path line, which the passage header repeats."""
    content = chunk.content.strip()
    first, _, rest = content.partition("\n")
    if chunk.heading_path and first.strip() == chunk.heading_path.strip():
        return rest.strip()
    return content


def _truncate_to_fit(chunk: RetrievedChunk, token_budget: int) -> RetrievedChunk:
    """Cut the chunk's body at a word boundary so its rendered block fits `token_budget`."""
    keeps_heading = chunk.heading_path and chunk.content.startswith(chunk.heading_path)
    heading_line = f"{chunk.heading_path}\n\n" if keeps_heading else ""
    words = _body(chunk).split(" ")

    def candidate(word_count: int) -> RetrievedChunk:
        text = " ".join(words[:word_count]).rstrip() + " ..."
        content = heading_line + text
        return replace(chunk, content=content, token_count=count_tokens(content))

    low, high = 0, len(words)
    while low < high:  # largest word count whose rendered passage fits
        middle = (low + high + 1) // 2
        if count_tokens(render_passage(1, candidate(middle))) <= token_budget:
            low = middle
        else:
            high = middle - 1
    return candidate(low)
