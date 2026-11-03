"""Tests for prompt assembly: passage order, numbering, and the token budget."""

import pytest
from fakes import make_chunk

from campus_rag.generate.models import REFUSAL_TEXT
from campus_rag.generate.prompt import (
    SYSTEM_PROMPT,
    build_prompt,
    render_passage,
    select_passages,
)
from campus_rag.ingest.chunk import count_tokens


def _cost(chunk) -> int:
    return count_tokens(render_passage(1, chunk))


def test_prompt_numbers_passages_in_rank_order_with_heading_and_url() -> None:
    chunks = [make_chunk(3), make_chunk(1), make_chunk(2)]
    prompt = build_prompt("  Who approves leave?  ", chunks)

    assert [chunk.rank for chunk in prompt.passages] == [1, 2, 3]
    system, user = prompt.messages
    assert system == {"role": "system", "content": SYSTEM_PROMPT}
    assert user["role"] == "user"
    text = user["content"]
    assert text.index("[1] Sample Policy 1 > Section 1") < text.index("[2] Sample Policy 2")
    assert "URL: https://policy.example.edu/sample-1/" in text
    assert "Placeholder rule number 1 applies." in text
    assert text.endswith("Question: Who approves leave?")
    # The heading line at the start of each chunk is not repeated under its passage header.
    assert text.count("Sample Policy 1 > Section 1") == 1


def test_system_prompt_states_the_rules() -> None:
    assert REFUSAL_TEXT in SYSTEM_PROMPT
    assert "150 words" in SYSTEM_PROMPT
    assert "[1]" in SYSTEM_PROMPT
    assert "only" in SYSTEM_PROMPT


def test_title_is_added_when_the_heading_path_omits_it() -> None:
    chunk = make_chunk(1, heading_path="Definitions", title="Sample Policy")
    assert render_passage(4, chunk).startswith("[4] Sample Policy > Definitions\nURL: ")


def test_at_most_max_passages_are_used() -> None:
    chunks = [make_chunk(rank) for rank in range(1, 11)]
    assert len(build_prompt("q", chunks).passages) == 6
    assert len(build_prompt("q", chunks, max_passages=2).passages) == 2


def test_a_passage_over_budget_is_skipped_and_smaller_later_ones_still_fit() -> None:
    small = [make_chunk(rank) for rank in (1, 3, 4)]
    big = make_chunk(2, content="word " * 400)
    budget = sum(_cost(chunk) for chunk in small) + 5

    selected = select_passages([small[0], big, *small[1:]], token_budget=budget)

    assert [chunk.rank for chunk in selected] == [1, 3, 4]
    rendered = [render_passage(n, c) for n, c in enumerate(selected, start=1)]
    assert sum(count_tokens(block) for block in rendered) <= budget


def test_budget_counts_rendered_passages() -> None:
    chunks = [make_chunk(rank, content="alpha beta gamma " * 30) for rank in range(1, 7)]
    prompt = build_prompt("q", chunks, token_budget=300)
    assert 0 < prompt.passage_tokens <= 300
    assert len(prompt.passages) < 6


def test_top_passage_is_truncated_when_it_alone_exceeds_the_budget() -> None:
    top = make_chunk(1, content="lengthy " * 2000)
    second = make_chunk(2, content="lengthy " * 2000)

    prompt = build_prompt("q", [top, second], token_budget=200)

    assert len(prompt.passages) == 1
    kept = prompt.passages[0]
    assert kept.chunk_id == top.chunk_id
    assert kept.content.startswith("Sample Policy 1 > Section 1\n\nlengthy")
    assert kept.content.endswith(" ...")
    assert kept.token_count == count_tokens(kept.content)
    assert 150 < prompt.passage_tokens <= 200


def test_no_chunks_gives_no_passages() -> None:
    prompt = build_prompt("q", [])
    assert prompt.passages == ()
    assert prompt.passage_tokens == 0


@pytest.mark.parametrize("kwargs", [{"max_passages": 0}, {"token_budget": 0}])
def test_invalid_limits_are_rejected(kwargs) -> None:
    with pytest.raises(ValueError):
        select_passages([make_chunk(1)], **kwargs)
