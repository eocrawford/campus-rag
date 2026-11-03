"""Embedding strategy experiment: how much each embedding choice contributes to retrieval.

Each `Variant` changes one thing about the production setup (text embedded, task prefixes, model,
or dimensions). Experiment vectors live in `chunk_embeddings` under the model key
`experiment:<variant>`, so the production rows and queries are untouched, and `--drop` removes
them. Retrieval is an exact cosine scan, so index approximation does not blur the comparison.
See docs/plans/mvp-design.md decision 15.
"""

import argparse
import json
import math
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg

from campus_rag.config import EmbeddingSettings, find_repo_root, load_settings
from campus_rag.evaluate.golden import GoldenSetError, first_relevant_rank, load_golden_set
from campus_rag.evaluate.retrieval import (
    EXIT_DATABASE_ERROR,
    EXIT_GOLDEN_SET_UNAVAILABLE,
    EXIT_USAGE,
    OVERALL_GROUP,
    UNANSWERABLE,
    WARM_UP_QUERY,
    QuestionOutcome,
    TimedEmbedder,
    _fraction,
    _table,
    corpus_counts,
    evaluate_questions,
    git_commit_label,
    group_outcomes,
    summarize_latency,
    summarize_mode,
)
from campus_rag.index.embed import (
    MAX_BATCH_SIZE,
    Embedder,
    EmbeddingError,
    OpenAICompatibleEmbedder,
)
from campus_rag.index.migrate import _first_line
from campus_rag.index.store import chunks_needing_embeddings, save_embeddings
from campus_rag.retrieve.connect import EXIT_EMBEDDING_UNAVAILABLE, SetupError, open_connection
from campus_rag.retrieve.fusion import reciprocal_rank_fusion
from campus_rag.retrieve.keyword import keyword_search
from campus_rag.retrieve.models import RetrievedChunk, SearchFilters, SearchMode
from campus_rag.retrieve.search import RRF_K
from campus_rag.retrieve.vector import exact_vector_search

PRODUCTION_MODEL = "nomic-embed-text"
EXPERIMENT_PREFIX = "experiment:"
K = 10
CANDIDATE_LIMIT = 50  # per list, as in production hybrid search
MODES: tuple[SearchMode, ...] = ("vector", "hybrid")
REPORT_CUTOFFS = (1, 5, 10)
NOISE_THRESHOLD = 0.06  # decision 15c: two questions' worth of recall
DERIVE_BATCH_SIZE = 200
DESIGN_LINK = "../plans/mvp-design.md#15-embedding-strategy-experiment"
RUNBOOK_LINK = "../../runbooks/evaluate-retrieval.md#compare-embedding-strategies"
DEFAULT_PREFIXES = EmbeddingSettings()  # the production document and query prefixes

EXIT_VARIANT_UNAVAILABLE = 8


class ExperimentError(Exception):
    """A precondition of the experiment is not met; the message names the fix."""


# --- variants ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Variant:
    """One embedding configuration. Field defaults are the production configuration."""

    name: str
    model: str = PRODUCTION_MODEL
    body_only: bool = False  # embed the chunk text without its leading heading path line
    document_prefix: str = DEFAULT_PREFIXES.document_prefix
    query_prefix: str = DEFAULT_PREFIXES.query_prefix
    dimensions: int = 768  # size of the vector the model returns
    truncate_to: int | None = None  # keep the first N values of the source variant's vectors,
    derive_from: str | None = None  # then renormalize; makes no model calls
    production: bool = False  # the rows already exist under the production model key
    notes: str = ""

    def __post_init__(self) -> None:
        if (self.truncate_to is None) != (self.derive_from is None):
            raise ValueError(f"{self.name}: truncate_to and derive_from go together")
        if self.truncate_to is not None and not 1 <= self.truncate_to <= self.dimensions:
            raise ValueError(f"{self.name}: truncate_to must be within 1..{self.dimensions}")

    @property
    def key(self) -> str:
        """The `chunk_embeddings.model` value the variant's vectors are stored under."""
        return self.model if self.production else f"{EXPERIMENT_PREFIX}{self.name}"

    @property
    def stored_dimensions(self) -> int:
        return self.truncate_to or self.dimensions

    def document_text(self, content: str) -> str:
        """The text sent to the model for a chunk with this stored `content`."""
        return body_text(content) if self.body_only else content

    def embedder_settings(self, base: EmbeddingSettings) -> EmbeddingSettings:
        """`base` with this variant's model, dimensions, and prefixes."""
        return replace(
            base,
            model=self.model,
            dimensions=self.dimensions,
            document_prefix=self.document_prefix,
            query_prefix=self.query_prefix,
        )

    @property
    def text_label(self) -> str:
        return "Body only" if self.body_only else "Heading path + body"

    @property
    def prefix_label(self) -> str:
        return "Yes" if self.document_prefix or self.query_prefix else "None"

    @property
    def dimensions_label(self) -> str:
        if self.truncate_to is not None:
            return f"First {self.truncate_to} of {self.dimensions}, renormalized"
        return f"{self.dimensions:,}"


VARIANTS: tuple[Variant, ...] = (
    Variant("baseline", production=True, notes="Production rows, not re-embedded"),
    Variant("no-heading", body_only=True, notes="Heading path stripped from the embedded text"),
    Variant(
        "no-task-prefix",
        document_prefix="",
        query_prefix="",
        notes="Same text as the baseline without the nomic task prefixes",
    ),
    Variant(
        "bge-m3",
        model="bge-m3",
        document_prefix="",
        query_prefix="",
        dimensions=1024,
        notes="A second model; it uses no task prefixes",
    ),
    Variant(
        "nomic-256",
        truncate_to=256,
        derive_from="baseline",
        notes="Matryoshka truncation of the baseline vectors; no model calls",
    ),
)
VARIANTS_BY_NAME = {variant.name: variant for variant in VARIANTS}


def parse_variant_names(text: str | None) -> list[Variant]:
    """The variants named in a comma-separated list, with the baseline; all when `text` is None."""
    if text is None:
        return list(VARIANTS)
    names = [name.strip() for name in text.split(",") if name.strip()]
    unknown = [name for name in names if name not in VARIANTS_BY_NAME]
    if unknown or not names:
        known = ", ".join(VARIANTS_BY_NAME)
        raise ValueError(
            f"unknown variant(s) {', '.join(unknown) or '(none given)'}; known: {known}"
        )
    chosen = {name for name in names} | {"baseline"}
    return [variant for variant in VARIANTS if variant.name in chosen]


def body_text(content: str) -> str:
    """A chunk's text without its leading heading path line and the blank line after it."""
    _, separator, body = content.partition("\n\n")
    return body if separator else content


def truncate_and_normalize(vector: Sequence[float], dimensions: int) -> list[float]:
    """The first `dimensions` values of `vector`, scaled to unit length."""
    if not 1 <= dimensions <= len(vector):
        raise ValueError(f"cannot keep {dimensions} of {len(vector)} dimensions")
    head = list(vector[:dimensions])
    norm = math.sqrt(sum(x * x for x in head))
    if norm == 0.0:
        raise ValueError("the first values of the vector are all zero")
    return [x / norm for x in head]


def parse_vector(text: str) -> list[float]:
    """Parse pgvector's text form, for example '[0.1,0.2]'."""
    return [float(part) for part in text.strip().strip("[]").split(",")]


class TruncatingEmbedder:
    """Wraps an `Embedder` so queries come back truncated and renormalized."""

    def __init__(self, inner: Embedder, dimensions: int) -> None:
        self._inner = inner
        self.model = inner.model
        self.dimensions = dimensions

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [
            truncate_and_normalize(v, self.dimensions) for v in self._inner.embed_documents(texts)
        ]

    def embed_query(self, text: str) -> list[float]:
        return truncate_and_normalize(self._inner.embed_query(text), self.dimensions)


# --- storing vectors --------------------------------------------------------------------------


@dataclass(frozen=True)
class EmbedStats:
    seconds: float  # time spent in model calls (0 for derived variants' model calls)
    chunks_embedded: int  # chunks stored by the run that measured this
    chunks_total: int


def _require_experiment_key(variant: Variant) -> str:
    if variant.production or not variant.key.startswith(EXPERIMENT_PREFIX):
        raise ExperimentError(f"refusing to write rows for {variant.name!r}: not an experiment key")
    return variant.key


def embed_variant(
    conn: psycopg.Connection,
    variant: Variant,
    embedder: Embedder,
    *,
    progress: Callable[[str], None] = print,
    clock: Callable[[], float] = time.perf_counter,
) -> EmbedStats:
    """Embed the chunks that have no current vector under the variant's key, 32 per request."""
    key = _require_experiment_key(variant)
    total = _count_chunks(conn)
    pending = chunks_needing_embeddings(conn, key)
    seconds = 0.0
    for start in range(0, len(pending), MAX_BATCH_SIZE):
        batch = pending[start : start + MAX_BATCH_SIZE]
        began = clock()
        vectors = embedder.embed_documents([variant.document_text(text) for _, text, _ in batch])
        seconds += clock() - began
        save_embeddings(
            conn,
            key,
            variant.stored_dimensions,
            [
                (chunk_id, digest, vector)
                for (chunk_id, _, digest), vector in zip(batch, vectors, strict=True)
            ],
        )
        done = start + len(batch)
        progress(f"  {variant.name}: embedded {done}/{len(pending)} chunks ({seconds:.0f}s)")
    return EmbedStats(seconds, len(pending), total)


def derive_variant(
    conn: psycopg.Connection,
    variant: Variant,
    source: Variant,
    *,
    clock: Callable[[], float] = time.perf_counter,
) -> EmbedStats:
    """Store truncated, renormalized copies of `source`'s current vectors; no model calls."""
    key = _require_experiment_key(variant)
    assert variant.truncate_to is not None
    total = _count_chunks(conn)
    pending = [chunk_id for chunk_id, _, _ in chunks_needing_embeddings(conn, key)]
    began = clock()
    stored = 0
    for start in range(0, len(pending), DERIVE_BATCH_SIZE):
        ids = pending[start : start + DERIVE_BATCH_SIZE]
        rows = conn.execute(
            "SELECT e.chunk_id, e.content_hash, e.embedding::text FROM chunk_embeddings e "
            "JOIN chunks c ON c.id = e.chunk_id AND c.content_hash = e.content_hash "
            "WHERE e.model = %s AND e.chunk_id = ANY(%s) ORDER BY e.chunk_id",
            (source.key, ids),
        ).fetchall()
        if len(rows) != len(ids):
            raise ExperimentError(
                f"{source.name} has current vectors for only {stored + len(rows)} of "
                f"{len(pending)} chunks that {variant.name} needs; embed the corpus first"
            )
        save_embeddings(
            conn,
            key,
            variant.stored_dimensions,
            [
                (chunk_id, digest, truncate_and_normalize(parse_vector(text), variant.truncate_to))
                for chunk_id, digest, text in rows
            ],
        )
        stored += len(rows)
    return EmbedStats(clock() - began, len(pending), total)


def drop_experiment_rows(conn: psycopg.Connection) -> int:
    """Delete every `experiment:` embedding and return how many rows were removed."""
    with conn.transaction():
        cur = conn.execute(
            "DELETE FROM chunk_embeddings WHERE starts_with(model, %s)", (EXPERIMENT_PREFIX,)
        )
        return cur.rowcount


def _count_chunks(conn: psycopg.Connection) -> int:
    row = conn.execute("SELECT count(*) FROM chunks").fetchone()
    assert row is not None
    return row[0]


def missing_embeddings(conn: psycopg.Connection, variant: Variant) -> int:
    """How many chunks lack a current vector under the variant's key."""
    return len(chunks_needing_embeddings(conn, variant.key))


# --- searching --------------------------------------------------------------------------------


class VariantSearcher:
    """`Searcher` for vector and hybrid search over one variant's vectors by exact scan.

    Hybrid fuses the top 50 of the variant's vector list with the production BM25 keyword list,
    with reciprocal rank fusion, as production hybrid search does.
    """

    def __init__(self, conn: psycopg.Connection, embedder: Embedder, key: str) -> None:
        self._conn = conn
        self._embedder = embedder
        self._key = key

    def search(
        self,
        query: str,
        *,
        mode: SearchMode = "hybrid",
        k: int = K,
        filters: SearchFilters | None = None,
    ) -> list[RetrievedChunk]:
        if mode == "vector":
            return self._vector(query, k)
        if mode == "hybrid":
            limit = max(CANDIDATE_LIMIT, k)
            keyword = keyword_search(self._conn, query, limit=limit)
            vector = self._vector(query, limit)
            return reciprocal_rank_fusion(keyword=keyword, vector=vector, limit=k, k=RRF_K)
        raise ValueError(f"unsupported search mode {mode!r}")

    def _vector(self, query: str, limit: int) -> list[RetrievedChunk]:
        return exact_vector_search(
            self._conn,
            self._embedder.embed_query(query),
            model=self._key,
            dimensions=self._embedder.dimensions,
            limit=limit,
        )


@dataclass(frozen=True)
class VariantResult:
    variant: Variant
    outcomes: Sequence[QuestionOutcome]
    embed: EmbedStats | None  # None when no timing is known (the production baseline)
    query_embed_median_ms: float


def evaluate_variant(
    conn: psycopg.Connection,
    variant: Variant,
    embedder: Embedder,
    questions: Sequence[Any],
    embed: EmbedStats | None,
) -> VariantResult:
    """Search every answerable standalone question with `embedder`'s query vectors."""
    timed = TimedEmbedder(embedder)
    searcher = VariantSearcher(conn, timed, variant.key)
    for mode in MODES:
        searcher.search(WARM_UP_QUERY, mode=mode, k=K)
    outcomes = evaluate_questions(
        searcher, questions, first_relevant_rank, k=K, embedder=timed, modes=MODES
    )
    return VariantResult(
        variant, outcomes, embed, summarize_latency(outcomes, "vector").embed_median_ms
    )


def select_questions(questions: Sequence[Any]) -> list[Any]:
    """The answerable standalone questions, the set the retrieval evaluation scores."""
    return [q for q in questions if q.type != UNANSWERABLE and q.follow_up_of is None]


# --- tables and report ------------------------------------------------------------------------


def _metric_headers() -> list[str]:
    return [*(f"recall@{c}" for c in REPORT_CUTOFFS), f"MRR@{K}"]


def _metric_cells(outcomes: Sequence[QuestionOutcome], mode: str) -> list[str]:
    summary = summarize_mode(outcomes, mode, K)
    return [*(_fraction(summary.recall[c]) for c in REPORT_CUTOFFS), _fraction(summary.mrr)]


def variants_table(variants: Sequence[Variant]) -> str:
    rows = [
        [f"`{v.name}`", f"`{v.model}`", v.text_label, v.prefix_label, v.dimensions_label, v.notes]
        for v in variants
    ]
    return _table(
        ["Variant", "Model", "Text embedded", "Task prefixes", "Dimensions", "Notes"], rows
    )


def overall_table(results: Sequence[VariantResult], mode: str) -> str:
    rows = [
        [f"`{r.variant.name}`", len(r.outcomes), *_metric_cells(r.outcomes, mode)] for r in results
    ]
    return _table(["Variant", "Questions", *_metric_headers()], rows)


def by_type_table(results: Sequence[VariantResult], mode: str) -> str:
    rows = []
    for name, _ in group_outcomes(results[0].outcomes):
        if name == OVERALL_GROUP:
            continue
        for r in results:
            members = dict(group_outcomes(r.outcomes))[name]
            rows.append([name, f"`{r.variant.name}`", len(members), *_metric_cells(members, mode)])
    return _table(["Question type", "Variant", "Questions", *_metric_headers()], rows)


def timing_table(results: Sequence[VariantResult]) -> str:
    rows = []
    for r in results:
        rows.append([f"`{r.variant.name}`", _embed_time_cell(r), f"{r.query_embed_median_ms:.0f}"])
    return _table(["Variant", "Corpus embedding time", "Median query embedding ms"], rows)


def _embed_time_cell(result: VariantResult) -> str:
    stats = result.embed
    if result.variant.production:
        return "production vectors reused"
    if stats is None:
        return "not measured"
    if result.variant.derive_from is not None:
        return f"{stats.seconds:.1f} s, derived with no model calls"
    if stats.chunks_embedded == 0:
        return "not measured"
    minutes = f"{stats.seconds:.0f} s ({stats.seconds / 60:.1f} min)"
    if stats.chunks_embedded < stats.chunks_total:
        return f"{minutes} for {stats.chunks_embedded} of {stats.chunks_total} chunks"
    return minutes


def recall_at(result: VariantResult, mode: str, cutoff: int) -> float:
    return summarize_mode(result.outcomes, mode, K).recall[cutoff]


def what_this_shows(results: Sequence[VariantResult]) -> str:
    """Recall@1 and recall@5 differences from the baseline, each marked against the noise threshold.

    Built only from the measured numbers.
    """
    baseline = next(r for r in results if r.variant.production)
    others = [r for r in results if r is not baseline]
    questions = len(baseline.outcomes)
    paragraphs = []
    for mode in MODES:
        sentences = []
        for cutoff in (1, 5):
            base = recall_at(baseline, mode, cutoff)
            sentence = (
                f"In {mode} search the baseline's recall@{cutoff} is {base:.3f} "
                f"over {questions} questions."
            )
            if others:
                deltas = {r.variant.name: recall_at(r, mode, cutoff) - base for r in others}
                changes = ", ".join(f"`{name}` {delta:+.3f}" for name, delta in deltas.items())
                beyond = [f"`{n}`" for n, delta in deltas.items() if abs(delta) >= NOISE_THRESHOLD]
                if beyond:
                    verb = "differs" if len(beyond) == 1 else "differ"
                    verdict = f"Only {', '.join(beyond)} {verb} by {NOISE_THRESHOLD:.2f} or more."
                else:
                    verdict = (
                        f"None differs by {NOISE_THRESHOLD:.2f} or more, so none is "
                        "distinguishable from noise."
                    )
                sentence += f" The change from the baseline is {changes}. {verdict}"
            sentences.append(sentence)
        paragraphs.append(" ".join(sentences))
    return "\n\n".join(paragraphs)


@dataclass(frozen=True)
class RunInfo:
    generated_at: datetime
    git_commit: str
    documents: int
    chunks: int
    production_embeddings: int
    golden_questions: int
    scored_questions: int


def stdout_report(results: Sequence[VariantResult]) -> str:
    parts = []
    for mode in MODES:
        parts += [f"{mode.capitalize()} search, overall", overall_table(results, mode)]
    parts += ["Embedding time", timing_table(results)]
    return "\n\n".join(parts)


def render_report(info: RunInfo, results: Sequence[VariantResult]) -> str:
    """The committed Markdown summary. It holds metrics and variant names, never policy text."""
    one_question = 1 / info.scored_questions
    variants = [r.variant for r in results]
    sections = []
    for mode, title in (("vector", "Vector search"), ("hybrid", "Hybrid search")):
        sections.append(
            f"""## {title}

Overall, over the {info.scored_questions} standalone answerable questions.

{overall_table(results, mode)}

By question type.

{by_type_table(results, mode)}
"""
        )
    results_text = "\n".join(sections)
    return f"""# Embedding strategy results

Generated on {info.generated_at:%Y-%m-%d} at commit `{info.git_commit}`. The corpus has \
{info.documents:,} documents and {info.chunks:,} chunks. The golden set has \
{info.golden_questions} questions; the {info.scored_questions} standalone answerable ones are \
scored here, and each variant retrieves the top {K} chunks by exact cosine search. Hybrid search \
fuses the variant's top {CANDIDATE_LIMIT} vector results with the production BM25 keyword results \
by reciprocal rank fusion. See [decision 15]({DESIGN_LINK}) for the design and the \
[retrieval runbook]({RUNBOOK_LINK}) to repeat the run.

## How much to trust the differences

With {info.scored_questions} questions, one question moves recall by about {one_question:.3f}. \
Differences under {NOISE_THRESHOLD:.2f}, about two questions, are not meaningful.

## Variants

{variants_table(variants)}

{results_text}
## Embedding time

Corpus embedding time is the time in embedding requests on this CPU, in batches of \
{MAX_BATCH_SIZE}; query embedding time is the median over the scored questions.

{timing_table(results)}

## What this shows

{what_this_shows(results)}
"""


# --- output files -----------------------------------------------------------------------------


def results_path(directory: Path, generated_at: datetime) -> Path:
    return directory / f"embeddings-{generated_at.astimezone(UTC):%Y%m%dT%H%M%SZ}.json"


def write_raw_results(path: Path, info: RunInfo, results: Sequence[VariantResult]) -> None:
    """Write per-question results and embedding times as JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    document: dict[str, Any] = {
        "run": {**asdict(info), "generated_at": info.generated_at.isoformat()},
        "variants": {
            r.variant.name: {
                "definition": asdict(r.variant),
                "embed": asdict(r.embed) if r.embed is not None else None,
                "query_embed_median_ms": r.query_embed_median_ms,
                "questions": [asdict(outcome) for outcome in r.outcomes],
            }
            for r in results
        },
    }
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def load_previous_embed_stats(directory: Path) -> dict[str, EmbedStats]:
    """The newest recorded corpus embedding time of each variant, from earlier raw results."""
    stats: dict[str, EmbedStats] = {}
    for path in sorted(directory.glob("embeddings-*.json"), reverse=True):
        try:
            variants = json.loads(path.read_text(encoding="utf-8"))["variants"]
        except (OSError, ValueError, KeyError):
            continue
        for name, entry in variants.items():
            embed = entry.get("embed") if isinstance(entry, dict) else None
            if name in stats or not embed or not embed.get("chunks_embedded"):
                continue
            try:
                stats[name] = EmbedStats(
                    float(embed["seconds"]),
                    int(embed["chunks_embedded"]),
                    int(embed["chunks_total"]),
                )
            except (KeyError, TypeError, ValueError):
                continue
    return stats


# --- command ----------------------------------------------------------------------------------


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--variants",
        metavar="LIST",
        help=(
            "comma-separated variants to run, from "
            f"{', '.join(VARIANTS_BY_NAME)} (default: all); baseline always runs"
        ),
    )
    parser.add_argument(
        "--skip-embed",
        action="store_true",
        help="reuse the stored experiment vectors; send no document embedding requests",
    )
    parser.add_argument(
        "--write-report",
        action="store_true",
        help="also write the committed summary docs/results/embedding-strategies.md",
    )
    parser.add_argument(
        "--drop",
        action="store_true",
        help="delete the experiment vectors (model keys 'experiment:*') and exit",
    )


def _error(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)


def run_variants(
    conn: psycopg.Connection,
    variants: Sequence[Variant],
    questions: Sequence[Any],
    embedder_factory: Callable[[Variant], Embedder],
    *,
    skip_embed: bool,
    previous: dict[str, EmbedStats],
    progress: Callable[[str], None] = print,
) -> list[VariantResult]:
    """Embed (unless `skip_embed`), derive, and evaluate each variant in order."""
    results = []
    by_name = {variant.name: variant for variant in variants}
    for variant in variants:
        progress(f"{variant.name}: {variant.notes or variant.text_label}")
        stats: EmbedStats | None = previous.get(variant.name)
        if variant.derive_from is not None:
            source = by_name.get(variant.derive_from) or VARIANTS_BY_NAME[variant.derive_from]
            fresh = derive_variant(conn, variant, source)
            stats = fresh if fresh.chunks_embedded else stats
            query_embedder: Embedder = TruncatingEmbedder(
                embedder_factory(source), variant.stored_dimensions
            )
        else:
            query_embedder = embedder_factory(variant)
            if not variant.production and not skip_embed:
                fresh = embed_variant(conn, variant, query_embedder, progress=progress)
                stats = fresh if fresh.chunks_embedded else stats
        if variant.production:
            stats = None
        missing = missing_embeddings(conn, variant)
        if missing:
            raise ExperimentError(
                f"{variant.name} has no current vector for {missing} chunk(s); "
                "rerun without --skip-embed"
            )
        results.append(evaluate_variant(conn, variant, query_embedder, questions, stats))
        progress(f"{variant.name}: evaluated {len(results[-1].outcomes)} questions")
    return results


def run(args: argparse.Namespace) -> int:
    """Run the `embeddings` subcommand. Returns the process exit code."""
    if args.drop and (args.variants is not None or args.skip_embed or args.write_report):
        _error("--drop cannot be combined with other options")
        return EXIT_USAGE
    try:
        variants = parse_variant_names(args.variants)
        settings = load_settings()
    except ValueError as exc:
        _error(str(exc))
        return EXIT_USAGE
    if not args.drop and settings.embedding.model != PRODUCTION_MODEL:
        _error(
            f"the experiment baseline is {PRODUCTION_MODEL}, but CAMPUS_RAG_EMBEDDING_MODEL is "
            f"{settings.embedding.model}"
        )
        return EXIT_USAGE

    try:
        conn = open_connection(settings)
    except SetupError as exc:
        _error(str(exc))
        return exc.exit_code

    try:
        with conn:
            if args.drop:
                print(f"Dropped {drop_experiment_rows(conn)} experiment embedding rows.")
                return 0
            return _run_experiment(args, conn, settings.embedding, variants)
    except ExperimentError as exc:
        _error(str(exc))
        return EXIT_VARIANT_UNAVAILABLE
    except EmbeddingError as exc:
        _error(str(exc))
        return EXIT_EMBEDDING_UNAVAILABLE
    except psycopg.Error as exc:
        _error(f"database error: {_first_line(exc)}")
        return EXIT_DATABASE_ERROR


def _run_experiment(
    args: argparse.Namespace,
    conn: psycopg.Connection,
    embedding: EmbeddingSettings,
    variants: Sequence[Variant],
) -> int:
    try:
        questions = load_golden_set()
    except GoldenSetError as exc:
        _error(f"cannot load the golden set: {exc}")
        return EXIT_GOLDEN_SET_UNAVAILABLE
    scored = select_questions(questions)
    if not scored:
        _error("the golden set has no standalone answerable questions")
        return EXIT_GOLDEN_SET_UNAVAILABLE
    baseline = VARIANTS_BY_NAME["baseline"]
    if missing_embeddings(conn, baseline):
        raise ExperimentError(
            f"the corpus is not fully embedded with {PRODUCTION_MODEL}; "
            "run: uv run python -m campus_rag.ingest"
        )

    repo_root = find_repo_root() or Path.cwd()
    opened: list[OpenAICompatibleEmbedder] = []

    def make_embedder(variant: Variant) -> Embedder:
        embedder = OpenAICompatibleEmbedder(variant.embedder_settings(embedding))
        opened.append(embedder)
        embedder.check()
        return embedder

    try:
        results = run_variants(
            conn,
            variants,
            scored,
            make_embedder,
            skip_embed=args.skip_embed,
            previous=load_previous_embed_stats(repo_root / "eval-results"),
        )
    finally:
        for embedder in opened:
            embedder.close()

    documents, chunks, production_embeddings = corpus_counts(conn, PRODUCTION_MODEL)
    info = RunInfo(
        generated_at=datetime.now(UTC),
        git_commit=git_commit_label(repo_root),
        documents=documents,
        chunks=chunks,
        production_embeddings=production_embeddings,
        golden_questions=len(questions),
        scored_questions=len(scored),
    )
    raw_path = results_path(repo_root / "eval-results", info.generated_at)
    write_raw_results(raw_path, info, results)
    print(stdout_report(results))
    print(f"\nRaw results: {raw_path}")
    if args.write_report:
        report_path = repo_root / "docs" / "results" / "embedding-strategies.md"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(render_report(info, results), encoding="utf-8")
        print(f"Report: {report_path}")
    return 0
