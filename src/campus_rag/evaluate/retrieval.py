"""Retrieval evaluation: recall@k, MRR, latency, and HNSW recall for each search mode.

`evaluate_questions` runs every answerable golden question through each mode. The metric
functions are pure, and the golden set is reached only through `GoldenQuestion` (a protocol) and a
`RankFunction`, so the module is tested without the golden-set files.
"""

import argparse
import json
import math
import statistics
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import psycopg

from campus_rag.config import find_repo_root, load_settings
from campus_rag.evaluate.golden import GoldenSetError, first_relevant_rank, load_golden_set
from campus_rag.index.embed import Embedder, EmbeddingError, OpenAICompatibleEmbedder
from campus_rag.index.migrate import _first_line
from campus_rag.retrieve.connect import EXIT_EMBEDDING_UNAVAILABLE, SetupError, open_connection
from campus_rag.retrieve.models import SEARCH_MODES, RetrievedChunk, Searcher, SearchMode
from campus_rag.retrieve.search import MAX_K, PolicySearcher
from campus_rag.retrieve.vector import exact_vector_search, vector_search

RECALL_CUTOFFS = (1, 3, 5, 10)
UNANSWERABLE = "unanswerable"
FOLLOW_UP_GROUP = "follow-up (not rewritten)"
OVERALL_GROUP = "all standalone"
DEFAULT_EF_SEARCHES = (40, 100)  # pgvector's default, then the value retrieval uses
EXAMPLE_COUNT = 5
WARM_UP_QUERY = "warm up the connection and the embedding model"

EXIT_USAGE = 1
EXIT_GOLDEN_SET_UNAVAILABLE = 5
EXIT_DATABASE_ERROR = 6


class GoldenQuestion(Protocol):
    id: str
    question: str
    type: str
    follow_up_of: str | None


# Returns the 1-based rank of the first chunk that matches the question's expected target.
RankFunction = Callable[[Any, Sequence[RetrievedChunk]], int | None]


@dataclass(frozen=True)
class ModeOutcome:
    """One search mode's result for one question."""

    rank: int | None  # 1-based rank of the first relevant chunk in the top k; None if absent
    seconds: float  # wall time of the whole search, including embedding the query
    embed_seconds: float  # the part spent embedding the query (0 for keyword search)
    retrieved: tuple[tuple[int, str, str], ...]  # (chunk_id, url, heading_path)


@dataclass(frozen=True)
class QuestionOutcome:
    id: str
    type: str
    follow_up: bool
    modes: Mapping[str, ModeOutcome]


@dataclass(frozen=True)
class ModeSummary:
    n: int
    recall: Mapping[int, float]
    mrr: float


@dataclass(frozen=True)
class LatencySummary:
    n: int
    median_ms: float
    p95_ms: float
    embed_median_ms: float


@dataclass(frozen=True)
class HnswRow:
    ef_search: int
    mean_recall: float  # share of the exact top k that the index also returned, averaged
    min_recall: float
    median_ms: float  # search time without embedding the query


@dataclass(frozen=True)
class HnswSummary:
    queries: int
    k: int
    exact_median_ms: float
    rows: tuple[HnswRow, ...]


# --- metrics ----------------------------------------------------------------------------------


def recall_at(ranks: Sequence[int | None], k: int) -> float:
    """Share of questions whose first relevant chunk ranks within the top `k`."""
    if not ranks:
        raise ValueError("no questions to score")
    return sum(1 for rank in ranks if rank is not None and rank <= k) / len(ranks)


def mean_reciprocal_rank(ranks: Sequence[int | None], k: int) -> float:
    """Mean of 1 / rank over questions, counting ranks beyond `k` or missing as 0."""
    if not ranks:
        raise ValueError("no questions to score")
    return sum(1.0 / rank for rank in ranks if rank is not None and rank <= k) / len(ranks)


def percentile(values: Sequence[float], fraction: float) -> float:
    """Nearest-rank percentile: the smallest value with `fraction` of the values at or below it."""
    if not values:
        raise ValueError("no values")
    ordered = sorted(values)
    return ordered[max(math.ceil(fraction * len(ordered)), 1) - 1]


def top_k_overlap(exact_ids: Sequence[int], approximate_ids: Sequence[int]) -> float:
    """Share of the exact top-k chunk ids that the approximate search also returned."""
    if not exact_ids:
        raise ValueError("the exact result is empty")
    return len(set(exact_ids) & set(approximate_ids)) / len(exact_ids)


def recall_cutoffs(k: int) -> tuple[int, ...]:
    return tuple(sorted({cutoff for cutoff in RECALL_CUTOFFS if cutoff <= k} | {k}))


def summarize_mode(outcomes: Sequence[QuestionOutcome], mode: str, k: int) -> ModeSummary:
    ranks = [outcome.modes[mode].rank for outcome in outcomes]
    return ModeSummary(
        n=len(ranks),
        recall={cutoff: recall_at(ranks, cutoff) for cutoff in recall_cutoffs(k)},
        mrr=mean_reciprocal_rank(ranks, k),
    )


def summarize_latency(outcomes: Sequence[QuestionOutcome], mode: str) -> LatencySummary:
    totals = [outcome.modes[mode].seconds * 1000 for outcome in outcomes]
    embeds = [outcome.modes[mode].embed_seconds * 1000 for outcome in outcomes]
    return LatencySummary(
        n=len(totals),
        median_ms=statistics.median(totals),
        p95_ms=percentile(totals, 0.95),
        embed_median_ms=statistics.median(embeds),
    )


def group_outcomes(outcomes: Sequence[QuestionOutcome]) -> list[tuple[str, list[QuestionOutcome]]]:
    """Group answerable questions: all standalone, each type, then follow-ups on their own.

    Follow-up questions are searched as written, without the rewrite that makes them standalone,
    so they get a separate group and stay out of the standalone groups.
    """
    standalone = [o for o in outcomes if not o.follow_up]
    types = sorted({o.type for o in standalone}, key=_type_order)
    groups = [(OVERALL_GROUP, standalone)]
    groups.extend((name, [o for o in standalone if o.type == name]) for name in types)
    groups.append((FOLLOW_UP_GROUP, [o for o in outcomes if o.follow_up]))
    return [(name, members) for name, members in groups if members]


def _type_order(name: str) -> tuple[int, str]:
    preferred = ("paraphrase", "exact_term")
    return (preferred.index(name) if name in preferred else len(preferred), name)


# --- running ----------------------------------------------------------------------------------


class TimedEmbedder:
    """Wraps an `Embedder` and records how long the last query embedding took."""

    def __init__(self, inner: Embedder) -> None:
        self._inner = inner
        self.model = inner.model
        self.dimensions = inner.dimensions
        self.last_query_seconds = 0.0

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return self._inner.embed_documents(texts)

    def embed_query(self, text: str) -> list[float]:
        start = time.perf_counter()
        try:
            return self._inner.embed_query(text)
        finally:
            self.last_query_seconds = time.perf_counter() - start


def evaluate_questions(
    searcher: Searcher,
    questions: Sequence[GoldenQuestion],
    rank_of_first_relevant: RankFunction,
    *,
    k: int = 10,
    embedder: TimedEmbedder | None = None,
    modes: Sequence[SearchMode] = SEARCH_MODES,
) -> list[QuestionOutcome]:
    """Search every answerable question in each mode and record the first relevant rank.

    Unanswerable questions have no target and are skipped. `embedder` must be the one the
    searcher uses, to attribute query-embedding time.
    """
    outcomes = []
    for question in questions:
        if question.type == UNANSWERABLE:
            continue
        results: dict[str, ModeOutcome] = {}
        for mode in modes:
            if embedder is not None:
                embedder.last_query_seconds = 0.0
            start = time.perf_counter()
            chunks = searcher.search(question.question, mode=mode, k=k)
            seconds = time.perf_counter() - start
            results[mode] = ModeOutcome(
                rank=rank_of_first_relevant(question, chunks),
                seconds=seconds,
                embed_seconds=embedder.last_query_seconds if embedder is not None else 0.0,
                retrieved=tuple((c.chunk_id, c.url, c.heading_path) for c in chunks),
            )
        outcomes.append(
            QuestionOutcome(
                id=question.id,
                type=question.type,
                follow_up=question.follow_up_of is not None,
                modes=results,
            )
        )
    return outcomes


def measure_hnsw_recall(
    conn: psycopg.Connection,
    embedder: Embedder,
    texts: Sequence[str],
    *,
    k: int = 10,
    ef_searches: Sequence[int] = DEFAULT_EF_SEARCHES,
) -> HnswSummary:
    """Compare the HNSW index with an exact scan on the same query vectors."""
    vectors = [embedder.embed_query(text) for text in texts]
    options = {"model": embedder.model, "dimensions": embedder.dimensions, "limit": k}

    def timed(search: Callable[[], list[RetrievedChunk]]) -> tuple[list[int], float]:
        start = time.perf_counter()
        chunks = search()
        return [c.chunk_id for c in chunks], (time.perf_counter() - start) * 1000

    exact = [timed(lambda v=v: exact_vector_search(conn, v, **options)) for v in vectors]
    rows = []
    for ef_search in ef_searches:
        approximate = [
            timed(lambda v=v, ef=ef_search: vector_search(conn, v, ef_search=ef, **options))
            for v in vectors
        ]
        overlaps = [top_k_overlap(e[0], a[0]) for e, a in zip(exact, approximate, strict=True)]
        rows.append(
            HnswRow(
                ef_search=ef_search,
                mean_recall=statistics.fmean(overlaps),
                min_recall=min(overlaps),
                median_ms=statistics.median(a[1] for a in approximate),
            )
        )
    return HnswSummary(
        queries=len(vectors),
        k=k,
        exact_median_ms=statistics.median(e[1] for e in exact),
        rows=tuple(rows),
    )


# --- tables -----------------------------------------------------------------------------------


def _table(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join("---" for _ in headers) + "|",
    ]
    lines.extend("| " + " | ".join(str(cell) for cell in row) + " |" for row in rows)
    return "\n".join(lines)


def _fraction(value: float) -> str:
    return f"{value:.3f}"


def metrics_table(outcomes: Sequence[QuestionOutcome], k: int) -> str:
    """Recall and MRR for each mode over the standalone answerable questions."""
    standalone = [o for o in outcomes if not o.follow_up]
    cutoffs = recall_cutoffs(k)
    headers = ["Mode", "Questions", *(f"recall@{c}" for c in cutoffs), f"MRR@{k}"]
    rows = []
    for mode in SEARCH_MODES:
        summary = summarize_mode(standalone, mode, k)
        rows.append(
            [
                mode,
                summary.n,
                *(_fraction(summary.recall[c]) for c in cutoffs),
                _fraction(summary.mrr),
            ]
        )
    return _table(headers, rows)


def group_table(outcomes: Sequence[QuestionOutcome], k: int) -> str:
    """Recall and MRR for each mode within each question group (type, then follow-ups)."""
    cutoffs = recall_cutoffs(k)
    headers = ["Group", "Mode", "Questions", *(f"recall@{c}" for c in cutoffs), f"MRR@{k}"]
    rows = []
    for name, members in group_outcomes(outcomes):
        if name == OVERALL_GROUP:
            continue
        for mode in SEARCH_MODES:
            summary = summarize_mode(members, mode, k)
            rows.append(
                [
                    name,
                    mode,
                    summary.n,
                    *(_fraction(summary.recall[c]) for c in cutoffs),
                    _fraction(summary.mrr),
                ]
            )
    return _table(headers, rows)


def ranking_comparison_table(
    outcomes: Sequence[QuestionOutcome], ts_rank_cd_outcomes: Sequence[QuestionOutcome], k: int
) -> str:
    """Keyword and hybrid search with each keyword ranking, plus vector search for reference."""
    cutoffs = recall_cutoffs(k)
    headers = ["Search", "Questions", *(f"recall@{c}" for c in cutoffs), f"MRR@{k}"]
    entries = [
        ("keyword, BM25", outcomes, "keyword"),
        ("keyword, ts_rank_cd", ts_rank_cd_outcomes, "keyword"),
        ("hybrid, BM25 keyword", outcomes, "hybrid"),
        ("hybrid, ts_rank_cd keyword", ts_rank_cd_outcomes, "hybrid"),
        ("vector", outcomes, "vector"),
    ]
    rows = []
    for label, source, mode in entries:
        standalone = [o for o in source if not o.follow_up and mode in o.modes]
        if not standalone:
            continue
        summary = summarize_mode(standalone, mode, k)
        rows.append(
            [
                label,
                summary.n,
                *(_fraction(summary.recall[c]) for c in cutoffs),
                _fraction(summary.mrr),
            ]
        )
    return _table(headers, rows)


def latency_table(outcomes: Sequence[QuestionOutcome]) -> str:
    rows = []
    for mode in SEARCH_MODES:
        latency = summarize_latency(outcomes, mode)
        rows.append(
            [
                mode,
                latency.n,
                f"{latency.median_ms:.0f}",
                f"{latency.p95_ms:.0f}",
                f"{latency.embed_median_ms:.0f}",
            ]
        )
    return _table(["Mode", "Searches", "Median ms", "p95 ms", "Median embedding ms"], rows)


def hnsw_table(hnsw: HnswSummary) -> str:
    rows = [
        [
            f"HNSW, ef_search = {row.ef_search}",
            _fraction(row.mean_recall),
            _fraction(row.min_recall),
            f"{row.median_ms:.0f}",
        ]
        for row in hnsw.rows
    ]
    rows.append(["Exact scan", "1.000", "1.000", f"{hnsw.exact_median_ms:.0f}"])
    return _table(["Search", f"Mean recall@{hnsw.k}", f"Lowest recall@{hnsw.k}", "Median ms"], rows)


def _hybrid_beats_single_modes(outcome: QuestionOutcome) -> bool:
    """Hybrid found the target and ranked it above each single mode that also found it."""
    hybrid = outcome.modes["hybrid"].rank
    singles = [outcome.modes[m].rank for m in ("keyword", "vector")]
    found = [rank for rank in singles if rank is not None]
    return hybrid is not None and bool(found) and all(hybrid < rank for rank in found)


def example_rows(outcomes: Sequence[QuestionOutcome]) -> list[tuple[str, QuestionOutcome]]:
    """Pick up to five questions that each show a different pattern, in golden-set order."""

    def found(outcome: QuestionOutcome, mode: str) -> bool:
        return outcome.modes[mode].rank is not None

    patterns: list[tuple[str, Callable[[QuestionOutcome], bool]]] = [
        (
            "every mode ranks it first",
            lambda o: all(o.modes[m].rank == 1 for m in SEARCH_MODES),
        ),
        (
            "keyword finds it, vector does not",
            lambda o: found(o, "keyword") and not found(o, "vector"),
        ),
        (
            "vector finds it, keyword does not",
            lambda o: found(o, "vector") and not found(o, "keyword"),
        ),
        ("hybrid ranks it above the single modes", _hybrid_beats_single_modes),
        ("no mode finds it", lambda o: not any(found(o, m) for m in SEARCH_MODES)),
    ]
    chosen: list[tuple[str, QuestionOutcome]] = []
    used: set[str] = set()
    for label, matches in patterns:
        for outcome in outcomes:
            if outcome.id not in used and matches(outcome):
                chosen.append((label, outcome))
                used.add(outcome.id)
                break
    return chosen[:EXAMPLE_COUNT]


def examples_table(outcomes: Sequence[QuestionOutcome], k: int) -> str:
    """Example rows from the standalone questions, whose misses reflect the search itself."""

    def cell(outcome: QuestionOutcome, mode: str) -> str:
        rank = outcome.modes[mode].rank
        return str(rank) if rank is not None else f"not in top {k}"

    standalone = [o for o in outcomes if not o.follow_up]
    rows = [
        [outcome.id, outcome.type, label, *(cell(outcome, mode) for mode in SEARCH_MODES)]
        for label, outcome in example_rows(standalone)
    ]
    return _table(["Question", "Type", "Pattern", *(f"{m} rank" for m in SEARCH_MODES)], rows)


def summary_sentence(outcomes: Sequence[QuestionOutcome], k: int) -> str:
    standalone = [o for o in outcomes if not o.follow_up]
    cutoff = 5 if k >= 5 else k
    scores = {mode: summarize_mode(standalone, mode, k).recall[cutoff] for mode in SEARCH_MODES}
    best = max(scores.values())
    leaders = [mode for mode in SEARCH_MODES if scores[mode] == best]
    detail = ", ".join(f"{mode} {scores[mode]:.2f}" for mode in SEARCH_MODES)
    verdict = "tie" if len(leaders) > 1 else f"{leaders[0]} leads"
    return f"recall@{cutoff} over {len(standalone)} standalone questions: {detail} ({verdict})."


def stdout_report(evaluation: "Evaluation", k: int) -> str:
    return "\n\n".join(
        [
            f"Retrieval metrics at k = {k}",
            metrics_table(evaluation.outcomes, k),
            "By question type",
            group_table(evaluation.outcomes, k),
            "Keyword ranking: BM25 versus ts_rank_cd",
            ranking_comparison_table(evaluation.outcomes, evaluation.ts_rank_cd_outcomes, k),
            "Latency per search",
            latency_table(evaluation.outcomes),
            "HNSW index versus exact scan",
            hnsw_table(evaluation.hnsw),
        ]
    )


# --- output files -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Evaluation:
    outcomes: Sequence[QuestionOutcome]  # keyword, vector, and hybrid with the default ranking
    ts_rank_cd_outcomes: Sequence[QuestionOutcome]  # keyword and hybrid with ts_rank_cd keyword
    hnsw: HnswSummary


@dataclass(frozen=True)
class RunInfo:
    generated_at: datetime
    git_commit: str
    embedding_model: str
    documents: int
    chunks: int
    embeddings: int
    golden_questions: int
    k: int


def git_commit_label(repo_root: Path) -> str:
    """Short commit sha, with '+uncommitted' when the working tree has changes."""
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return f"{sha}+uncommitted" if dirty else sha


def corpus_counts(conn: psycopg.Connection, model: str) -> tuple[int, int, int]:
    """(documents, chunks, embeddings from `model`)."""
    row = conn.execute(
        "SELECT (SELECT count(*) FROM documents), (SELECT count(*) FROM chunks), "
        "(SELECT count(*) FROM chunk_embeddings WHERE model = %s)",
        (model,),
    ).fetchone()
    assert row is not None
    return row[0], row[1], row[2]


def results_path(directory: Path, generated_at: datetime) -> Path:
    return directory / f"retrieval-{generated_at.astimezone(UTC):%Y%m%dT%H%M%SZ}.json"


def write_raw_results(path: Path, info: RunInfo, evaluation: Evaluation) -> None:
    """Write per-question results (ranks, timings, retrieved chunk ids) as JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    document: dict[str, Any] = {
        "run": {**asdict(info), "generated_at": info.generated_at.isoformat()},
        "hnsw": asdict(evaluation.hnsw),
        "questions": [asdict(outcome) for outcome in evaluation.outcomes],
        "questions_ts_rank_cd_keyword": [asdict(o) for o in evaluation.ts_rank_cd_outcomes],
    }
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def render_report(info: RunInfo, evaluation: Evaluation) -> str:
    """The committed Markdown summary. It holds ranks and question ids only, never policy text."""
    k = info.k
    outcomes = evaluation.outcomes
    return f"""# Retrieval results

{summary_sentence(outcomes, k)}

Generated on {info.generated_at:%Y-%m-%d} at commit `{info.git_commit}`. The corpus has \
{info.documents:,} documents and {info.chunks:,} chunks, embedded with `{info.embedding_model}`. \
The golden set has {info.golden_questions} questions; the unanswerable ones have no target and \
are not scored here. Each mode retrieves the top {k} chunks, and a question counts as found when a \
chunk matching its expected target is among them. See [evaluation](../evaluation.md) for the \
metric definitions and the [retrieval runbook](../../runbooks/evaluate-retrieval.md) to repeat \
the run.

## Recall and MRR by mode

Standalone answerable questions, searched as written.

{metrics_table(outcomes, k)}

## By question type

Follow-up questions are searched as written, without a rewrite into a standalone query, so they \
are scored apart from the standalone groups. They are the baseline for the gain from query \
rewriting.

{group_table(outcomes, k)}

## Keyword ranking

Keyword search ranks chunks with BM25, computed in SQL. PostgreSQL's built-in `ts_rank_cd` has no \
inverse document frequency, so words found in most chunks ("university", "policy", "student") \
outweigh rare, distinctive ones. The table compares both rankings in keyword and in hybrid \
search; vector search is unchanged. BM25 was adopted after the first measurement showed this \
gap, with the textbook constants `k1 = 1.2` and `b = 0.75` and no parameter search, so the \
golden set both revealed the problem and measures the fix.

{ranking_comparison_table(outcomes, evaluation.ts_rank_cd_outcomes, k)}

## Latency

Wall time per search on the local machine, from one pass after a warm-up query. Vector and \
hybrid searches include the call to the embedding server.

{latency_table(outcomes)}

## HNSW recall

The HNSW index is approximate. This table measures the share of an exact scan's top \
{evaluation.hnsw.k} chunks that the index also returns, for the {evaluation.hnsw.queries} \
golden-set questions. `ef_search` is the index's candidate list size: larger values are slower \
and more accurate.

{hnsw_table(evaluation.hnsw)}

## Example questions

One question for each pattern that occurs, with the rank of its first relevant chunk in each mode.

{examples_table(outcomes, k)}
"""


# --- command ----------------------------------------------------------------------------------


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--k",
        type=int,
        default=10,
        metavar="N",
        help=f"results retrieved per question, 1-{MAX_K} (default: 10)",
    )
    parser.add_argument(
        "--write-report",
        action="store_true",
        help="also write the committed summary docs/results/retrieval.md",
    )


def _load_golden() -> tuple[list[GoldenQuestion], RankFunction]:
    """The golden questions and the function that finds the first relevant chunk's rank."""
    return list(load_golden_set()), first_relevant_rank


def _error(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)


def run(args: argparse.Namespace) -> int:
    """Run the `retrieval` subcommand. Returns the process exit code."""
    k: int = args.k
    if not 1 <= k <= MAX_K:
        _error(f"--k must be between 1 and {MAX_K}, got {k}")
        return EXIT_USAGE
    try:
        settings = load_settings()
    except ValueError as exc:
        _error(str(exc))
        return EXIT_USAGE
    try:
        questions, rank_of_first_relevant = _load_golden()
    except GoldenSetError as exc:
        _error(f"cannot load the golden set: {exc}")
        return EXIT_GOLDEN_SET_UNAVAILABLE
    answerable = [q for q in questions if q.type != UNANSWERABLE and q.follow_up_of is None]
    if not answerable:
        _error("the golden set has no standalone answerable questions")
        return EXIT_GOLDEN_SET_UNAVAILABLE

    try:
        conn = open_connection(settings)
    except SetupError as exc:
        _error(str(exc))
        return exc.exit_code

    try:
        with conn, OpenAICompatibleEmbedder(settings.embedding) as embedder:
            try:
                embedder.check()
            except EmbeddingError as exc:
                _error(f"{exc}\nRetrieval evaluation embeds every question.")
                return EXIT_EMBEDDING_UNAVAILABLE
            timed = TimedEmbedder(embedder)
            searcher = PolicySearcher(conn, timed)
            baseline_searcher = PolicySearcher(conn, timed, keyword_ranking="ts_rank_cd")
            for mode in SEARCH_MODES:
                searcher.search(WARM_UP_QUERY, mode=mode, k=k)
            outcomes = evaluate_questions(
                searcher, questions, rank_of_first_relevant, k=k, embedder=timed
            )
            ts_rank_cd_outcomes = evaluate_questions(
                baseline_searcher,
                questions,
                rank_of_first_relevant,
                k=k,
                embedder=timed,
                modes=("keyword", "hybrid"),
            )
            hnsw = measure_hnsw_recall(conn, timed, [q.question for q in questions], k=k)
            documents, chunks, embeddings = corpus_counts(conn, embedder.model)
            model = embedder.model
    except EmbeddingError as exc:
        _error(str(exc))
        return EXIT_EMBEDDING_UNAVAILABLE
    except psycopg.Error as exc:
        _error(f"database error: {_first_line(exc)}")
        return EXIT_DATABASE_ERROR

    evaluation = Evaluation(outcomes, ts_rank_cd_outcomes, hnsw)
    repo_root = find_repo_root() or Path.cwd()
    info = RunInfo(
        generated_at=datetime.now(UTC),
        git_commit=git_commit_label(repo_root),
        embedding_model=model,
        documents=documents,
        chunks=chunks,
        embeddings=embeddings,
        golden_questions=len(questions),
        k=k,
    )
    raw_path = results_path(repo_root / "eval-results", info.generated_at)
    write_raw_results(raw_path, info, evaluation)
    print(stdout_report(evaluation, k))
    print(f"\nSummary: {summary_sentence(outcomes, k)}")
    print(f"Raw results: {raw_path}")
    if args.write_report:
        report_path = repo_root / "docs" / "results" / "retrieval.md"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(render_report(info, evaluation), encoding="utf-8")
        print(f"Report: {report_path}")
    return 0
