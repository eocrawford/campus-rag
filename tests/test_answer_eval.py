"""Answer evaluation: metric math, sentence splitting, follow-up wiring, saving and resuming, the
report, and the command's exit codes. Every model and the database are faked."""

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest
from fakes import FakeChat, FakeSearcher, make_chunk

from campus_rag.config import ChatSettings, Settings
from campus_rag.evaluate import __main__ as evaluate_cli
from campus_rag.evaluate import answers as ae
from campus_rag.evaluate.golden import GoldenQuestion, GoldenSetError, GoldenTarget
from campus_rag.evaluate.judge import FAITHFULNESS_SYSTEM_PROMPT, Judge
from campus_rag.generate.answer import AnswerService
from campus_rag.generate.chat import ChatError
from campus_rag.generate.models import REFUSAL_TEXT, ChatTurn
from campus_rag.index.embed import EmbeddingError
from campus_rag.retrieve.connect import SetupError

CHAT = ChatSettings(answer_model="answerer", rewrite_model="rewriter", judge_model="judge")
REWRITTEN = "Can graduate students get an incomplete?"
FAITHFUL = json.dumps(
    {
        "claims": [
            {"claim": "Rule one applies.", "passages": [1], "reason": "", "verdict": "supported"},
            {"claim": "Rule two applies.", "passages": [], "reason": "", "verdict": "unsupported"},
        ]
    }
)
RELEVANT = json.dumps({"reason": "On point.", "score": 4})


def golden(
    question_id: str,
    text: str = "Which rules apply?",
    type: str = "paraphrase",
    *,
    target_rank: int | None = 1,
    follow_up_of: str | None = None,
) -> GoldenQuestion:
    """A golden question whose target is make_chunk(target_rank)'s document and heading."""
    expected = ()
    if type != "unanswerable" and target_rank is not None:
        chunk = make_chunk(target_rank)
        expected = (GoldenTarget(chunk.url, chunk.heading_path),)
    elif type != "unanswerable":
        expected = (GoldenTarget("https://elsewhere.example.edu/", "Elsewhere"),)
    return GoldenQuestion(
        id=question_id,
        question=text,
        type=type,
        expected=expected,
        reference_answer="Rules one and two apply.",
        follow_up_of=follow_up_of,
    )


def scripted_chat(
    answer: str = "Rule one applies [1]. Rule two applies [2].",
    *,
    faithfulness: str = FAITHFUL,
    relevance: str = RELEVANT,
) -> FakeChat:
    """Replies by model: the answer, the rewritten query, or the judge's JSON."""

    def respond(messages: Sequence[Mapping[str, str]], model: str) -> str:
        if model == "rewriter":
            return REWRITTEN
        if model == "judge":
            is_faithfulness = messages[0]["content"] == FAITHFULNESS_SYSTEM_PROMPT
            return faithfulness if is_faithfulness else relevance
        return answer

    return FakeChat(respond=respond)


class RecordingService:
    """Wraps AnswerService and records the history passed with each question."""

    def __init__(self, inner: AnswerService) -> None:
        self.inner = inner
        self.histories: list[tuple[ChatTurn, ...]] = []

    def answer(self, question: str, history: Sequence[ChatTurn] = ()):
        self.histories.append(tuple(history))
        return self.inner.answer(question, history)


def parts(chat: FakeChat | None = None, chunks=None):
    chat = chat or scripted_chat()
    searcher = FakeSearcher(chunks if chunks is not None else [make_chunk(r) for r in (1, 2, 3)])
    service = RecordingService(AnswerService(searcher, chat, CHAT))
    return chat, searcher, service, Judge(chat, "judge")


def record(question_id: str = "q1", **overrides) -> ae.QuestionRecord:
    values = {
        "id": question_id,
        "type": "paraphrase",
        "question": "Which rules apply?",
        "answer": "Rule one applies [1].",
        "refused": False,
        "search_query": "Which rules apply?",
        "cited": [1],
        "sentences": 1,
        "cited_sentences": 1,
        "retrieval_rank": 1,
        "timings_ms": {"retrieve": 100.0, "generate": 50_000.0},
        "faithfulness": 1.0,
        "claims": [{"claim": "Rule one applies.", "supported": True}],
        "relevance": 5,
        "judge_attempts": 2,
        "judge_ms": 20_000.0,
    }
    values.update(overrides)
    return ae.QuestionRecord(**values)


def refusal(question_id: str, type: str = "unanswerable", **overrides) -> ae.QuestionRecord:
    values = {
        "type": type,
        "answer": REFUSAL_TEXT,
        "refused": True,
        "cited": [],
        "sentences": 0,
        "cited_sentences": 0,
        "retrieval_rank": None,
        "faithfulness": None,
        "claims": [],
        "relevance": None,
        "judge_attempts": 0,
        "judge_ms": 0.0,
    }
    values.update(overrides)
    return record(question_id, **values)


# --- sentence citation coverage -----------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Rule one applies [1]. Rule two applies [2].", (2, 2)),
        ("Rule one applies [1]. Rule two applies.", (2, 1)),
        ("Rule one applies. [1] Rule two applies [2][3].", (2, 2)),
        ("The U.S. Army rule applies [1]. Under APS No. 11 and APS 2.4, yes [2].", (2, 2)),
        ("Some cases, e.g. illness, qualify [1]. Others do not!", (2, 1)),
        ('He said "yes." [1] Then "No" was added [2]? Fine.', (3, 2)),
        ("Key points:\n- First rule [1].\n- Second rule.\n\n1. Third rule [2]", (3, 2)),
        ("**Eligibility:**\nStaff qualify [1].\n[2]", (1, 1)),
        ("", (0, 0)),
    ],
)
def test_sentence_citation_counts(text: str, expected: tuple[int, int]) -> None:
    assert ae.sentence_citation_counts(text) == expected


def test_split_sentences_keeps_markers_with_their_sentence() -> None:
    assert ae.split_sentences("Rule one applies. [1] Rule two applies [2].") == [
        "Rule one applies. [1]",
        "Rule two applies [2].",
    ]


# --- record metrics -----------------------------------------------------------------------------


def test_record_metrics() -> None:
    answered = record(cited=[1, 2], invalid_citations=[7], sentences=4, cited_sentences=3)
    assert answered.citation_validity == pytest.approx(2 / 3)
    assert answered.sentence_coverage == pytest.approx(0.75)
    assert answered.correct_refusal and answered.retrieval_hit is True
    assert answered.raw_query_hit is None
    assert record(retrieval_rank=None).retrieval_hit is False
    assert refusal("q9").correct_refusal and refusal("q9").retrieval_hit is None
    assert refusal("q9").citation_validity is None and refusal("q9").sentence_coverage is None
    assert not refusal("q9", type="paraphrase").correct_refusal
    assert not record(type="unanswerable", retrieval_rank=None).correct_refusal
    follow_up = record(follow_up_of="q0", raw_query_rank=None)
    assert follow_up.follow_up and follow_up.raw_query_hit is False


def test_summarize_counts_refusals_citations_retrieval_and_judge_scores() -> None:
    records = [
        record("q1", cited=[1, 2], invalid_citations=[9], sentences=2, cited_sentences=2),
        record("q2", retrieval_rank=None, faithfulness=0.5, relevance=2, sentences=3),
        refusal("q3", type="exact_term"),
        refusal("q4"),
        record("q5", type="unanswerable", retrieval_rank=None, relevance=3, faithfulness=1.0),
        refusal("q6", invalid_citations=[4]),
        record("q7", judge_errors=["relevance: bad"], relevance=None),
    ]
    s = ae.summarize(records)
    assert (s.questions, s.answered, s.correct_refusals) == (7, 4, 5)
    assert (s.answerable, s.false_refusals) == (4, 1)
    assert (s.unanswerable, s.false_answers) == (3, 1)
    assert (s.cited_valid, s.cited_total) == (5, 7)
    assert (s.sentences, s.cited_sentences, s.answers_fully_cited) == (7, 5, 3)
    assert (s.retrieval_scored, s.retrieval_hits) == (4, 2)
    assert s.faithfulness == (1.0, 0.5, 1.0, 1.0)
    assert s.relevance == (5, 2, 3)
    assert s.judge_errors == 1


def test_groups_split_standalone_types_from_follow_ups() -> None:
    records = [
        record("q1"),
        record("q2", type="exact_term"),
        record("q3", follow_up_of="q1"),
        refusal("q4"),
    ]
    groups = dict(ae.group_records(records))
    assert list(groups) == ["all", "paraphrase", "exact_term", "unanswerable", "follow-up"]
    assert [r.id for r in groups["paraphrase"]] == ["q1"]
    assert [r.id for r in groups["follow-up"]] == ["q3"]
    assert len(groups["all"]) == 4


def test_failure_categories() -> None:
    assert ae.failure_categories(record()) == []
    assert ae.failure_categories(refusal("q1")) == []
    assert ae.failure_categories(refusal("q1", type="paraphrase", retrieval_rank=None)) == [
        "retrieval miss",
        "false refusal",
    ]
    assert ae.failure_categories(record(type="unanswerable", retrieval_rank=None)) == [
        "false answer"
    ]
    assert ae.failure_categories(
        record(invalid_citations=[8], faithfulness=0.5, relevance=2, judge_errors=["x"])
    ) == ["invalid citation", "unsupported claim", "low relevance", "judge error"]


def test_latency_stages_use_seconds_and_skip_unjudged_questions() -> None:
    records = [
        record("q1", timings_ms={"rewrite": 2000.0, "retrieve": 100.0, "generate": 40_000.0}),
        record("q2"),
        refusal("q3", timings_ms={"retrieve": 120.0, "generate": 0.0}),
    ]
    stages = dict(ae.latency_stages(records))
    assert stages["rewrite (follow-ups)"] == [2.0]
    assert stages["retrieve"] == [0.1, 0.1, 0.12]
    assert stages["generate"] == [40.0, 50.0]
    assert stages["judge (answered questions)"] == [20.0, 20.0]
    assert stages["total per question"] == pytest.approx([62.1, 70.1, 0.12])


# --- answering and judging ----------------------------------------------------------------------


def test_evaluate_question_records_answer_metrics_and_judge_scores() -> None:
    chat, searcher, service, judge = parts()
    result = ae.evaluate_question(golden("q1"), service=service, searcher=searcher, judge=judge)
    assert not result.refused and result.cited == [1, 2] and result.invalid_citations == []
    assert (result.sentences, result.cited_sentences) == (2, 2)
    assert result.retrieval_rank == 1 and result.raw_query_rank is None
    assert result.faithfulness == pytest.approx(0.5) and result.relevance == 4
    assert result.claims[1] == {
        "claim": "Rule two applies.",
        "supported": False,
        "passages": (),
        "reason": "",
    }
    assert result.judge_attempts == 2 and result.judge_errors == [] and result.judge_replies == []
    assert result.unshown_support == 0
    assert [p["number"] for p in result.passages] == [1, 2, 3]
    assert service.histories == [()]
    assert [call["model"] for call in chat.calls] == ["answerer", "judge", "judge"]
    assert len(searcher.calls) == 1


def test_supported_claims_naming_a_passage_the_judge_was_not_shown_are_counted() -> None:
    misread = json.dumps(
        {"claims": [{"claim": "Rule one.", "passages": [9], "reason": "", "verdict": "supported"}]}
    )
    _, searcher, service, judge = parts(scripted_chat(faithfulness=misread))
    result = ae.evaluate_question(golden("q1"), service=service, searcher=searcher, judge=judge)
    assert result.faithfulness == 1.0 and result.unshown_support == 1
    summary = ae.summarize([result, record("q2", claims=[{}, {}], unshown_support=0)])
    assert (summary.claims, summary.unshown_support) == (3, 1)
    assert "| Supported claims naming no shown passage | 0.33 (1/3) |" in ae.summary_table(
        [result, record("q2", claims=[{}, {}])]
    )


def test_a_target_outside_the_passages_is_a_retrieval_miss() -> None:
    _, searcher, service, judge = parts()
    question = golden("q1", target_rank=None)
    result = ae.evaluate_question(question, service=service, searcher=searcher, judge=judge)
    assert result.retrieval_rank is None and result.retrieval_hit is False


def test_refusals_are_not_judged() -> None:
    chat, searcher, service, judge = parts(scripted_chat(answer=REFUSAL_TEXT))
    question = golden("q9", "Where is lost and found?", "unanswerable")
    result = ae.evaluate_question(question, service=service, searcher=searcher, judge=judge)
    assert result.refused and result.correct_refusal
    assert result.faithfulness is None and result.relevance is None and result.judge_attempts == 0
    assert result.retrieval_rank is None and result.sentences == 0
    assert [call["model"] for call in chat.calls] == ["answerer"]


def test_judge_errors_are_recorded_with_the_raw_replies() -> None:
    chat, searcher, service, judge = parts(scripted_chat(relevance="nope"))
    result = ae.evaluate_question(golden("q1"), service=service, searcher=searcher, judge=judge)
    assert result.relevance is None and result.faithfulness == pytest.approx(0.5)
    assert len(result.judge_errors) == 1 and result.judge_errors[0].startswith("relevance:")
    assert result.judge_attempts == 3 and result.judge_replies == [FAITHFUL, "nope", "nope"]
    assert ae.failure_categories(result) == ["unsupported claim", "judge error"]


def test_a_follow_up_is_asked_with_the_earlier_question_and_this_runs_answer() -> None:
    chat, searcher, service, judge = parts()
    parent = golden("q1", "Can I get an incomplete?")
    follow_up = golden("q2", "What about graduate students?", follow_up_of="q1", target_rank=2)
    parent_record = ae.evaluate_question(parent, service=service, searcher=searcher, judge=judge)
    result = ae.evaluate_question(
        follow_up,
        service=service,
        searcher=searcher,
        judge=judge,
        parent=parent,
        parent_record=parent_record,
    )
    assert service.histories[1] == (
        ChatTurn("user", "Can I get an incomplete?"),
        ChatTurn("assistant", parent_record.answer),
    )
    assert result.history_has_answer is True
    assert result.search_query == REWRITTEN
    assert [call["query"] for call in searcher.calls] == [
        "Can I get an incomplete?",
        REWRITTEN,
        "What about graduate students?",
    ]
    assert searcher.calls[2]["mode"] == "hybrid" and searcher.calls[2]["k"] == ae.SEARCH_K
    assert result.retrieval_rank == 2 and result.raw_query_rank == 2
    assert "rewrite" in result.timings_ms
    relevance_prompt = chat.calls[-1]["messages"][1]["content"]
    assert "Earlier question in the conversation:\nCan I get an incomplete?" in relevance_prompt


def test_a_follow_up_without_the_earlier_answer_still_gets_the_earlier_question() -> None:
    _, searcher, service, judge = parts()
    parent = golden("q1", "Can I get an incomplete?")
    follow_up = golden("q2", "What about graduate students?", follow_up_of="q1")
    result = ae.evaluate_question(
        follow_up, service=service, searcher=searcher, judge=judge, parent=parent
    )
    assert service.histories == [(ChatTurn("user", "Can I get an incomplete?"),)]
    assert result.history_has_answer is False
    with pytest.raises(ValueError, match="unknown"):
        ae.evaluate_question(follow_up, service=service, searcher=searcher, judge=judge)


def test_evaluate_questions_saves_after_each_question_and_wires_follow_ups() -> None:
    _, searcher, service, judge = parts()
    questions = [
        golden("q1", "Can I get an incomplete?"),
        golden("q2", "What about graduate students?", follow_up_of="q1"),
        golden("q3", "Where is lost and found?", "unanswerable"),
    ]
    saved: list[list[str]] = []
    lines: list[str] = []
    ticks = iter([0.0, 0.0, 60.0, 60.0, 120.0, 120.0, 180.0])
    records = ae.evaluate_questions(
        questions,
        service=service,
        searcher=searcher,
        judge=judge,
        golden=questions,
        completed={},
        save=lambda current: saved.append([r.id for r in current]),
        progress=lines.append,
        clock=lambda: next(ticks),
    )
    assert [r.id for r in records] == ["q1", "q2", "q3"]
    assert saved == [["q1"], ["q1", "q2"], ["q1", "q2", "q3"]]
    assert service.histories[1][1] == ChatTurn("assistant", records[0].answer)
    assert lines[0].startswith("[1/3] q1 paraphrase: answered, faithfulness 0.50, relevance 4")
    assert "elapsed 1m00s, ETA 2m00s" in lines[0]
    assert "(FALSE ANSWER)" in lines[2] and lines[2].endswith("ETA 0s")


def test_evaluate_questions_resumes_by_skipping_completed_ids() -> None:
    _, searcher, service, judge = parts()
    questions = [
        golden("q1", "Can I get an incomplete?"),
        golden("q2", "What about graduate students?", follow_up_of="q1"),
    ]
    done = record("q1", question="Can I get an incomplete?", answer="Earlier answer [1].")
    lines: list[str] = []
    records = ae.evaluate_questions(
        list(reversed(questions)),
        service=service,
        searcher=searcher,
        judge=judge,
        golden=questions,
        completed={"q1": done},
        save=lambda current: None,
        progress=lines.append,
    )
    assert [r.id for r in records] == ["q1", "q2"] and records[0] is done
    assert service.histories == [
        (
            ChatTurn("user", "Can I get an incomplete?"),
            ChatTurn("assistant", "Earlier answer [1]."),
        )
    ]
    assert lines[0] == "Resuming: 1 question(s) already done, 1 to go."


# --- selection and files ------------------------------------------------------------------------


def test_select_questions_filters_by_id_in_golden_order_then_limits() -> None:
    questions = [golden(f"q{n}") for n in range(1, 6)]
    assert [q.id for q in ae.select_questions(questions, "q4, q2", None)] == ["q2", "q4"]
    assert [q.id for q in ae.select_questions(questions, None, 2)] == ["q1", "q2"]
    assert [q.id for q in ae.select_questions(questions, "q5,q3,q1", 2)] == ["q1", "q3"]
    with pytest.raises(ValueError, match="unknown question id"):
        ae.select_questions(questions, "q1,q99", None)
    with pytest.raises(ValueError, match="at least 1"):
        ae.select_questions(questions, None, 0)


INFO = ae.RunInfo(
    started_at="2026-11-03T20:00:00+00:00",
    git_commit="abc1234+uncommitted",
    answer_model="qwen2.5:7b-instruct",
    rewrite_model="qwen2.5:3b-instruct",
    judge_model="llama3.1:8b",
    embedding_model="nomic-embed-text",
    documents=247,
    chunks=1978,
    golden_questions=47,
)


def test_raw_results_round_trip_and_replace_the_file(tmp_path: Path) -> None:
    path = tmp_path / "out" / "answers.json"
    records = [record("q1", follow_up_of=None), refusal("q2")]
    ae.write_raw_results(path, INFO, records[:1])
    ae.write_raw_results(path, INFO, records)
    info, loaded = ae.read_raw_results(path)
    assert info == INFO and loaded == records
    assert [p.name for p in path.parent.iterdir()] == ["answers.json"]


def test_read_raw_results_rejects_other_files(tmp_path: Path) -> None:
    with pytest.raises(ae.ResumeError, match="cannot read"):
        ae.read_raw_results(tmp_path / "missing.json")
    other = tmp_path / "retrieval.json"
    other.write_text('{"run": {"k": 10}, "questions": []}', encoding="utf-8")
    with pytest.raises(ae.ResumeError, match="not an answer evaluation results file"):
        ae.read_raw_results(other)


def test_results_path_uses_a_utc_timestamp(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    started = datetime(2026, 11, 3, 20, 4, 43, tzinfo=UTC)
    assert ae.results_path(tmp_path, started) == tmp_path / "answers-20261103T200443Z.json"


# --- report -------------------------------------------------------------------------------------


REPORT_RECORDS = [
    record("q1"),
    record(
        "q2",
        follow_up_of="q1",
        question="What about | grad students?",
        search_query="Do grad students qualify?",
        raw_query_rank=None,
        timings_ms={"rewrite": 1500.0, "retrieve": 90.0, "generate": 45_000.0},
    ),
    refusal("q3", type="exact_term", retrieval_rank=4, question="What is APS 9.9?"),
    record(
        "q4",
        type="unanswerable",
        retrieval_rank=None,
        question="Where is lost and found?",
        answer="It is in the main office [1]. " + "More detail follows here. " * 10,
        faithfulness=0.5,
        claims=[
            {"claim": "It is in the main office.", "supported": False},
            {"claim": "Detail.", "supported": True},
        ],
    ),
]


def test_tables_and_headline() -> None:
    summary = ae.summary_table(REPORT_RECORDS)
    assert "| Refusal accuracy | 0.50 (2/4) |" in summary
    assert "| False-refusal rate | 0.33 (1/3) |" in summary
    assert "| False-answer rate | 1.00 (1/1) |" in summary
    assert "| Faithfulness (mean) | 0.83 |" in summary
    groups = ae.group_table(REPORT_RECORDS)
    assert "| follow-up | 1 |" in groups and "| exact_term | 1 |" in groups
    rewrites = ae.rewrite_table(REPORT_RECORDS)
    assert "| q2 | q1 | What about \\| grad students? | Do grad students qualify? | no | yes |" in (
        rewrites
    )
    assert ae.rewrite_gain_sentence(REPORT_RECORDS).endswith("a gain of +1.")
    assert ae.headline(REPORT_RECORDS) == (
        "Over 4 questions: refusal accuracy 0.50, faithfulness 0.83, relevance 5.0 of 5, "
        "citation validity 1.00."
    )


def test_failure_table_lists_each_failed_question_with_a_short_quote() -> None:
    rows = {row[0]: row for row in ae.failure_rows(REPORT_RECORDS)}
    assert set(rows) == {"q3", "q4"}
    assert rows["q3"][2] == "false refusal"
    assert "Refused; a target was passage 4." in rows["q3"][3]
    assert rows["q4"][2] == "false answer, unsupported claim"
    detail = rows["q4"][3]
    assert detail.startswith('Asked: "Where is lost and found?" Answered: "It is in the main')
    assert '..." 1 of 2 claims unsupported, for example: "It is in the main office."' in detail
    assert ae.failure_table([record("q1")]) == "Every question passed every check."


def test_report_has_the_required_sections_and_run_details() -> None:
    report = ae.render_report(INFO, REPORT_RECORDS)
    assert report.startswith("# Answer results\n\nOver 4 questions:")
    for heading in (
        "## Summary",
        "## By question type",
        "## Follow-up rewriting",
        "## Failure analysis",
        "## Latency",
        "## Method and limitations",
    ):
        assert heading in report
    assert "2026-11-03" in report and "`abc1234+uncommitted`" in report
    assert "247 documents" in report and "1,978 chunks" in report
    for model in ("qwen2.5:7b-instruct", "qwen2.5:3b-instruct", "llama3.1:8b", "nomic-embed-text"):
        assert f"`{model}`" in report
    assert "4 of them (a subset)" in report
    assert "| rewrite (follow-ups) | 1 | 1.5 | 1.5 |" in report
    assert "{" not in report and "}" not in report  # every template field was filled


# --- command ------------------------------------------------------------------------------------


class FakeConnection:
    def __init__(self) -> None:
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.closed = True


class Harness:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self.connection = FakeConnection()
        self.setup_error: SetupError | None = None
        self.check_error: Exception | None = None
        self.golden_error: GoldenSetError | None = None
        self.fail_on_question: str | None = None
        self.database_error: psycopg.Error | None = None
        self.questions = [
            golden("q1", "Can I get an incomplete?"),
            golden("q2", "What about graduate students?", follow_up_of="q1"),
            golden("q3", "Where is lost and found?", "unanswerable"),
        ]
        self.chat = scripted_chat()
        self.checked_models: list[str] = []
        harness = self

        class Embedder:
            model = "nomic-embed-text"

            def __init__(self, settings) -> None:
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc) -> None:
                pass

            def check(self) -> None:
                if isinstance(harness.check_error, EmbeddingError):
                    raise harness.check_error

        class Chat:
            def __init__(self, settings) -> None:
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc) -> None:
                pass

            def check(self, model: str) -> None:
                harness.checked_models.append(model)
                if isinstance(harness.check_error, ChatError):
                    raise harness.check_error

            def complete(self, messages, **options):
                if harness.fail_on_question and harness.fail_on_question in str(messages):
                    raise ChatError("chat request timed out")
                return harness.chat.complete(messages, **options)

        class Searcher(FakeSearcher):
            def search(self, query, **options):
                if harness.database_error:
                    raise harness.database_error
                return super().search(query, **options)

        def open_connection(settings):
            if harness.setup_error:
                raise harness.setup_error
            return harness.connection

        def load_golden():
            if harness.golden_error:
                raise harness.golden_error
            return harness.questions

        settings = Settings(database_url="postgresql://unused", chat=CHAT)
        monkeypatch.setattr(ae, "load_settings", lambda: settings)
        monkeypatch.setattr(ae, "open_connection", open_connection)
        monkeypatch.setattr(ae, "OpenAICompatibleEmbedder", Embedder)
        monkeypatch.setattr(ae, "OpenAICompatibleChat", Chat)
        monkeypatch.setattr(
            ae, "PolicySearcher", lambda conn, embedder: Searcher([make_chunk(1), make_chunk(2)])
        )
        monkeypatch.setattr(ae, "_load_golden", load_golden)
        monkeypatch.setattr(ae, "corpus_counts", lambda conn, model: (247, 1978, 1978))
        monkeypatch.setattr(ae, "find_repo_root", lambda: tmp_path)
        monkeypatch.setattr(ae, "git_commit_label", lambda root: "abc1234")

    def raw_files(self, tmp_path: Path) -> list[Path]:
        return sorted((tmp_path / "eval-results").glob("answers-*.json"))


def run_command(*argv: str) -> int:
    parser = argparse.ArgumentParser()
    ae.add_arguments(parser)
    return ae.run(parser.parse_args(list(argv)))


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Harness:
    return Harness(monkeypatch, tmp_path)


def test_command_answers_every_question_and_writes_raw_results(
    harness: Harness, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_command() == 0
    output = capsys.readouterr().out
    assert "[1/3] q1 paraphrase" in output and "[3/3] q3 unanswerable" in output
    assert "| Refusal accuracy |" in output and "Summary: Over 3 questions" in output
    (raw,) = harness.raw_files(tmp_path)
    document = json.loads(raw.read_text(encoding="utf-8"))
    assert document["run"]["judge_model"] == "judge" and document["run"]["git_commit"] == "abc1234"
    assert [q["id"] for q in document["questions"]] == ["q1", "q2", "q3"]
    assert document["questions"][1]["search_query"] == REWRITTEN
    assert harness.checked_models == ["answerer", "rewriter", "judge"]
    assert not (tmp_path / "docs" / "results" / "answers.md").exists()
    assert harness.connection.closed


def test_command_writes_the_report_on_request(harness: Harness, tmp_path: Path) -> None:
    assert run_command("--ids", "q1,q3", "--write-report") == 0
    report = (tmp_path / "docs" / "results" / "answers.md").read_text(encoding="utf-8")
    assert report.startswith("# Answer results") and "2 of them (a subset)" in report


def test_command_saves_progress_on_failure_and_resumes(
    harness: Harness, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    harness.fail_on_question = "Where is lost and found?"
    assert run_command() == ae.EXIT_MODEL_UNAVAILABLE
    err = capsys.readouterr().err
    assert "chat request timed out" in err and "2 finished question(s) are saved" in err
    (raw,) = harness.raw_files(tmp_path)
    assert "--resume " + str(raw) in err
    _, saved = ae.read_raw_results(raw)
    assert [r.id for r in saved] == ["q1", "q2"]

    harness.fail_on_question = None
    answers_before = sum(1 for call in harness.chat.calls if call["model"] == "answerer")
    assert run_command("--resume", str(raw), "--write-report") == 0
    answers_after = sum(1 for call in harness.chat.calls if call["model"] == "answerer")
    assert answers_after - answers_before == 1
    _, resumed = ae.read_raw_results(raw)
    assert [r.id for r in resumed] == ["q1", "q2", "q3"] and resumed[:2] == saved
    assert harness.raw_files(tmp_path) == [raw]
    report = (tmp_path / "docs" / "results" / "answers.md").read_text(encoding="utf-8")
    assert "answered every question" in report


def test_command_refuses_to_resume_a_run_with_other_models(
    harness: Harness, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "old.json"
    ae.write_raw_results(path, INFO, [record("q1")])
    assert run_command("--resume", str(path)) == ae.EXIT_USAGE
    assert "answer model 'qwen2.5:7b-instruct'" in capsys.readouterr().err


def test_command_reports_each_failure_with_its_exit_code(
    harness: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_command("--ids", "q404") == ae.EXIT_USAGE
    assert run_command("--limit", "0") == ae.EXIT_USAGE
    harness.golden_error = GoldenSetError("missing")
    assert run_command() == ae.EXIT_GOLDEN_SET_UNAVAILABLE
    harness.golden_error = None
    harness.setup_error = SetupError("cannot connect to PostgreSQL", 2)
    assert run_command() == 2
    harness.setup_error = None
    harness.check_error = EmbeddingError("cannot reach the embedding server")
    assert run_command() == ae.EXIT_MODEL_UNAVAILABLE
    harness.check_error = ChatError("does not have model 'judge'")
    assert run_command() == ae.EXIT_MODEL_UNAVAILABLE
    harness.check_error = None
    harness.database_error = psycopg.OperationalError("connection lost")
    assert run_command() == ae.EXIT_DATABASE_ERROR
    err = capsys.readouterr().err
    assert "unknown question id(s): q404" in err and "golden set" in err
    assert "cannot connect to PostgreSQL" in err and "embedding server" in err
    assert "does not have model 'judge'" in err
    assert "database error: connection lost" in err and "No question finished" in err


def test_evaluate_cli_registers_the_answers_command() -> None:
    args = evaluate_cli.build_parser().parse_args(
        ["answers", "--ids", "q001,q005", "--limit", "3", "--resume", "r.json", "--write-report"]
    )
    assert (args.command, args.ids, args.limit, args.write_report) == (
        "answers",
        "q001,q005",
        3,
        True,
    )
    assert args.resume == Path("r.json") and args.module == "campus_rag.evaluate.answers"
    assert SimpleNamespace(name="answers") in [
        SimpleNamespace(name=command.name) for command in evaluate_cli.COMMANDS
    ]
