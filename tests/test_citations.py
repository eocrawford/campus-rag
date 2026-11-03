"""Tests for citation parsing, validation, and refusal enforcement."""

import pytest
from fakes import make_chunk

from campus_rag.generate.citations import (
    check_answer,
    is_refusal,
    normalize_citations,
    parse_marker_run,
)
from campus_rag.generate.models import REFUSAL_TEXT, Citation

PASSAGES = tuple(make_chunk(rank) for rank in range(1, 5))


@pytest.mark.parametrize(
    ("marker", "numbers"),
    [
        ("[1]", [1]),
        ("[ 2 ]", [2]),
        ("[1, 3]", [1, 3]),
        ("[1,3,4]", [1, 3, 4]),
        ("[1][2]", [1, 2]),
        ("[1] [2]", [1, 2]),
        ("[2-3]", [2, 3]),
        ("[2 - 4]", [2, 3, 4]),
        ("[2\u20133]", [2, 3]),
        ("[1, 3-4]", [1, 3, 4]),
        ("[4-2]", [4, 2]),
        ("[1-500]", [1, 500]),
    ],
)
def test_parse_marker_forms(marker: str, numbers: list[int]) -> None:
    assert parse_marker_run(marker) == numbers


def test_valid_markers_are_normalized_and_cited_in_first_use_order() -> None:
    text, used, invalid = normalize_citations("Rule B applies [3, 1]. Rule A applies [1][2].", 4)
    assert text == "Rule B applies [3][1]. Rule A applies [1][2]."
    assert used == (3, 1, 2)
    assert invalid == ()


def test_ranges_and_duplicates_are_expanded_once() -> None:
    text, used, _ = normalize_citations("Both apply [2-3][3].", 4)
    assert text == "Both apply [2][3]."
    assert used == (2, 3)


def test_invalid_numbers_are_removed_and_recorded() -> None:
    text, used, invalid = normalize_citations("One [1, 7]. Two [9]. Three [0][2].", 4)
    assert text == "One [1]. Two. Three [2]."
    assert used == (1, 2)
    assert invalid == (7, 9, 0)


def test_spacing_is_normalized() -> None:
    text, _, _ = normalize_citations(
        "Applies[1] , and  also [8] here [2] .\n\n\n\nNext  line [3]  ", 4
    )
    assert text == "Applies [1], and also here [2].\n\nNext line [3]"


def test_non_citation_brackets_are_left_alone() -> None:
    text, used, _ = normalize_citations("See section [a] and note [1].", 4)
    assert text == "See section [a] and note [1]."
    assert used == (1,)


def test_check_answer_builds_citations_from_supplied_passages() -> None:
    checked = check_answer("Leave needs approval [2]. Pay continues [1][2].", PASSAGES)
    assert not checked.refused
    assert checked.text == "Leave needs approval [2]. Pay continues [1][2]."
    assert checked.citations == (
        Citation(2, 102, PASSAGES[1].url, PASSAGES[1].title, PASSAGES[1].heading_path),
        Citation(1, 101, PASSAGES[0].url, PASSAGES[0].title, PASSAGES[0].heading_path),
    )
    assert checked.invalid_citations == ()


def test_check_answer_removes_invalid_citations_but_keeps_valid_ones() -> None:
    checked = check_answer("Leave needs approval [2][6].", PASSAGES)
    assert not checked.refused
    assert checked.text == "Leave needs approval [2]."
    assert [c.number for c in checked.citations] == [2]
    assert checked.invalid_citations == (6,)


@pytest.mark.parametrize(
    "reply",
    [
        REFUSAL_TEXT,
        "  i DON'T know based on the   available policies  ",
        "I don\u2019t know based on the available policies.",
        f'"{REFUSAL_TEXT}"',
        f"{REFUSAL_TEXT} The passages cover a different topic [1].",
    ],
)
def test_refusal_replies_become_the_exact_refusal(reply: str) -> None:
    assert is_refusal(reply)
    checked = check_answer(reply, PASSAGES)
    assert checked.refused
    assert checked.text == REFUSAL_TEXT
    assert checked.citations == ()


def test_uncited_answer_becomes_a_refusal() -> None:
    checked = check_answer("Leave always needs approval.", PASSAGES)
    assert checked.refused
    assert checked.text == REFUSAL_TEXT


def test_answer_with_only_invalid_citations_becomes_a_refusal_and_records_them() -> None:
    checked = check_answer("Leave always needs approval [5][7].", PASSAGES)
    assert checked.refused
    assert checked.text == REFUSAL_TEXT
    assert checked.invalid_citations == (5, 7)


def test_empty_reply_is_a_refusal() -> None:
    assert check_answer("   ", PASSAGES).refused


def test_any_citation_is_invalid_without_passages() -> None:
    checked = check_answer("Something [1].", ())
    assert checked.refused
    assert checked.invalid_citations == (1,)


def test_mentioning_not_knowing_later_is_not_a_refusal() -> None:
    assert not is_refusal("Leave needs approval [1]. I don't know the deadline.")
