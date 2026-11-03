"""LLM-as-judge for answer evaluation: faithfulness and answer relevance.

The judge model (`llama3.1:8b` by default) differs from the answering model's family, so it does
not grade its own family's output. Each request uses temperature 0 and a JSON-schema
`response_format`. Code validates every reply: an invalid reply gets one retry with a repair
instruction, and a second failure is returned as an error rather than raised, so one bad reply never
stops an evaluation run.
"""

import json
import re
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from campus_rag.generate.chat import ChatClient, ChatError, Message
from campus_rag.generate.models import Answer
from campus_rag.generate.prompt import render_passage
from campus_rag.retrieve.models import RetrievedChunk

FAITHFULNESS_MAX_TOKENS = 1200
RELEVANCE_MAX_TOKENS = 300
RELEVANCE_SCORES = (1, 2, 3, 4, 5)
SUPPORTED = "supported"
UNSUPPORTED = "unsupported"

FAITHFULNESS_SYSTEM_PROMPT = """\
You check whether an answer is supported by the numbered source passages it cites.

1. Split the answer into atomic claims. An atomic claim is one short, self-contained statement of \
fact. Write each claim in plain words, without citation markers such as [1]. Split a sentence \
that states two facts into two claims. Skip text that states no fact.
2. For each claim, list the numbers of the passages that state it.
3. Give a one-sentence reason, then the verdict:
   - "supported": a listed passage states the claim, or the claim follows directly from what a \
passage states.
   - "unsupported": no passage states it. Use this when a claim adds a detail, number, condition, \
or exception that the passages do not contain, even if it sounds plausible. Use an empty \
passage list.

Judge only against the passages. Background knowledge does not count as support.
Reply with only a JSON object:
{"claims": [{"claim": "...", "passages": [1], "reason": "...", "verdict": "supported"}]}"""

RELEVANCE_SYSTEM_PROMPT = """\
You rate how well an answer responds to a question about university policy.
A reference answer shows what a good answer covers. The answer may use different words, add \
correct detail, or be organized differently. Do not check whether the answer's facts are true; \
another check does that. Rate only whether it answers what was asked.

5 = directly and completely answers the question.
4 = answers the question but misses a minor point the reference covers.
3 = partly answers: on topic, but misses the main point or only hedges.
2 = mostly beside the point: a related topic that does not answer what was asked.
1 = off-topic, or does not answer at all.

Give a one-sentence reason, then the score.
Reply with only a JSON object: {"reason": "...", "score": 5}"""

REPAIR_INSTRUCTION = (
    "Your reply could not be used: {problem}. Reply again with only a JSON object that matches "
    "the required format, with no other text."
)

FAITHFULNESS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim": {"type": "string"},
                    "passages": {"type": "array", "items": {"type": "integer"}},
                    "reason": {"type": "string"},
                    "verdict": {"type": "string", "enum": [SUPPORTED, UNSUPPORTED]},
                },
                "required": ["claim", "passages", "reason", "verdict"],
            },
        }
    },
    "required": ["claims"],
}

RELEVANCE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reason": {"type": "string"},
        "score": {"type": "integer", "enum": list(RELEVANCE_SCORES)},
    },
    "required": ["reason", "score"],
}

_CODE_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL | re.IGNORECASE)


class JudgeReplyError(ValueError):
    """The judge's reply is not valid JSON or does not match the expected shape."""


@dataclass(frozen=True)
class ClaimVerdict:
    claim: str
    supported: bool
    passages: tuple[int, ...]  # passage numbers the judge named as support
    reason: str


@dataclass(frozen=True)
class Faithfulness:
    claims: tuple[ClaimVerdict, ...]  # never empty

    @property
    def score(self) -> float:
        """Share of claims the cited passages support."""
        return sum(1 for claim in self.claims if claim.supported) / len(self.claims)

    def unshown_support(self, shown: Collection[int]) -> int:
        """Supported claims that name no passage the judge was shown, a sign of a misread.

        The verdict stands, because a judge sometimes mislabels the passage it read; the count
        lets a reader check those verdicts by hand.
        """
        return sum(
            1
            for claim in self.claims
            if claim.supported and not any(number in shown for number in claim.passages)
        )


@dataclass(frozen=True)
class Relevance:
    score: int  # 1-5
    reason: str


@dataclass(frozen=True)
class JudgeResult[T]:
    value: T | None  # None when the judge failed
    error: str | None  # why the judge failed; None on success
    attempts: int  # chat requests made: 1, or 2 after a repair retry
    elapsed_ms: float
    replies: tuple[str, ...]  # raw judge replies, for the raw results file


# --- prompts ----------------------------------------------------------------------------------


def cited_passages(answer: Answer) -> list[tuple[int, RetrievedChunk]]:
    """The passages the answer cites, with the numbers the answer uses, in number order."""
    numbers = sorted({citation.number for citation in answer.citations})
    return [(number, answer.passages[number - 1]) for number in numbers]


def faithfulness_messages(
    answer_text: str, passages: Sequence[tuple[int, RetrievedChunk]]
) -> list[Message]:
    """Messages asking the judge to split `answer_text` into claims and check each one."""
    blocks = "\n\n".join(render_passage(number, chunk) for number, chunk in passages)
    numbers = ", ".join(str(number) for number, _ in passages)
    user = (
        f"Passages, numbered as the answer cites them ({numbers}):\n\n{blocks}\n\n"
        f"Answer:\n{answer_text.strip()}"
    )
    return [
        {"role": "system", "content": FAITHFULNESS_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def relevance_messages(
    question: str,
    answer_text: str,
    reference_answer: str,
    *,
    earlier_question: str | None = None,
) -> list[Message]:
    """Messages asking the judge to rate how well the answer responds to the question.

    A follow-up such as "What about graduate students?" needs `earlier_question` to make sense.
    """
    context = (
        f"Earlier question in the conversation:\n{earlier_question.strip()}\n\n"
        if earlier_question
        else ""
    )
    user = (
        f"{context}Question:\n{question.strip()}\n\n"
        f"Reference answer:\n{reference_answer.strip()}\n\n"
        f"Answer to rate:\n{answer_text.strip()}"
    )
    return [
        {"role": "system", "content": RELEVANCE_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def json_schema_format(name: str, schema: Mapping[str, Any]) -> dict[str, Any]:
    """An OpenAI-style structured-output `response_format`."""
    return {"type": "json_schema", "json_schema": {"name": name, "schema": schema, "strict": True}}


# --- parsing ----------------------------------------------------------------------------------


def load_json_object(text: str) -> dict[str, Any]:
    """Parse a reply as one JSON object, tolerating a surrounding Markdown code fence."""
    stripped = text.strip()
    fenced = _CODE_FENCE.match(stripped)
    if fenced:
        stripped = fenced.group(1)
    if not stripped:
        raise JudgeReplyError("the reply is empty")
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise JudgeReplyError(f"the reply is not valid JSON ({exc.msg})") from None
    if not isinstance(value, dict):
        raise JudgeReplyError("the reply is not a JSON object")
    return value


def parse_faithfulness(text: str) -> Faithfulness:
    """Validate a faithfulness reply. Raises `JudgeReplyError`."""
    claims = load_json_object(text).get("claims")
    if not isinstance(claims, list) or not claims:
        raise JudgeReplyError('"claims" must be a non-empty list')
    verdicts = []
    for index, item in enumerate(claims):
        where = f"claims[{index}]"
        if not isinstance(item, dict):
            raise JudgeReplyError(f"{where} must be an object")
        claim = item.get("claim")
        if not isinstance(claim, str) or not claim.strip():
            raise JudgeReplyError(f'{where}."claim" must be a non-empty string')
        verdict = item.get("verdict")
        if verdict not in (SUPPORTED, UNSUPPORTED):
            raise JudgeReplyError(f'{where}."verdict" must be "{SUPPORTED}" or "{UNSUPPORTED}"')
        passages = item.get("passages", [])
        if not isinstance(passages, list) or not all(
            isinstance(number, int) and not isinstance(number, bool) for number in passages
        ):
            raise JudgeReplyError(f'{where}."passages" must be a list of integers')
        reason = item.get("reason", "")
        verdicts.append(
            ClaimVerdict(
                claim=claim.strip(),
                supported=verdict == SUPPORTED,
                passages=tuple(passages),
                reason=reason.strip() if isinstance(reason, str) else "",
            )
        )
    return Faithfulness(tuple(verdicts))


def parse_relevance(text: str) -> Relevance:
    """Validate a relevance reply. Raises `JudgeReplyError`."""
    value = load_json_object(text)
    score = value.get("score")
    if isinstance(score, float) and score.is_integer():
        score = int(score)
    if isinstance(score, bool) or score not in RELEVANCE_SCORES:
        raise JudgeReplyError('"score" must be an integer from 1 to 5')
    reason = value.get("reason", "")
    return Relevance(score=score, reason=reason.strip() if isinstance(reason, str) else "")


# --- judging ----------------------------------------------------------------------------------


class Judge:
    """Grades answers with a chat model. Methods return a `JudgeResult` and never raise."""

    def __init__(
        self, chat: ChatClient, model: str, *, clock: Callable[[], float] = time.perf_counter
    ) -> None:
        self._chat = chat
        self.model = model
        self._clock = clock

    def faithfulness(self, answer: Answer) -> JudgeResult[Faithfulness]:
        """Split the answer into claims and check each against the passages it cites."""
        messages = faithfulness_messages(answer.text, cited_passages(answer))
        return self._ask(
            messages,
            json_schema_format("faithfulness", FAITHFULNESS_SCHEMA),
            FAITHFULNESS_MAX_TOKENS,
            parse_faithfulness,
        )

    def relevance(
        self,
        question: str,
        answer_text: str,
        reference_answer: str,
        *,
        earlier_question: str | None = None,
    ) -> JudgeResult[Relevance]:
        """Rate from 1 to 5 how directly and completely the answer responds to the question."""
        return self._ask(
            relevance_messages(
                question, answer_text, reference_answer, earlier_question=earlier_question
            ),
            json_schema_format("relevance", RELEVANCE_SCHEMA),
            RELEVANCE_MAX_TOKENS,
            parse_relevance,
        )

    def _ask[T](
        self,
        messages: list[Message],
        response_format: Mapping[str, Any],
        max_tokens: int,
        parse: Callable[[str], T],
    ) -> JudgeResult[T]:
        started = self._clock()
        replies: list[str] = []
        conversation = list(messages)
        problem = ""
        for attempt in (1, 2):
            try:
                reply = self._chat.complete(
                    conversation,
                    model=self.model,
                    temperature=0.0,
                    max_tokens=max_tokens,
                    response_format=response_format,
                ).text
            except ChatError as exc:
                return self._result(None, f"judge request failed: {exc}", attempt, started, replies)
            replies.append(reply)
            try:
                return self._result(parse(reply), None, attempt, started, replies)
            except JudgeReplyError as exc:
                problem = str(exc)
            conversation += [
                {"role": "assistant", "content": reply},
                {"role": "user", "content": REPAIR_INSTRUCTION.format(problem=problem)},
            ]
        return self._result(
            None, f"invalid judge reply after a retry: {problem}", 2, started, replies
        )

    def _result[T](
        self,
        value: T | None,
        error: str | None,
        attempts: int,
        started: float,
        replies: list[str],
    ) -> JudgeResult[T]:
        elapsed_ms = round((self._clock() - started) * 1000, 1)
        return JudgeResult(value, error, attempts, elapsed_ms, tuple(replies))
