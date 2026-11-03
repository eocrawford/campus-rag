"""Tests for follow-up query rewriting and its fallbacks."""

import pytest
from fakes import FakeChat

from campus_rag.generate.chat import ChatError
from campus_rag.generate.models import ChatTurn
from campus_rag.generate.rewrite import rewrite_query

HISTORY = (
    ChatTurn("user", "How much sick leave do staff earn?"),
    ChatTurn("assistant", "Staff earn eight hours per month [1][2]."),
)


def test_rewrite_returns_the_cleaned_standalone_query() -> None:
    chat = FakeChat(['Standalone question: "How much sick leave do faculty earn?"\n'])
    query = rewrite_query(chat, "rewriter", HISTORY, "And faculty?")

    assert query == "How much sick leave do faculty earn?"
    call = chat.calls[0]
    assert call["model"] == "rewriter"
    assert call["temperature"] == 0.0
    assert call["messages"][1]["content"] == (
        "Earlier question: How much sick leave do staff earn?\n"
        "Follow-up question: And faculty?\n\n"
        "Standalone question:"
    )


def test_no_history_returns_the_question_without_calling_the_model() -> None:
    chat = FakeChat()
    assert rewrite_query(chat, "rewriter", (), "What is FMLA?") == "What is FMLA?"
    assert rewrite_query(chat, "rewriter", (ChatTurn("assistant", "Hi"),), "Q?") == "Q?"
    assert chat.calls == []


def test_only_recent_user_questions_are_sent_and_long_ones_are_shortened() -> None:
    history = []
    for i in range(5):
        history += [ChatTurn("user", f"question {i}"), ChatTurn("assistant", f"answer {i}")]
    history.append(ChatTurn("user", "y" * 1000))
    chat = FakeChat(["Rewritten question?"])
    rewrite_query(chat, "rewriter", history, "follow up")
    user = chat.calls[0]["messages"][1]["content"]
    assert "question 2" not in user and "question 3" in user and "question 4" in user
    assert "answer" not in user
    assert "y" * 400 + " ..." in user and "y" * 401 not in user


@pytest.mark.parametrize(
    "reply",
    [
        "",
        "   \n  ",
        "Standalone question:",
        "Q" * 301,
    ],
)
def test_empty_or_overlong_replies_fall_back_to_the_question(reply: str) -> None:
    question = "What about the deadline for appeals in the graduate school?"
    assert rewrite_query(FakeChat([reply]), "m", HISTORY, question) == question


def test_reply_over_three_times_a_long_question_falls_back() -> None:
    question = "Q" * 60  # 3x = 180 characters
    assert rewrite_query(FakeChat(["a" * 181]), "m", HISTORY, question) == question
    assert rewrite_query(FakeChat(["a" * 180]), "m", HISTORY, question) == "a" * 180


def test_reply_over_three_hundred_characters_always_falls_back() -> None:
    question = "Q" * 200
    assert rewrite_query(FakeChat(["a" * 301]), "m", HISTORY, question) == question
    assert rewrite_query(FakeChat(["a" * 300]), "m", HISTORY, question) == "a" * 300


def test_short_follow_ups_may_expand_to_one_hundred_characters() -> None:
    rewritten = "How many hours of sick leave do faculty members earn each month at the university?"
    assert len(rewritten) < 100
    assert rewrite_query(FakeChat([rewritten]), "m", HISTORY, "And faculty?") == rewritten
    assert rewrite_query(FakeChat(["a" * 101]), "m", HISTORY, "And faculty?") == "And faculty?"


def test_chat_error_falls_back_to_the_question() -> None:
    chat = FakeChat([ChatError("server down")])
    assert rewrite_query(chat, "m", HISTORY, "And faculty?") == "And faculty?"
