"""Tests for AnswerService with a fake searcher and a fake chat model."""

import itertools

import pytest
from fakes import FakeChat, FakeSearcher, make_chunk

from campus_rag.config import ChatSettings
from campus_rag.generate.answer import ANSWER_MAX_TOKENS, AnswerService
from campus_rag.generate.chat import ChatError
from campus_rag.generate.models import REFUSAL_TEXT, ChatTurn
from campus_rag.retrieve.models import SearchFilters

SETTINGS = ChatSettings(answer_model="answerer", rewrite_model="rewriter")


def _service(searcher, chat, **kwargs) -> AnswerService:
    ticks = itertools.count(0.0, 0.25)  # each clock read advances 250 ms
    return AnswerService(searcher, chat, SETTINGS, clock=lambda: next(ticks), **kwargs)


def test_answer_cites_supplied_passages() -> None:
    searcher = FakeSearcher([make_chunk(rank) for rank in range(1, 9)])
    chat = FakeChat(["Rule two applies [2]. Rule one also applies [1, 2]."])

    answer = _service(searcher, chat).answer("  Which rules apply?  ")

    assert answer.question == answer.search_query == "Which rules apply?"
    assert not answer.refused
    assert answer.text == "Rule two applies [2]. Rule one also applies [1][2]."
    assert [c.number for c in answer.citations] == [2, 1]
    assert [c.chunk_id for c in answer.citations] == [102, 101]
    assert len(answer.passages) == 6
    assert answer.model == "answerer"
    assert answer.invalid_citations == ()
    assert answer.timings_ms == {"retrieve": 250.0, "generate": 250.0}

    assert searcher.calls == [
        {"query": "Which rules apply?", "mode": "hybrid", "k": 8, "filters": None}
    ]
    call = chat.calls[0]
    assert call["model"] == "answerer"
    assert call["temperature"] == 0.0
    assert call["max_tokens"] == ANSWER_MAX_TOKENS
    assert call["response_format"] is None
    assert call["messages"][1]["content"].endswith("Question: Which rules apply?")


def test_search_options_are_passed_to_the_searcher() -> None:
    searcher = FakeSearcher([make_chunk(1)])
    filters = SearchFilters(sources=("uw-policy",))
    service = _service(
        searcher, FakeChat(["Yes [1]."]), k=3, search_mode="keyword", filters=filters
    )
    service.answer("q")
    assert searcher.calls == [{"query": "q", "mode": "keyword", "k": 3, "filters": filters}]


def test_max_passages_and_budget_limit_the_prompt() -> None:
    searcher = FakeSearcher([make_chunk(rank) for rank in range(1, 9)])
    answer = _service(searcher, FakeChat(["Yes [1]."]), max_passages=2).answer("q")
    assert len(answer.passages) == 2


def test_model_refusal_is_returned_exactly() -> None:
    searcher = FakeSearcher([make_chunk(1)])
    answer = _service(searcher, FakeChat(["i don't know based on the available policies"])).answer(
        "Unrelated?"
    )
    assert answer.refused
    assert answer.text == REFUSAL_TEXT
    assert answer.citations == ()
    assert len(answer.passages) == 1


def test_invalid_citations_are_removed_and_recorded() -> None:
    searcher = FakeSearcher([make_chunk(1), make_chunk(2)])
    answer = _service(searcher, FakeChat(["Rule one applies [1][7]. Rule nine [9]."])).answer("q")
    assert not answer.refused
    assert answer.text == "Rule one applies [1]. Rule nine."
    assert answer.invalid_citations == (7, 9)
    assert [c.number for c in answer.citations] == [1]


def test_uncited_answer_is_refused() -> None:
    searcher = FakeSearcher([make_chunk(1)])
    answer = _service(searcher, FakeChat(["Rules apply everywhere."])).answer("q")
    assert answer.refused
    assert answer.text == REFUSAL_TEXT


def test_no_retrieved_passages_refuses_without_calling_the_model() -> None:
    chat = FakeChat()
    answer = _service(FakeSearcher([]), chat).answer("q")
    assert answer.refused
    assert answer.text == REFUSAL_TEXT
    assert answer.passages == ()
    assert chat.calls == []
    assert answer.timings_ms["generate"] == 0.0


def test_history_triggers_a_rewrite_used_for_search_and_prompt() -> None:
    searcher = FakeSearcher([make_chunk(1)])
    chat = FakeChat(["How much sick leave do faculty earn?", "Faculty earn some [1]."])
    history = (
        ChatTurn("user", "How much sick leave do staff earn?"),
        ChatTurn("assistant", "Eight hours [1]."),
    )

    answer = _service(searcher, chat).answer("And faculty?", history)

    assert answer.question == "And faculty?"
    assert answer.search_query == "How much sick leave do faculty earn?"
    assert searcher.calls[0]["query"] == "How much sick leave do faculty earn?"
    assert [call["model"] for call in chat.calls] == ["rewriter", "answerer"]
    assert chat.calls[1]["messages"][1]["content"].endswith(
        "Question: How much sick leave do faculty earn?"
    )
    assert set(answer.timings_ms) == {"rewrite", "retrieve", "generate"}


def test_failed_rewrite_falls_back_to_the_question() -> None:
    searcher = FakeSearcher([make_chunk(1)])
    chat = FakeChat([ChatError("rewriter missing"), "Answer [1]."])
    answer = _service(searcher, chat).answer("And faculty?", (ChatTurn("user", "earlier"),))
    assert answer.search_query == "And faculty?"
    assert not answer.refused


def test_answer_model_failure_propagates() -> None:
    searcher = FakeSearcher([make_chunk(1)])
    with pytest.raises(ChatError):
        _service(searcher, FakeChat([ChatError("down")])).answer("q")


def test_blank_question_is_rejected() -> None:
    with pytest.raises(ValueError):
        _service(FakeSearcher(), FakeChat()).answer("   ")
