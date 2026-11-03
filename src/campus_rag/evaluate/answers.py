"""Answer evaluation: answer every golden question, then score refusals, citations, retrieval, and
judge-graded faithfulness and relevance.

`evaluate_questions` answers the questions through an `AnswerService`, computes the deterministic
metrics, asks the judge, and saves the raw results after every question, so an interrupted run
loses nothing and `--resume` continues it. The metric and report functions are pure and work on
`QuestionRecord`s, the rows of the raw results file.
"""

import argparse
import json
import os
import re
import statistics
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import psycopg

from campus_rag.config import find_repo_root, load_settings
from campus_rag.evaluate.golden import (
    GoldenQuestion,
    GoldenSetError,
    first_relevant_rank,
    load_golden_set,
)
from campus_rag.evaluate.judge import Judge
from campus_rag.evaluate.retrieval import _table, corpus_counts, git_commit_label, percentile
from campus_rag.generate.answer import AnswerService
from campus_rag.generate.chat import ChatError, OpenAICompatibleChat
from campus_rag.generate.models import Answer, ChatTurn
from campus_rag.generate.prompt import select_passages
from campus_rag.index.embed import EmbeddingError, OpenAICompatibleEmbedder
from campus_rag.index.migrate import _first_line
from campus_rag.retrieve.connect import SetupError, open_connection
from campus_rag.retrieve.models import Searcher, SearchMode
from campus_rag.retrieve.search import PolicySearcher

SEARCH_MODE: SearchMode = "hybrid"
SEARCH_K = 8  # chunks retrieved per answer, AnswerService's default
UNANSWERABLE = "unanswerable"
LOW_RELEVANCE = 2  # relevance scores at or below this count as a failure
QUOTE_CHARS = 120  # longest quote of our model's answer or claim in the report

EXIT_USAGE = 1
EXIT_GOLDEN_SET_UNAVAILABLE = 5
EXIT_DATABASE_ERROR = 6
EXIT_MODEL_UNAVAILABLE = 7

GROUP_ORDER = ("all", "paraphrase", "exact_term", "unanswerable", "follow-up")


class AnswerProvider(Protocol):
    def answer(self, question: str, history: Sequence[ChatTurn] = ()) -> Answer: ...


class ResumeError(Exception):
    """The --resume file is missing, malformed, or from a run with different models."""


# --- per-question record ------------------------------------------------------------------------


@dataclass(frozen=True)
class QuestionRecord:
    """One question's outcome; a row of the raw results file."""

    id: str
    type: str
    question: str
    answer: str  # final answer text with [n] markers, or the refusal sentence
    refused: bool
    search_query: str  # the rewritten query for follow-ups
    follow_up_of: str | None = None
    history_has_answer: bool | None = None  # follow-ups: the earlier answer was in the history
    cited: list[int] = field(default_factory=list)  # valid cited passage numbers
    invalid_citations: list[int] = field(default_factory=list)  # cited but not supplied
    sentences: int = 0  # answer sentences (0 for refusals)
    cited_sentences: int = 0  # answer sentences with at least one [n]
    passages: list[dict[str, Any]] = field(default_factory=list)  # given to the model, numbered
    retrieval_rank: int | None = None  # first passage matching a target; None if absent
    raw_query_rank: int | None = None  # follow-ups: the same, searching the follow-up as written
    timings_ms: dict[str, float] = field(default_factory=dict)  # rewrite, retrieve, generate
    faithfulness: float | None = None  # supported claims / claims; None when not judged
    claims: list[dict[str, Any]] = field(default_factory=list)  # the judge's claim verdicts
    unshown_support: int = 0  # supported claims naming no passage the judge was shown
    relevance: int | None = None  # 1-5; None when not judged
    relevance_reason: str = ""
    judge_errors: list[str] = field(default_factory=list)
    judge_attempts: int = 0  # judge requests, including repair retries
    judge_ms: float = 0.0
    judge_replies: list[str] = field(default_factory=list)  # raw replies, kept on a judge error

    @property
    def answerable(self) -> bool:
        return self.type != UNANSWERABLE

    @property
    def follow_up(self) -> bool:
        return self.follow_up_of is not None

    @property
    def correct_refusal(self) -> bool:
        """Unanswerable questions refused and answerable questions answered."""
        return self.refused != self.answerable

    @property
    def citation_validity(self) -> float | None:
        """Valid cited numbers / all cited numbers; None when the answer cites nothing."""
        total = len(self.cited) + len(self.invalid_citations)
        return len(self.cited) / total if total else None

    @property
    def sentence_coverage(self) -> float | None:
        return self.cited_sentences / self.sentences if self.sentences else None

    @property
    def retrieval_hit(self) -> bool | None:
        """A passage given to the model matched a target; None for unanswerable questions."""
        return self.retrieval_rank is not None if self.answerable else None

    @property
    def raw_query_hit(self) -> bool | None:
        if not (self.answerable and self.follow_up):
            return None
        return self.raw_query_rank is not None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "QuestionRecord":
        names = {f.name for f in fields(cls)}
        unknown = sorted(set(data) - names)
        if unknown:
            raise ValueError(f"unknown field(s): {', '.join(unknown)}")
        return cls(**data)


# --- deterministic metrics ----------------------------------------------------------------------

_BULLET = re.compile(r"^\s*(?:[-*\u2022]|\d+[.)])\s+")
_MARKER = re.compile(r"\[\d+\]")
# A sentence ends at . ! or ? (plus closing quotes and any citation markers that follow), before
# whitespace and a capital letter or opening quote. Initials ("U.S."), "e.g.", "i.e.", "vs.", and
# "No." never end a sentence, and "No. 11" or "2.4" has no capital after the period.
_SENTENCE_END = re.compile(
    r"(?<!\b[A-Z])(?<!\be\.g)(?<!\bi\.e)(?<!\bvs)(?<!\bNo)"
    r"[.!?][\"'\u201d)]*((?:\s*\[\d+\])*)\s+(?=[A-Z\"'\u201c(*])"
)


def split_sentences(text: str) -> list[str]:
    """Split an answer into sentences. Each line, such as a list item, is split on its own.

    Citation markers after a sentence's period belong to that sentence. Lines that introduce a
    list (ending with a colon) and fragments with no letters are not sentences.
    """
    sentences: list[str] = []
    for line in text.splitlines():
        line = _BULLET.sub("", line).strip()
        if not line:
            continue
        start = 0
        pieces = []
        for match in _SENTENCE_END.finditer(line):
            pieces.append(line[start : match.end(1) if match.group(1) else match.start() + 1])
            start = match.end()
        pieces.append(line[start:])
        for piece in (p.strip() for p in pieces):
            words = _MARKER.sub("", piece).strip(" *_")
            if any(ch.isalpha() for ch in words) and not words.endswith(":"):
                sentences.append(piece)
            elif _MARKER.search(piece) and sentences:
                sentences[-1] = f"{sentences[-1]} {piece}"
    return sentences


def sentence_citation_counts(text: str) -> tuple[int, int]:
    """(sentences, sentences with at least one [n] marker)."""
    sentences = split_sentences(text)
    return len(sentences), sum(1 for sentence in sentences if _MARKER.search(sentence))


def target_rank(question: GoldenQuestion, passages: Sequence[Any]) -> int | None:
    """Position of the first passage matching a target; None for unanswerable questions."""
    if question.type == UNANSWERABLE:
        return None
    return first_relevant_rank(question, passages)


# --- running ------------------------------------------------------------------------------------


def follow_up_history(
    parent: GoldenQuestion, parent_record: QuestionRecord | None
) -> tuple[ChatTurn, ...]:
    """The earlier question and, when this run answered it, the answer it produced."""
    turns = [ChatTurn("user", parent.question)]
    if parent_record is not None:
        turns.append(ChatTurn("assistant", parent_record.answer))
    return tuple(turns)


def evaluate_question(
    question: GoldenQuestion,
    *,
    service: AnswerProvider,
    searcher: Searcher,
    judge: Judge,
    parent: GoldenQuestion | None = None,
    parent_record: QuestionRecord | None = None,
) -> QuestionRecord:
    """Answer one question, compute its deterministic metrics, and judge the answer.

    Raises what the answer service raises (`ChatError`, `EmbeddingError`, database errors).
    Judge failures are recorded, never raised.
    """
    history: tuple[ChatTurn, ...] = ()
    if question.follow_up_of is not None:
        if parent is None:
            raise ValueError(
                f"{question.id}: the earlier question {question.follow_up_of} is unknown"
            )
        history = follow_up_history(parent, parent_record)
    answer = service.answer(question.question, history)

    raw_query_rank = None
    if history and question.type != UNANSWERABLE:
        raw = searcher.search(question.question, mode=SEARCH_MODE, k=SEARCH_K)
        raw_query_rank = target_rank(question, select_passages(raw))

    sentences, cited_sentences = (0, 0) if answer.refused else sentence_citation_counts(answer.text)
    judged: dict[str, Any] = {}
    if not answer.refused:
        judged = _judge(judge, question, answer, parent)

    return QuestionRecord(
        id=question.id,
        type=question.type,
        question=question.question,
        answer=answer.text,
        refused=answer.refused,
        search_query=answer.search_query,
        follow_up_of=question.follow_up_of,
        history_has_answer=(parent_record is not None) if history else None,
        cited=[citation.number for citation in answer.citations],
        invalid_citations=list(answer.invalid_citations),
        sentences=sentences,
        cited_sentences=cited_sentences,
        passages=[
            {
                "number": number,
                "chunk_id": chunk.chunk_id,
                "url": chunk.url,
                "heading_path": chunk.heading_path,
            }
            for number, chunk in enumerate(answer.passages, start=1)
        ],
        retrieval_rank=target_rank(question, answer.passages),
        raw_query_rank=raw_query_rank,
        timings_ms=dict(answer.timings_ms),
        **judged,
    )


def _judge(
    judge: Judge, question: GoldenQuestion, answer: Answer, parent: GoldenQuestion | None
) -> dict[str, Any]:
    faithfulness = judge.faithfulness(answer)
    relevance = judge.relevance(
        question.question,
        answer.text,
        question.reference_answer,
        earlier_question=parent.question if parent is not None else None,
    )
    errors = [
        f"{name}: {result.error}"
        for name, result in (("faithfulness", faithfulness), ("relevance", relevance))
        if result.error
    ]
    replies = [*faithfulness.replies, *relevance.replies] if errors else []
    return {
        "faithfulness": faithfulness.value.score if faithfulness.value else None,
        "claims": [asdict(claim) for claim in faithfulness.value.claims]
        if faithfulness.value
        else [],
        "unshown_support": faithfulness.value.unshown_support(
            {citation.number for citation in answer.citations}
        )
        if faithfulness.value
        else 0,
        "relevance": relevance.value.score if relevance.value else None,
        "relevance_reason": relevance.value.reason if relevance.value else "",
        "judge_errors": errors,
        "judge_attempts": faithfulness.attempts + relevance.attempts,
        "judge_ms": round(faithfulness.elapsed_ms + relevance.elapsed_ms, 1),
        "judge_replies": replies,
    }


def _print_flushed(line: str) -> None:
    print(line, flush=True)  # progress shows up promptly even when output is piped to a file


def evaluate_questions(
    questions: Sequence[GoldenQuestion],
    *,
    service: AnswerProvider,
    searcher: Searcher,
    judge: Judge,
    golden: Sequence[GoldenQuestion],
    completed: Mapping[str, QuestionRecord],
    save: Callable[[list[QuestionRecord]], None],
    progress: Callable[[str], None] = _print_flushed,
    clock: Callable[[], float] = time.monotonic,
) -> list[QuestionRecord]:
    """Evaluate `questions` in order, skipping ids in `completed`, saving after each one.

    Returns every record, completed and new, in golden-set order. A follow-up uses the earlier
    question's record from `completed` or from this run for its history.
    """
    by_id = {question.id: question for question in golden}
    records = dict(completed)
    pending = [question for question in questions if question.id not in records]
    skipped = len(questions) - len(pending)
    if skipped:
        progress(f"Resuming: {skipped} question(s) already done, {len(pending)} to go.")
    started = clock()
    for index, question in enumerate(pending, start=1):
        question_started = clock()
        parent = by_id.get(question.follow_up_of) if question.follow_up_of else None
        record = evaluate_question(
            question,
            service=service,
            searcher=searcher,
            judge=judge,
            parent=parent,
            parent_record=records.get(parent.id) if parent else None,
        )
        records[question.id] = record
        ordered = order_records(records.values(), golden)
        save(ordered)
        now = clock()
        elapsed = now - started
        eta = elapsed / index * (len(pending) - index)
        progress(
            f"[{index}/{len(pending)}] {question.id} {question.type}: {_status(record)} | "
            f"{now - question_started:.0f}s, elapsed {_duration(elapsed)}, ETA {_duration(eta)}"
        )
    return order_records(records.values(), golden)


def order_records(
    records: Iterable[QuestionRecord], golden: Sequence[GoldenQuestion]
) -> list[QuestionRecord]:
    """Records in golden-set order; ids no longer in the golden set go last."""
    position = {question.id: index for index, question in enumerate(golden)}
    return sorted(records, key=lambda record: position.get(record.id, len(position)))


def _status(record: QuestionRecord) -> str:
    if record.refused:
        return "refused" + ("" if record.correct_refusal else " (FALSE REFUSAL)")
    parts = ["answered" + ("" if record.correct_refusal else " (FALSE ANSWER)")]
    if record.faithfulness is not None:
        parts.append(f"faithfulness {record.faithfulness:.2f}")
    if record.relevance is not None:
        parts.append(f"relevance {record.relevance}")
    if record.judge_errors:
        parts.append("JUDGE ERROR")
    return ", ".join(parts)


def _duration(seconds: float) -> str:
    minutes, secs = divmod(round(seconds), 60)
    return f"{minutes}m{secs:02d}s" if minutes else f"{secs}s"


# --- aggregation --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Summary:
    questions: int
    answered: int
    correct_refusals: int
    answerable: int
    false_refusals: int  # answerable questions refused
    unanswerable: int
    false_answers: int  # unanswerable questions answered
    cited_valid: int
    cited_total: int
    sentences: int
    cited_sentences: int
    answers_fully_cited: int  # answered questions whose every sentence has a citation
    retrieval_scored: int
    retrieval_hits: int
    faithfulness: tuple[float, ...]
    relevance: tuple[int, ...]
    claims: int  # claims the judge checked
    unshown_support: int  # supported claims naming no passage the judge was shown
    judge_errors: int  # questions with at least one judge error


def summarize(records: Sequence[QuestionRecord]) -> Summary:
    answered = [r for r in records if not r.refused]
    answerable = [r for r in records if r.answerable]
    unanswerable = [r for r in records if not r.answerable]
    return Summary(
        questions=len(records),
        answered=len(answered),
        correct_refusals=sum(1 for r in records if r.correct_refusal),
        answerable=len(answerable),
        false_refusals=sum(1 for r in answerable if r.refused),
        unanswerable=len(unanswerable),
        false_answers=sum(1 for r in unanswerable if not r.refused),
        cited_valid=sum(len(r.cited) for r in records),
        cited_total=sum(len(r.cited) + len(r.invalid_citations) for r in records),
        sentences=sum(r.sentences for r in answered),
        cited_sentences=sum(r.cited_sentences for r in answered),
        answers_fully_cited=sum(
            1 for r in answered if r.sentences and r.cited_sentences == r.sentences
        ),
        retrieval_scored=len(answerable),
        retrieval_hits=sum(1 for r in answerable if r.retrieval_hit),
        faithfulness=tuple(r.faithfulness for r in records if r.faithfulness is not None),
        relevance=tuple(r.relevance for r in records if r.relevance is not None),
        claims=sum(len(r.claims) for r in records),
        unshown_support=sum(r.unshown_support for r in records),
        judge_errors=sum(1 for r in records if r.judge_errors),
    )


def group_records(records: Sequence[QuestionRecord]) -> list[tuple[str, list[QuestionRecord]]]:
    """All questions, each type of standalone question, then follow-ups; empty groups dropped."""
    standalone = [r for r in records if not r.follow_up]
    groups = [("all", list(records))]
    groups += [(name, [r for r in standalone if r.type == name]) for name in GROUP_ORDER[1:4]]
    other_types = sorted({r.type for r in standalone} - set(GROUP_ORDER))
    groups += [(name, [r for r in standalone if r.type == name]) for name in other_types]
    groups.append(("follow-up", [r for r in records if r.follow_up]))
    return [(name, members) for name, members in groups if members]


def failure_categories(record: QuestionRecord) -> list[str]:
    """Why a question counts as a failure; empty when it passed every check."""
    categories = []
    if not record.answerable and not record.refused:
        categories.append("false answer")
    if record.retrieval_hit is False:
        categories.append("retrieval miss")
    if record.answerable and record.refused:
        categories.append("false refusal")
    if record.invalid_citations:
        categories.append("invalid citation")
    if record.faithfulness is not None and record.faithfulness < 1:
        categories.append("unsupported claim")
    if record.relevance is not None and record.relevance <= LOW_RELEVANCE:
        categories.append("low relevance")
    if record.judge_errors:
        categories.append("judge error")
    return categories


def latency_stages(records: Sequence[QuestionRecord]) -> list[tuple[str, list[float]]]:
    """Seconds per stage: rewrite (follow-ups), retrieve, generate, judge, and the total."""

    def stage(name: str) -> list[float]:
        return [r.timings_ms[name] / 1000 for r in records if name in r.timings_ms]

    judged = [r.judge_ms / 1000 for r in records if r.judge_attempts]
    totals = [(sum(r.timings_ms.values()) + r.judge_ms) / 1000 for r in records]
    stages = [
        ("rewrite (follow-ups)", stage("rewrite")),
        ("retrieve", stage("retrieve")),
        ("generate", [s for s in stage("generate") if s > 0]),
        ("judge (answered questions)", judged),
        ("total per question", totals),
    ]
    return [(name, values) for name, values in stages if values]


# --- tables -------------------------------------------------------------------------------------


def _ratio(numerator: int, denominator: int) -> str:
    if not denominator:
        return "n/a"
    return f"{numerator / denominator:.2f} ({numerator}/{denominator})"


def _mean(values: Sequence[float], digits: int = 2) -> str:
    return f"{statistics.fmean(values):.{digits}f}" if values else "n/a"


def summary_table(records: Sequence[QuestionRecord]) -> str:
    s = summarize(records)
    full_faithful = sum(1 for score in s.faithfulness if score == 1)
    high_relevance = sum(1 for score in s.relevance if score >= 4)
    rows = [
        ["Refusal accuracy", _ratio(s.correct_refusals, s.questions), "All questions"],
        ["False-refusal rate", _ratio(s.false_refusals, s.answerable), "Answerable, refused"],
        ["False-answer rate", _ratio(s.false_answers, s.unanswerable), "Unanswerable, answered"],
        ["Citation validity", _ratio(s.cited_valid, s.cited_total), "Cited numbers supplied"],
        [
            "Sentence citation coverage",
            _ratio(s.cited_sentences, s.sentences),
            "Answer sentences with a citation",
        ],
        [
            "Fully cited answers",
            _ratio(s.answers_fully_cited, s.answered),
            "Answers citing every sentence",
        ],
        [
            "Retrieval hit",
            _ratio(s.retrieval_hits, s.retrieval_scored),
            "Answerable, target among the passages",
        ],
        ["Faithfulness (mean)", _mean(s.faithfulness), f"{len(s.faithfulness)} judged answers"],
        [
            "Fully supported answers",
            _ratio(full_faithful, len(s.faithfulness)),
            "Every claim supported",
        ],
        ["Answer relevance (mean of 1-5)", _mean(s.relevance), f"{len(s.relevance)} judged"],
        ["Relevance 4 or 5", _ratio(high_relevance, len(s.relevance)), "Judged answers"],
        [
            "Supported claims naming no shown passage",
            _ratio(s.unshown_support, s.claims),
            "Claims judged; check these verdicts by hand",
        ],
        ["Judge errors", str(s.judge_errors), "Questions with an unusable judge reply"],
    ]
    return _table(["Metric", "Value", "Over"], rows)


def group_table(records: Sequence[QuestionRecord]) -> str:
    rows = []
    for name, members in group_records(records):
        s = summarize(members)
        rows.append(
            [
                name,
                s.questions,
                _ratio(s.correct_refusals, s.questions),
                _ratio(s.cited_valid, s.cited_total),
                _ratio(s.cited_sentences, s.sentences),
                _ratio(s.retrieval_hits, s.retrieval_scored),
                _mean(s.faithfulness),
                _mean(s.relevance),
            ]
        )
    headers = [
        "Group",
        "Questions",
        "Refusal accuracy",
        "Citation validity",
        "Sentence coverage",
        "Retrieval hit",
        "Faithfulness",
        "Relevance",
    ]
    return _table(headers, rows)


def rewrite_table(records: Sequence[QuestionRecord]) -> str:
    rows = [
        [
            r.id,
            r.follow_up_of,
            _cell(r.question),
            _cell(r.search_query),
            _found(r.raw_query_hit),
            _found(r.retrieval_hit),
        ]
        for r in records
        if r.follow_up
    ]
    headers = ["Question", "Follows", "Asked", "Rewritten query", "As asked", "Rewritten"]
    return _table(headers, rows)


def rewrite_gain_sentence(records: Sequence[QuestionRecord]) -> str:
    scored = [r for r in records if r.raw_query_hit is not None]
    if not scored:
        return "No answerable follow-up questions were evaluated."
    rewritten = sum(1 for r in scored if r.retrieval_hit)
    raw = sum(1 for r in scored if r.raw_query_hit)
    gain = rewritten - raw
    return (
        f"The rewritten query put a target among the model's passages for {rewritten} of "
        f"{len(scored)} follow-ups, against {raw} of {len(scored)} searching the follow-up as "
        f"asked: a gain of {gain:+d}."
    )


def latency_table(records: Sequence[QuestionRecord]) -> str:
    rows = [
        [
            name,
            len(values),
            f"{statistics.median(values):.1f}",
            f"{percentile(values, 0.95):.1f}",
        ]
        for name, values in latency_stages(records)
    ]
    return _table(["Stage", "Questions", "Median s", "p95 s"], rows)


def failure_rows(records: Sequence[QuestionRecord]) -> list[list[str]]:
    rows = []
    for record in records:
        categories = failure_categories(record)
        if categories:
            rows.append(
                [record.id, record.type, ", ".join(categories), _cell(_failure_detail(record))]
            )
    return rows


def failure_table(records: Sequence[QuestionRecord]) -> str:
    rows = failure_rows(records)
    if not rows:
        return "Every question passed every check."
    return _table(["Question", "Type", "Category", "Detail"], rows)


def _failure_detail(record: QuestionRecord) -> str:
    details = [f'Asked: "{record.question}"']
    if not record.answerable and not record.refused:
        details.append(f'Answered: "{_quote(record.answer)}"')
    if record.answerable and record.refused:
        where = (
            f"a target was passage {record.retrieval_rank}"
            if record.retrieval_rank
            else "no target was among the passages"
        )
        details.append(f"Refused; {where}.")
    if record.invalid_citations:
        cited = ", ".join(str(n) for n in record.invalid_citations)
        details.append(f"Cited passages not supplied: {cited}.")
    unsupported = [claim for claim in record.claims if not claim.get("supported")]
    if unsupported and record.faithfulness is not None:
        details.append(
            f"{len(unsupported)} of {len(record.claims)} claims unsupported, for example: "
            f'"{_quote(str(unsupported[0].get("claim", "")))}"'
        )
    if record.relevance is not None and record.relevance <= LOW_RELEVANCE:
        details.append(f"Relevance {record.relevance}.")
    if record.judge_errors:
        details.append("; ".join(record.judge_errors))
    return " ".join(details)


def _quote(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= QUOTE_CHARS else text[: QUOTE_CHARS - 3].rstrip() + "..."


def _cell(text: str | None) -> str:
    return " ".join((text or "").split()).replace("|", "\\|")


def _found(hit: bool | None) -> str:
    if hit is None:
        return "n/a"
    return "yes" if hit else "no"


def headline(records: Sequence[QuestionRecord]) -> str:
    s = summarize(records)
    parts = [f"refusal accuracy {s.correct_refusals / s.questions:.2f}" if s.questions else ""]
    if s.faithfulness:
        parts.append(f"faithfulness {statistics.fmean(s.faithfulness):.2f}")
    if s.relevance:
        parts.append(f"relevance {statistics.fmean(s.relevance):.1f} of 5")
    if s.cited_total:
        parts.append(f"citation validity {s.cited_valid / s.cited_total:.2f}")
    joined = ", ".join(part for part in parts if part)
    return f"Over {s.questions} questions: {joined}."


def stdout_report(records: Sequence[QuestionRecord]) -> str:
    return "\n\n".join(
        [
            "Answer metrics",
            summary_table(records),
            "By question type",
            group_table(records),
            "Follow-up rewriting",
            rewrite_table(records),
            rewrite_gain_sentence(records),
            "Failures",
            failure_table(records),
            "Latency",
            latency_table(records),
        ]
    )


# --- output files -------------------------------------------------------------------------------


@dataclass(frozen=True)
class RunInfo:
    started_at: str  # ISO 8601, UTC
    git_commit: str
    answer_model: str
    rewrite_model: str
    judge_model: str
    embedding_model: str
    documents: int
    chunks: int
    golden_questions: int
    search_mode: str = SEARCH_MODE
    k: int = SEARCH_K


def results_path(directory: Path, started_at: datetime) -> Path:
    return directory / f"answers-{started_at.astimezone(UTC):%Y%m%dT%H%M%SZ}.json"


def write_raw_results(path: Path, info: RunInfo, records: Sequence[QuestionRecord]) -> None:
    """Write the run details and every record, replacing the file atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "run": asdict(info),
        "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "questions": [asdict(record) for record in records],
    }
    partial = path.with_name(path.name + ".partial")
    partial.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(partial, path)


def read_raw_results(path: Path) -> tuple[RunInfo, list[QuestionRecord]]:
    """Load a raw results file for --resume. Raises `ResumeError`."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        info = RunInfo(**document["run"])
        records = [QuestionRecord.from_dict(item) for item in document["questions"]]
    except OSError as exc:
        raise ResumeError(f"cannot read {path}: {exc}") from exc
    except (ValueError, KeyError, TypeError) as exc:
        raise ResumeError(f"{path} is not an answer evaluation results file: {exc}") from exc
    return info, records


def render_report(info: RunInfo, records: Sequence[QuestionRecord]) -> str:
    """The committed Markdown summary. It quotes our questions and, briefly, our model's answers;
    it never holds passage text."""
    date = info.started_at[:10]
    scope = (
        "every question"
        if len(records) == info.golden_questions
        else f"{len(records)} of them (a subset)"
    )
    return f"""# Answer results

{headline(records)}

Generated on {date} at commit `{info.git_commit}`. The corpus has {info.documents:,} documents \
and {info.chunks:,} chunks, embedded with `{info.embedding_model}`. The golden set has \
{info.golden_questions} questions and this run answered {scope}. `{info.answer_model}` writes each \
answer from {info.search_mode} search (top {info.k} chunks, up to six passages in the prompt), \
`{info.rewrite_model}` rewrites follow-up questions, and `{info.judge_model}` judges faithfulness \
and relevance. See [evaluation](../evaluation.md#answer-metrics) for the metric definitions and \
the [answer evaluation runbook](../../runbooks/evaluate-answers.md) to repeat the run.

## Summary

{summary_table(records)}

## By question type

Paraphrase, exact-term, and unanswerable rows hold standalone questions; follow-ups are grouped \
on their own. Refusal accuracy counts an answerable question as correct when it is answered and \
an unanswerable one when it is refused. Faithfulness and relevance are judged only for answers, \
not refusals.

{group_table(records)}

## Follow-up rewriting

Each follow-up is asked with the earlier question and this run's answer to it as history, so the \
rewrite model turns it into a standalone query. The last two columns show whether a target \
passage reached the answer model when searching the follow-up as asked and when searching the \
rewritten query.

{rewrite_table(records)}

{rewrite_gain_sentence(records)}

## Failure analysis

Each question that failed a check, with its categories: retrieval miss (no target among the \
passages given to the model), false refusal, false answer (an unanswerable question answered), \
invalid citation, unsupported claim (faithfulness below 1), low relevance (a score of \
{LOW_RELEVANCE} or less), and judge error. Quotes of the model's answers are shortened.

{failure_table(records)}

## Latency

Wall time per stage on the local machine (16 CPU cores, no GPU). The first question includes \
loading each model, and the judge and answer models share the CPU, so treat these as rough.

{latency_table(records)}

## Method and limitations

One judge model, `{info.judge_model}`, grades every answer at temperature 0 with a JSON schema. \
It comes from a different family than the answer model, so it does not grade its own family's \
work, but a single 8B judge still misreads some claims; spot-check its verdicts in the raw \
results before acting on a small difference. The judge sometimes labels a supported claim with a \
passage number it was not shown; those verdicts stand and the summary counts them. Faithfulness \
gives the judge only the passages an \
answer cites, so a true claim with a missing or wrong citation counts as unsupported. Relevance \
gives the judge the golden reference answer as a hint of what a good answer covers. The golden \
set is small ({info.golden_questions} questions), so one question moves a rate by several \
points. Timings come from one CPU-only run and vary with other load on the machine.
"""


# --- command ------------------------------------------------------------------------------------


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--ids",
        metavar="ID[,ID...]",
        help="evaluate only these question ids, for example q001,q005 (default: all)",
    )
    parser.add_argument(
        "--limit", type=int, metavar="N", help="evaluate only the first N selected questions"
    )
    parser.add_argument(
        "--resume",
        type=Path,
        metavar="PATH",
        help="continue a run: skip the questions already in this raw results file and append",
    )
    parser.add_argument(
        "--write-report",
        action="store_true",
        help="also write the committed summary docs/results/answers.md",
    )


def select_questions(
    golden: Sequence[GoldenQuestion], ids: str | None, limit: int | None
) -> list[GoldenQuestion]:
    """Questions in golden-set order, filtered by `ids` and cut to `limit`. Raises ValueError."""
    selected = list(golden)
    if ids:
        wanted = [part.strip() for part in ids.split(",") if part.strip()]
        known = {question.id for question in golden}
        unknown = [question_id for question_id in wanted if question_id not in known]
        if unknown:
            raise ValueError(f"unknown question id(s): {', '.join(unknown)}")
        selected = [question for question in golden if question.id in set(wanted)]
    if limit is not None:
        if limit < 1:
            raise ValueError(f"--limit must be at least 1, got {limit}")
        selected = selected[:limit]
    if not selected:
        raise ValueError("no questions selected")
    return selected


def _load_golden() -> list[GoldenQuestion]:
    return load_golden_set()


def _error(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)


def _check_resume_models(info: RunInfo, current: Mapping[str, str]) -> None:
    for name, model in current.items():
        if getattr(info, name) != model:
            raise ResumeError(
                f"the results file used {name.replace('_', ' ')} {getattr(info, name)!r}, "
                f"but the current setting is {model!r}; start a new run instead"
            )


def run(args: argparse.Namespace) -> int:
    """Run the `answers` subcommand. Returns the process exit code."""
    try:
        settings = load_settings()
    except ValueError as exc:
        _error(str(exc))
        return EXIT_USAGE
    try:
        golden = _load_golden()
    except GoldenSetError as exc:
        _error(f"cannot load the golden set: {exc}")
        return EXIT_GOLDEN_SET_UNAVAILABLE
    try:
        questions = select_questions(golden, args.ids, args.limit)
    except ValueError as exc:
        _error(str(exc))
        return EXIT_USAGE

    models = {
        "answer_model": settings.chat.answer_model,
        "rewrite_model": settings.chat.rewrite_model,
        "judge_model": settings.chat.judge_model,
        "embedding_model": settings.embedding.model,
    }
    repo_root = find_repo_root() or Path.cwd()
    info: RunInfo | None = None
    completed: dict[str, QuestionRecord] = {}
    if args.resume is not None:
        try:
            info, resumed = read_raw_results(args.resume)
            _check_resume_models(info, models)
        except ResumeError as exc:
            _error(str(exc))
            return EXIT_USAGE
        completed = {record.id: record for record in resumed}
        raw_path: Path = args.resume
    else:
        started_at = datetime.now(UTC)
        raw_path = results_path(repo_root / "eval-results", started_at)

    # Answers and judge replies can hold characters the console encoding cannot show.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(errors="replace")

    try:
        conn = open_connection(settings)
    except SetupError as exc:
        _error(str(exc))
        return exc.exit_code

    records: list[QuestionRecord] = order_records(completed.values(), golden)
    try:
        with conn, ExitStack() as stack:
            embedder = stack.enter_context(OpenAICompatibleEmbedder(settings.embedding))
            chat = stack.enter_context(OpenAICompatibleChat(settings.chat))
            try:
                embedder.check()
                for model in dict.fromkeys(
                    (models["answer_model"], models["rewrite_model"], models["judge_model"])
                ):
                    chat.check(model)
            except (EmbeddingError, ChatError) as exc:
                _error(str(exc))
                return EXIT_MODEL_UNAVAILABLE
            if info is None:
                documents, chunks, _ = corpus_counts(conn, embedder.model)
                info = RunInfo(
                    started_at=started_at.isoformat(timespec="seconds"),
                    git_commit=git_commit_label(repo_root),
                    documents=documents,
                    chunks=chunks,
                    golden_questions=len(golden),
                    **models,
                )
            run_info = info
            searcher = PolicySearcher(conn, embedder)
            service = AnswerService(
                searcher, chat, settings.chat, k=SEARCH_K, search_mode=SEARCH_MODE
            )
            judge = Judge(chat, settings.chat.judge_model)

            def save(current: list[QuestionRecord]) -> None:
                nonlocal records
                records = current
                write_raw_results(raw_path, run_info, current)

            print(f"Raw results: {raw_path}", flush=True)
            records = evaluate_questions(
                questions,
                service=service,
                searcher=searcher,
                judge=judge,
                golden=golden,
                completed=completed,
                save=save,
            )
            write_raw_results(raw_path, run_info, records)
    except (EmbeddingError, ChatError) as exc:
        _error(f"{exc}\n{_resume_hint(raw_path, records)}")
        return EXIT_MODEL_UNAVAILABLE
    except psycopg.Error as exc:
        _error(f"database error: {_first_line(exc)}\n{_resume_hint(raw_path, records)}")
        return EXIT_DATABASE_ERROR

    print()
    print(stdout_report(records))
    print(f"\nSummary: {headline(records)}")
    print(f"Raw results: {raw_path}")
    if args.write_report:
        report_path = repo_root / "docs" / "results" / "answers.md"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(render_report(info, records), encoding="utf-8")
        print(f"Report: {report_path}")
    return 0


def _resume_hint(raw_path: Path, records: Sequence[QuestionRecord]) -> str:
    if not records:
        return "No question finished; rerun the command."
    return (
        f"{len(records)} finished question(s) are saved. Continue with: "
        f"uv run python -m campus_rag.evaluate answers --resume {raw_path}"
    )
