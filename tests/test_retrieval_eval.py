"""Retrieval evaluation: metric math, grouping, tables, the report, and the command's exit codes."""

import argparse
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest

from campus_rag.evaluate import __main__ as evaluate_cli
from campus_rag.evaluate import retrieval as ev
from campus_rag.evaluate.golden import GoldenSetError
from campus_rag.index.embed import EmbeddingError
from campus_rag.retrieve.connect import SetupError
from campus_rag.retrieve.models import RetrievedChunk, SearchFilters, SearchMode


def outcome(
    question_id: str,
    ranks: tuple[int | None, int | None, int | None],
    *,
    type: str = "paraphrase",
    follow_up: bool = False,
    seconds: tuple[float, float, float] = (0.01, 0.1, 0.12),
) -> ev.QuestionOutcome:
    """An outcome with the given (keyword, vector, hybrid) ranks."""
    modes = {
        mode: ev.ModeOutcome(
            rank=rank,
            seconds=took,
            embed_seconds=0.0 if mode == "keyword" else took / 2,
            retrieved=(),
        )
        for mode, rank, took in zip(("keyword", "vector", "hybrid"), ranks, seconds, strict=True)
    }
    return ev.QuestionOutcome(id=question_id, type=type, follow_up=follow_up, modes=modes)


SAMPLE = [
    outcome("q1", (1, 1, 1)),
    outcome("q2", (None, 2, 2)),
    outcome("q3", (4, None, 3), type="exact_term"),
    outcome("q4", (None, None, None), type="exact_term"),
    outcome("q5", (2, 6, 1), follow_up=True),
]


# --- metric math -------------------------------------------------------------------------------


def test_recall_at_counts_ranks_within_k() -> None:
    ranks = [1, 3, None, 6, 10]
    assert ev.recall_at(ranks, 1) == pytest.approx(1 / 5)
    assert ev.recall_at(ranks, 3) == pytest.approx(2 / 5)
    assert ev.recall_at(ranks, 5) == pytest.approx(2 / 5)
    assert ev.recall_at(ranks, 10) == pytest.approx(4 / 5)


def test_mean_reciprocal_rank_ignores_misses_and_ranks_beyond_k() -> None:
    assert ev.mean_reciprocal_rank([1, 2, None, 4], 10) == pytest.approx((1 + 0.5 + 0 + 0.25) / 4)
    assert ev.mean_reciprocal_rank([1, 5, 12], 10) == pytest.approx((1 + 0.2 + 0) / 3)
    assert ev.mean_reciprocal_rank([1, 5], 3) == pytest.approx(0.5)
    assert ev.mean_reciprocal_rank([None, None], 10) == 0.0


def test_metrics_reject_an_empty_question_list() -> None:
    for function in (ev.recall_at, ev.mean_reciprocal_rank):
        with pytest.raises(ValueError, match="no questions"):
            function([], 5)


def test_percentile_uses_the_nearest_rank() -> None:
    values = [float(n) for n in range(1, 21)]
    assert ev.percentile(values, 0.95) == 19.0
    assert ev.percentile(values, 0.5) == 10.0
    assert ev.percentile(values, 1.0) == 20.0
    assert ev.percentile([7.0], 0.95) == 7.0
    assert ev.percentile([3.0, 1.0, 2.0], 0.95) == 3.0  # input order does not matter
    with pytest.raises(ValueError):
        ev.percentile([], 0.5)


def test_top_k_overlap_is_the_share_of_exact_ids_returned() -> None:
    assert ev.top_k_overlap([1, 2, 3, 4], [4, 3, 2, 1]) == 1.0
    assert ev.top_k_overlap([1, 2, 3, 4], [1, 2, 9, 8]) == 0.5
    assert ev.top_k_overlap([1, 2], []) == 0.0
    with pytest.raises(ValueError):
        ev.top_k_overlap([], [1])


def test_recall_cutoffs_always_include_k() -> None:
    assert ev.recall_cutoffs(10) == (1, 3, 5, 10)
    assert ev.recall_cutoffs(4) == (1, 3, 4)
    assert ev.recall_cutoffs(1) == (1,)
    assert ev.recall_cutoffs(20) == (1, 3, 5, 10, 20)


def test_summarize_mode_computes_recall_and_mrr() -> None:
    standalone = [o for o in SAMPLE if not o.follow_up]
    keyword = ev.summarize_mode(standalone, "keyword", 10)
    assert keyword.n == 4
    assert keyword.recall == {1: 0.25, 3: 0.25, 5: 0.5, 10: 0.5}
    assert keyword.mrr == pytest.approx((1 + 0.25) / 4)
    vector = ev.summarize_mode(standalone, "vector", 10)
    assert vector.recall[3] == pytest.approx(0.5)
    assert vector.mrr == pytest.approx((1 + 0.5) / 4)


def test_summarize_latency_reports_median_p95_and_embedding_time() -> None:
    outcomes = [
        outcome(f"q{n}", (1, 1, 1), seconds=(n / 1000, n / 100, n / 50)) for n in range(1, 21)
    ]
    keyword = ev.summarize_latency(outcomes, "keyword")
    assert keyword.n == 20
    assert keyword.median_ms == pytest.approx(10.5)
    assert keyword.p95_ms == pytest.approx(19.0)
    assert keyword.embed_median_ms == 0.0
    vector = ev.summarize_latency(outcomes, "vector")
    assert vector.embed_median_ms == pytest.approx(vector.median_ms / 2)


# --- grouping and tables -----------------------------------------------------------------------


def test_groups_keep_follow_ups_apart_from_standalone_questions() -> None:
    groups = dict(ev.group_outcomes(SAMPLE))
    assert list(groups) == [ev.OVERALL_GROUP, "paraphrase", "exact_term", ev.FOLLOW_UP_GROUP]
    assert [o.id for o in groups[ev.OVERALL_GROUP]] == ["q1", "q2", "q3", "q4"]
    assert [o.id for o in groups["paraphrase"]] == ["q1", "q2"]
    assert [o.id for o in groups["exact_term"]] == ["q3", "q4"]
    assert [o.id for o in groups[ev.FOLLOW_UP_GROUP]] == ["q5"]


def test_groups_omit_empty_follow_up_and_unknown_types_sort_last() -> None:
    outcomes = [outcome("a", (1, 1, 1), type="zeta"), outcome("b", (1, 1, 1), type="paraphrase")]
    assert [name for name, _ in ev.group_outcomes(outcomes)] == [
        ev.OVERALL_GROUP,
        "paraphrase",
        "zeta",
    ]


def test_metrics_table_scores_standalone_questions_only() -> None:
    lines = ev.metrics_table(SAMPLE, 10).splitlines()
    assert lines[0] == "| Mode | Questions | recall@1 | recall@3 | recall@5 | recall@10 | MRR@10 |"
    assert lines[2] == "| keyword | 4 | 0.250 | 0.250 | 0.500 | 0.500 | 0.312 |"
    assert [line.split("|")[1].strip() for line in lines[2:]] == ["keyword", "vector", "hybrid"]


def test_group_table_has_a_row_per_group_and_mode_including_follow_ups() -> None:
    lines = ev.group_table(SAMPLE, 10).splitlines()[2:]
    groups = [line.split("|")[1].strip() for line in lines]
    assert groups == ["paraphrase"] * 3 + ["exact_term"] * 3 + [ev.FOLLOW_UP_GROUP] * 3
    follow_up_keyword = lines[6]
    assert follow_up_keyword.split("|")[2].strip() == "keyword"
    assert follow_up_keyword.split("|")[3].strip() == "1"


def test_ranking_comparison_table_labels_each_ranking() -> None:
    baseline = [
        ev.QuestionOutcome(
            id=o.id,
            type=o.type,
            follow_up=o.follow_up,
            modes={m: o.modes[m] for m in ("keyword", "hybrid")},
        )
        for o in SAMPLE
    ]
    rows = ev.ranking_comparison_table(SAMPLE, baseline, 10).splitlines()[2:]
    assert [row.split("|")[1].strip() for row in rows] == [
        "keyword, BM25",
        "keyword, ts_rank_cd",
        "hybrid, BM25 keyword",
        "hybrid, ts_rank_cd keyword",
        "vector",
    ]


def test_latency_and_hnsw_tables() -> None:
    assert "| keyword | 5 | 10 | 10 | 0 |" in ev.latency_table(SAMPLE)
    hnsw = ev.HnswSummary(
        queries=40,
        k=10,
        exact_median_ms=17.2,
        rows=(
            ev.HnswRow(ef_search=40, mean_recall=0.9889, min_recall=0.9, median_ms=9.6),
            ev.HnswRow(ef_search=100, mean_recall=1.0, min_recall=1.0, median_ms=10.1),
        ),
    )
    table = ev.hnsw_table(hnsw)
    assert "| HNSW, ef_search = 40 | 0.989 | 0.900 | 10 |" in table
    assert "| Exact scan | 1.000 | 1.000 | 17 |" in table


def test_examples_show_one_question_per_pattern_in_golden_set_order() -> None:
    outcomes = [
        outcome("a", (1, 1, 1)),
        outcome("b", (2, None, 3)),
        outcome("c", (None, 2, 2)),
        outcome("d", (3, 2, 1)),
        outcome("e", (None, None, None)),
        outcome("f", (1, 1, 1)),
    ]
    chosen = ev.example_rows(outcomes)
    assert [(label, o.id) for label, o in chosen] == [
        ("every mode ranks it first", "a"),
        ("keyword finds it, vector does not", "b"),
        ("vector finds it, keyword does not", "c"),
        ("hybrid ranks it above the single modes", "d"),
        ("no mode finds it", "e"),
    ]
    table = ev.examples_table(outcomes, 10)
    assert (
        "| e | paraphrase | no mode finds it | not in top 10 | not in top 10 | not in top 10 |"
        in table
    )


def test_summary_sentence_names_the_leading_mode() -> None:
    sentence = ev.summary_sentence(SAMPLE, 10)
    assert sentence.startswith("recall@5 over 4 standalone questions: keyword 0.50")
    assert "(vector leads)" in sentence or "(hybrid leads)" in sentence or "tie" in sentence


# --- running the evaluation --------------------------------------------------------------------


class ScriptedSearcher:
    """Returns chunks whose ids come from a per-(query, mode) script."""

    def __init__(self, script: dict[tuple[str, str], list[int]]) -> None:
        self.script = script
        self.calls: list[tuple[str, str, int]] = []

    def search(
        self,
        query: str,
        *,
        mode: SearchMode = "hybrid",
        k: int = 8,
        filters: SearchFilters | None = None,
    ) -> list[RetrievedChunk]:
        self.calls.append((query, mode, k))
        return [
            RetrievedChunk(
                chunk_id=chunk_id,
                document_id=1,
                url=f"https://example.test/{chunk_id}",
                title="Title",
                heading_path=f"Heading {chunk_id}",
                content="text",
                token_count=1,
                score=1.0,
                rank=rank,
            )
            for rank, chunk_id in enumerate(self.script.get((query, mode), []), start=1)
        ]


def question(question_id: str, text: str, type: str = "paraphrase", follow_up_of=None):
    return SimpleNamespace(id=question_id, question=text, type=type, follow_up_of=follow_up_of)


def first_target_rank(item, chunks: Sequence[RetrievedChunk]) -> int | None:
    target = int(item.id[1:])  # question "q7" is answered by chunk 7
    for rank, chunk in enumerate(chunks, start=1):
        if chunk.chunk_id == target:
            return rank
    return None


def test_evaluate_questions_skips_unanswerable_and_marks_follow_ups() -> None:
    questions = [
        question("q1", "alpha"),
        question("q2", "beta", type="exact_term"),
        question("q3", "no answer here", type="unanswerable"),
        question("q4", "and gamma?", follow_up_of="q1"),
    ]
    searcher = ScriptedSearcher(
        {
            ("alpha", "keyword"): [9, 1],
            ("alpha", "vector"): [1],
            ("alpha", "hybrid"): [1, 9],
            ("beta", "keyword"): [2],
        }
    )
    outcomes = ev.evaluate_questions(searcher, questions, first_target_rank, k=7)
    assert [o.id for o in outcomes] == ["q1", "q2", "q4"]
    assert [o.follow_up for o in outcomes] == [False, False, True]
    assert outcomes[0].type == "paraphrase" and outcomes[1].type == "exact_term"
    assert {m: o.rank for m, o in outcomes[0].modes.items()} == {
        "keyword": 2,
        "vector": 1,
        "hybrid": 1,
    }
    assert {m: o.rank for m, o in outcomes[1].modes.items()} == {
        "keyword": 1,
        "vector": None,
        "hybrid": None,
    }
    assert outcomes[0].modes["keyword"].retrieved == (
        (9, "https://example.test/9", "Heading 9"),
        (1, "https://example.test/1", "Heading 1"),
    )
    assert ("no answer here", "keyword", 7) not in searcher.calls
    assert ("and gamma?", "hybrid", 7) in searcher.calls  # follow-ups are searched as written
    assert len(searcher.calls) == 9


def test_evaluate_questions_can_run_a_subset_of_modes_and_times_embedding() -> None:
    timed = ev.TimedEmbedder(SimpleNamespace(model="m", dimensions=3, embed_query=lambda t: [0.0]))
    script = {("alpha", "keyword"): [1], ("alpha", "hybrid"): [1]}

    class EmbeddingSearcher(ScriptedSearcher):
        def search(self, query, *, mode="hybrid", k=8, filters=None):
            if mode == "hybrid":
                timed.embed_query(query)
            return super().search(query, mode=mode, k=k, filters=filters)

    (result,) = ev.evaluate_questions(
        EmbeddingSearcher(script),
        [question("q1", "alpha")],
        first_target_rank,
        embedder=timed,
        modes=("keyword", "hybrid"),
    )
    assert set(result.modes) == {"keyword", "hybrid"}
    assert result.modes["keyword"].embed_seconds == 0.0
    assert 0 < result.modes["hybrid"].embed_seconds <= result.modes["hybrid"].seconds


# --- files -------------------------------------------------------------------------------------

INFO = ev.RunInfo(
    generated_at=datetime(2026, 11, 3, 20, 4, 43, tzinfo=UTC),
    git_commit="abc1234",
    embedding_model="nomic-embed-text",
    documents=247,
    chunks=1978,
    embeddings=1978,
    golden_questions=47,
    k=10,
)
HNSW = ev.HnswSummary(
    queries=47,
    k=10,
    exact_median_ms=17.0,
    rows=(ev.HnswRow(ef_search=100, mean_recall=0.998, min_recall=0.9, median_ms=10.0),),
)
EVALUATION = ev.Evaluation(outcomes=SAMPLE, ts_rank_cd_outcomes=SAMPLE, hnsw=HNSW)


def test_results_path_uses_a_utc_timestamp(tmp_path: Path) -> None:
    path = ev.results_path(tmp_path, INFO.generated_at)
    assert path == tmp_path / "retrieval-20261103T200443Z.json"


def test_raw_results_hold_per_question_ranks_and_run_details(tmp_path: Path) -> None:
    path = tmp_path / "out" / "retrieval.json"
    ev.write_raw_results(path, INFO, EVALUATION)
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["run"]["git_commit"] == "abc1234"
    assert document["run"]["generated_at"] == "2026-11-03T20:04:43+00:00"
    assert document["questions"][1]["id"] == "q2"
    assert document["questions"][1]["modes"]["vector"]["rank"] == 2
    assert document["hnsw"]["rows"][0]["ef_search"] == 100
    assert len(document["questions_ts_rank_cd_keyword"]) == 5


def test_report_has_the_required_sections_and_run_details() -> None:
    report = ev.render_report(INFO, EVALUATION)
    assert report.startswith("# Retrieval results\n")
    for heading in (
        "## Recall and MRR by mode",
        "## By question type",
        "## Keyword ranking",
        "## Latency",
        "## HNSW recall",
        "## Example questions",
    ):
        assert heading in report
    assert "2026-11-03" in report and "`abc1234`" in report
    assert "247 documents" in report and "1,978 chunks" in report
    assert "`nomic-embed-text`" in report and "47 questions" in report
    assert "| q1 |" in report or "| q2 |" in report
    assert "{" not in report  # every template field was filled


# --- command -----------------------------------------------------------------------------------


class FakeConnection:
    def __init__(self) -> None:
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self.closed = True


class Harness:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self.connection = FakeConnection()
        self.setup_error: SetupError | None = None
        self.check_error: EmbeddingError | None = None
        self.golden_error: GoldenSetError | None = None
        self.questions = [question("q1", "alpha"), question("q2", "beta", "unanswerable")]
        self.database_error: psycopg.Error | None = None
        harness = self

        class FakeEmbedder:
            model = "nomic-embed-text"
            dimensions = 768

            def __init__(self, settings) -> None:
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc) -> None:
                pass

            def check(self) -> None:
                if harness.check_error:
                    raise harness.check_error

        class FakeSearcher:
            def __init__(self, conn, embedder, **options) -> None:
                self.options = options

            def search(self, query, *, mode="hybrid", k=8, filters=None):
                if harness.database_error:
                    raise harness.database_error
                return [RetrievedChunk(1, 1, "https://example.test/1", "T", "H", "c", 1, 1.0, 1)]

        def open_connection(settings):
            if harness.setup_error:
                raise harness.setup_error
            return harness.connection

        def load_golden():
            if harness.golden_error:
                raise harness.golden_error
            return harness.questions, first_target_rank

        monkeypatch.setattr(ev, "open_connection", open_connection)
        monkeypatch.setattr(ev, "OpenAICompatibleEmbedder", FakeEmbedder)
        monkeypatch.setattr(ev, "PolicySearcher", FakeSearcher)
        monkeypatch.setattr(ev, "_load_golden", load_golden)
        monkeypatch.setattr(ev, "measure_hnsw_recall", lambda *a, **k: HNSW)
        monkeypatch.setattr(ev, "corpus_counts", lambda conn, model: (247, 1978, 1978))
        monkeypatch.setattr(ev, "find_repo_root", lambda: tmp_path)
        monkeypatch.setattr(ev, "git_commit_label", lambda root: "abc1234")
        monkeypatch.setattr(ev, "load_settings", lambda: SimpleNamespace(embedding=object()))


def run_command(*argv: str) -> int:
    parser = argparse.ArgumentParser()
    ev.add_arguments(parser)
    return ev.run(parser.parse_args(list(argv)))


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Harness:
    return Harness(monkeypatch, tmp_path)


def test_command_prints_tables_and_writes_raw_results_only_by_default(
    harness: Harness, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_command() == 0
    output = capsys.readouterr().out
    assert "| Mode | Questions | recall@1" in output and "Raw results:" in output
    assert "HNSW, ef_search = 100" in output
    (raw,) = (tmp_path / "eval-results").glob("retrieval-*.json")
    assert json.loads(raw.read_text(encoding="utf-8"))["run"]["golden_questions"] == 2
    assert not (tmp_path / "docs" / "results" / "retrieval.md").exists()
    assert harness.connection.closed


def test_command_writes_the_report_on_request(harness: Harness, tmp_path: Path) -> None:
    assert run_command("--write-report", "--k", "5") == 0
    report = (tmp_path / "docs" / "results" / "retrieval.md").read_text(encoding="utf-8")
    assert report.startswith("# Retrieval results") and "recall@5" in report


def test_command_reports_each_failure_with_its_exit_code(
    harness: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    harness.setup_error = SetupError("cannot connect to PostgreSQL", 2)
    assert run_command() == 2
    harness.setup_error = SetupError("database schema is out of date", 3)
    assert run_command() == 3
    harness.setup_error = None
    harness.check_error = EmbeddingError("cannot reach the embedding server")
    assert run_command() == 7
    harness.check_error = None
    harness.golden_error = GoldenSetError("missing")
    assert run_command() == 5
    harness.golden_error = None
    harness.questions = [question("q1", "no answer", "unanswerable")]
    assert run_command() == 5
    harness.questions = [question("q1", "alpha")]
    harness.database_error = psycopg.OperationalError("connection lost")
    assert run_command() == 6
    assert run_command("--k", "0") == 1
    assert run_command("--k", "51") == 1
    err = capsys.readouterr().err
    assert "cannot connect to PostgreSQL" in err and "out of date" in err
    assert "cannot reach the embedding server" in err and "golden set" in err
    assert "database error: connection lost" in err


def test_evaluate_cli_dispatches_subcommands() -> None:
    args = evaluate_cli.build_parser().parse_args(["retrieval", "--k", "5", "--write-report"])
    assert (args.command, args.k, args.write_report) == ("retrieval", 5, True)
    assert args.module == "campus_rag.evaluate.retrieval"
    with pytest.raises(SystemExit):
        evaluate_cli.build_parser().parse_args([])
    assert any(command.name == "retrieval" for command in evaluate_cli.COMMANDS)
