"""Answer-evaluation judge: prompts, structured-output requests, parsing, and repair retries."""

import json

import pytest
from fakes import FakeChat, make_chunk

from campus_rag.evaluate import judge as jd
from campus_rag.generate.chat import ChatError
from campus_rag.generate.models import Answer, Citation

JUDGE_MODEL = "llama3.1:8b"

FAITHFUL_REPLY = json.dumps(
    {
        "claims": [
            {
                "claim": "Rule one applies.",
                "passages": [1],
                "reason": "P1.",
                "verdict": "supported",
            },
            {
                "claim": "Rule three applies.",
                "passages": [3],
                "reason": "P3.",
                "verdict": "supported",
            },
            {
                "claim": "It costs $5.",
                "passages": [],
                "reason": "Absent.",
                "verdict": "unsupported",
            },
        ]
    }
)
RELEVANCE_REPLY = json.dumps({"reason": "Answers the question.", "score": 4})


def make_answer(cited: tuple[int, ...] = (1, 3), passages: int = 3) -> Answer:
    chunks = tuple(make_chunk(rank) for rank in range(1, passages + 1))
    return Answer(
        question="Which rules apply?",
        search_query="Which rules apply?",
        text="Rule one applies [1]. Rule three applies [3]. It costs $5 [3].",
        refused=False,
        citations=tuple(
            Citation(n, chunks[n - 1].chunk_id, chunks[n - 1].url, "t", chunks[n - 1].heading_path)
            for n in cited
        ),
        passages=chunks,
        model="qwen2.5:7b-instruct",
    )


# --- prompts ------------------------------------------------------------------------------------


def test_faithfulness_prompt_holds_only_the_cited_passages_with_their_answer_numbers() -> None:
    answer = make_answer(cited=(3, 1))
    passages = jd.cited_passages(answer)
    assert [number for number, _ in passages] == [1, 3]
    messages = jd.faithfulness_messages(answer.text, passages)
    assert messages[0] == {"role": "system", "content": jd.FAITHFULNESS_SYSTEM_PROMPT}
    user = messages[1]["content"]
    assert "[1] Sample Policy 1 > Section 1" in user and "[3] Sample Policy 3 > Section 3" in user
    assert user.startswith("Passages, numbered as the answer cites them (1, 3):")
    assert "Sample Policy 2" not in user and "[2]" not in user
    assert user.endswith(f"Answer:\n{answer.text}")


def test_relevance_prompt_has_the_question_reference_and_earlier_question() -> None:
    messages = jd.relevance_messages(
        "What about graduate students?", "Yes [1].", "Graduate rules differ.", earlier_question="Q0"
    )
    assert messages[0]["content"] == jd.RELEVANCE_SYSTEM_PROMPT
    user = messages[1]["content"]
    assert user.startswith("Earlier question in the conversation:\nQ0\n\nQuestion:\nWhat about")
    assert "Reference answer:\nGraduate rules differ." in user
    assert user.endswith("Answer to rate:\nYes [1].")
    plain = jd.relevance_messages("Q?", "A [1].", "R.")[1]["content"]
    assert plain.startswith("Question:\nQ?")


# --- parsing ------------------------------------------------------------------------------------


def test_parse_faithfulness_scores_supported_over_total() -> None:
    result = jd.parse_faithfulness(FAITHFUL_REPLY)
    assert [claim.supported for claim in result.claims] == [True, True, False]
    assert result.claims[1].passages == (3,)
    assert result.score == pytest.approx(2 / 3)


def test_unshown_support_counts_supported_claims_naming_no_shown_passage() -> None:
    result = jd.parse_faithfulness(FAITHFUL_REPLY)
    assert result.unshown_support({1, 3}) == 0
    assert result.unshown_support({3}) == 1  # "Rule one" names only passage 1
    assert result.unshown_support(set()) == 2  # unsupported claims never count
    misread = jd.parse_faithfulness(
        '{"claims": [{"claim": "x", "passages": [], "reason": "", "verdict": "supported"}]}'
    )
    assert misread.unshown_support({1}) == 1


def test_parse_tolerates_a_code_fence() -> None:
    assert jd.parse_relevance(f"```json\n{RELEVANCE_REPLY}\n```").score == 4


@pytest.mark.parametrize(
    "reply, problem",
    [
        ("", "empty"),
        ("The answer is faithful.", "not valid JSON"),
        ("[1, 2]", "not a JSON object"),
        ('{"claims": []}', "non-empty list"),
        ('{"claims": [{"claim": "x", "passages": [1], "verdict": "maybe"}]}', "verdict"),
        ('{"claims": [{"claim": "", "passages": [1], "verdict": "supported"}]}', "claim"),
        ('{"claims": [{"claim": "x", "passages": ["1"], "verdict": "supported"}]}', "integers"),
    ],
)
def test_parse_faithfulness_rejects_malformed_replies(reply: str, problem: str) -> None:
    with pytest.raises(jd.JudgeReplyError, match=problem):
        jd.parse_faithfulness(reply)


def test_parse_relevance_accepts_scores_one_to_five_only() -> None:
    assert jd.parse_relevance('{"reason": "ok", "score": 5.0}') == jd.Relevance(5, "ok")
    assert jd.parse_relevance('{"score": 1}') == jd.Relevance(1, "")
    for bad in ('{"score": 6}', '{"score": 0}', '{"score": true}', '{"score": "5"}', "{}"):
        with pytest.raises(jd.JudgeReplyError, match="score"):
            jd.parse_relevance(bad)


# --- judging ------------------------------------------------------------------------------------


def test_faithfulness_request_uses_the_judge_model_temperature_zero_and_a_json_schema() -> None:
    chat = FakeChat([FAITHFUL_REPLY])
    result = jd.Judge(chat, JUDGE_MODEL).faithfulness(make_answer())
    assert result.error is None and result.attempts == 1
    assert result.value is not None and result.value.score == pytest.approx(2 / 3)
    (call,) = chat.calls
    assert call["model"] == JUDGE_MODEL and call["temperature"] == 0.0
    response_format = call["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["schema"] == jd.FAITHFULNESS_SCHEMA
    assert result.replies == (FAITHFUL_REPLY,)


def test_an_invalid_reply_gets_one_repair_retry() -> None:
    chat = FakeChat(["not json", RELEVANCE_REPLY])
    result = jd.Judge(chat, JUDGE_MODEL).relevance("Q?", "A [1].", "R.")
    assert result.value == jd.Relevance(4, "Answers the question.")
    assert result.error is None and result.attempts == 2
    retry = chat.calls[1]["messages"]
    assert retry[:2] == chat.calls[0]["messages"]
    assert retry[2] == {"role": "assistant", "content": "not json"}
    assert retry[3]["role"] == "user" and "not valid JSON" in retry[3]["content"]
    assert chat.calls[1]["response_format"]["json_schema"]["schema"] == jd.RELEVANCE_SCHEMA


def test_two_invalid_replies_become_a_recorded_error_not_an_exception() -> None:
    chat = FakeChat(['{"score": 9}', "still wrong"])
    result = jd.Judge(chat, JUDGE_MODEL).relevance("Q?", "A [1].", "R.")
    assert result.value is None and result.attempts == 2
    assert result.error is not None and "after a retry" in result.error
    assert result.replies == ('{"score": 9}', "still wrong")


def test_a_chat_failure_becomes_a_recorded_error() -> None:
    chat = FakeChat([ChatError("timed out")])
    result = jd.Judge(chat, JUDGE_MODEL).faithfulness(make_answer())
    assert result.value is None and result.attempts == 1
    assert result.error == "judge request failed: timed out"


def test_judge_time_is_measured_with_the_clock() -> None:
    ticks = iter([10.0, 12.5])
    judge = jd.Judge(FakeChat([RELEVANCE_REPLY]), JUDGE_MODEL, clock=lambda: next(ticks))
    assert judge.relevance("Q?", "A [1].", "R.").elapsed_ms == 2500.0
