"""Embedding strategy experiment: variants, vector math, metrics, report, storage, command."""

import argparse
import json
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest
from fakes import FakeEmbedder
from test_embeddings_db import add_document

from campus_rag.evaluate import __main__ as evaluate_cli
from campus_rag.evaluate import embeddings as exp
from campus_rag.evaluate.golden import GoldenQuestion, GoldenTarget
from campus_rag.evaluate.retrieval import ModeOutcome, QuestionOutcome
from campus_rag.index.embed import EmbeddingError
from campus_rag.index.migrate import apply_migrations, discover_migrations
from campus_rag.index.store import count_embeddings, save_embeddings

# --- variants ---------------------------------------------------------------------------------


def test_body_text_strips_the_heading_line_and_blank_line() -> None:
    content = "Policy > 1. Part\n\nFirst paragraph.\n\nSecond paragraph."
    assert exp.body_text(content) == "First paragraph.\n\nSecond paragraph."


def test_body_text_keeps_content_without_a_heading_separator() -> None:
    assert exp.body_text("Only one line") == "Only one line"


def test_document_text_depends_on_body_only() -> None:
    content = "Heading\n\nBody."
    assert exp.Variant("a").document_text(content) == content
    assert exp.Variant("b", body_only=True).document_text(content) == "Body."


def test_variants_match_decision_15() -> None:
    by_name = exp.VARIANTS_BY_NAME
    assert list(by_name) == ["baseline", "no-heading", "no-task-prefix", "bge-m3", "nomic-256"]
    baseline = by_name["baseline"]
    assert baseline.production and baseline.key == "nomic-embed-text"
    assert (baseline.document_prefix, baseline.query_prefix) == (
        "search_document: ",
        "search_query: ",
    )
    assert by_name["no-heading"].body_only and by_name["no-heading"].key == "experiment:no-heading"
    assert by_name["no-task-prefix"].prefix_label == "None"
    bge = by_name["bge-m3"]
    assert (bge.model, bge.dimensions, bge.stored_dimensions) == ("bge-m3", 1024, 1024)
    assert (bge.document_prefix, bge.query_prefix) == ("", "")
    nomic_256 = by_name["nomic-256"]
    assert (nomic_256.truncate_to, nomic_256.derive_from) == (256, "baseline")
    assert nomic_256.stored_dimensions == 256
    assert nomic_256.dimensions_label == "First 256 of 768, renormalized"


def test_embedder_settings_carry_the_variant_model_dimensions_and_prefixes() -> None:
    from campus_rag.config import EmbeddingSettings

    base = EmbeddingSettings(base_url="http://example.test/v1", api_key="secret")
    settings = exp.VARIANTS_BY_NAME["bge-m3"].embedder_settings(base)
    assert (settings.model, settings.dimensions) == ("bge-m3", 1024)
    assert (settings.document_prefix, settings.query_prefix) == ("", "")
    assert settings.base_url == "http://example.test/v1" and settings.api_key == "secret"


def test_variant_rejects_inconsistent_truncation() -> None:
    with pytest.raises(ValueError, match="go together"):
        exp.Variant("x", truncate_to=4)
    with pytest.raises(ValueError, match="within"):
        exp.Variant("x", truncate_to=999, derive_from="baseline")


def test_parse_variant_names() -> None:
    assert [v.name for v in exp.parse_variant_names(None)] == list(exp.VARIANTS_BY_NAME)
    assert [v.name for v in exp.parse_variant_names("bge-m3, no-heading")] == [
        "baseline",
        "no-heading",
        "bge-m3",
    ]
    with pytest.raises(ValueError, match="unknown variant.*nope"):
        exp.parse_variant_names("no-heading,nope")
    with pytest.raises(ValueError):
        exp.parse_variant_names(" , ")


# --- truncation -------------------------------------------------------------------------------


def test_truncate_and_normalize_keeps_the_head_at_unit_length() -> None:
    assert exp.truncate_and_normalize([3.0, 4.0, 12.0], 2) == pytest.approx([0.6, 0.8])
    assert exp.truncate_and_normalize([2.0, 0.0], 2) == pytest.approx([1.0, 0.0])


def test_truncate_and_normalize_rejects_bad_input() -> None:
    with pytest.raises(ValueError):
        exp.truncate_and_normalize([1.0, 2.0], 3)
    with pytest.raises(ValueError):
        exp.truncate_and_normalize([1.0, 2.0], 0)
    with pytest.raises(ValueError, match="zero"):
        exp.truncate_and_normalize([0.0, 0.0, 5.0], 2)


def test_parse_vector_reads_pgvector_text() -> None:
    assert exp.parse_vector("[0.5,-1,2.25]") == [0.5, -1.0, 2.25]


def test_truncating_embedder_shortens_queries() -> None:
    inner = FakeEmbedder(dimensions=8)
    embedder = exp.TruncatingEmbedder(inner, 3)
    vector = embedder.embed_query("hello")
    assert embedder.dimensions == 3 and len(vector) == 3
    assert sum(x * x for x in vector) == pytest.approx(1.0)
    expected = exp.truncate_and_normalize(inner.embed_query("hello"), 3)
    assert vector == pytest.approx(expected)


# --- metrics and report -----------------------------------------------------------------------


def outcome(
    question_id: str, vector: int | None, hybrid: int | None, *, type: str = "paraphrase"
) -> QuestionOutcome:
    return QuestionOutcome(
        id=question_id,
        type=type,
        follow_up=False,
        modes={
            "vector": ModeOutcome(vector, 0.02, 0.01, ()),
            "hybrid": ModeOutcome(hybrid, 0.03, 0.01, ()),
        },
    )


def result(
    variant: exp.Variant,
    ranks: Sequence[tuple[int | None, int | None]],
    embed: exp.EmbedStats | None = None,
) -> exp.VariantResult:
    types = ["paraphrase", "paraphrase", "exact_term", "exact_term"]
    outcomes = [
        outcome(f"q{i}", v, h, type=types[i % 4]) for i, (v, h) in enumerate(ranks, start=1)
    ]
    return exp.VariantResult(variant, outcomes, embed, query_embed_median_ms=12.0)


BASELINE = exp.VARIANTS_BY_NAME["baseline"]
NO_HEADING = exp.VARIANTS_BY_NAME["no-heading"]


def sample_results() -> list[exp.VariantResult]:
    base = result(BASELINE, [(1, 1), (2, 1), (6, 3), (None, 2)])
    other = result(
        NO_HEADING,
        [(1, 1), (None, 2), (6, 3), (None, None)],
        exp.EmbedStats(seconds=300.0, chunks_embedded=10, chunks_total=10),
    )
    return [base, other]


def test_overall_table_scores_recall_and_mrr() -> None:
    table = exp.overall_table(sample_results(), "vector")
    assert "| `baseline` | 4 | 0.250 | 0.500 | 0.750 | 0.417 |" in table
    assert "| `no-heading` | 4 | 0.250 | 0.250 | 0.500 | 0.292 |" in table
    assert "recall@1 | recall@5 | recall@10 | MRR@10" in table


def test_by_type_table_groups_questions_by_type() -> None:
    table = exp.by_type_table(sample_results(), "hybrid")
    assert "| paraphrase | `baseline` | 2 | 0.500 | 1.000 | 1.000 | 0.750 |" in table
    assert "| exact_term | `baseline` | 2 | 0.500 | 1.000 | 1.000 | 0.667 |" in table
    assert "| exact_term | `no-heading` | 2 | 0.000 | 1.000 | 1.000 | 0.417 |" in table


def test_what_this_shows_marks_differences_against_the_noise_threshold() -> None:
    text = exp.what_this_shows(sample_results())
    assert "baseline's recall@5 is 0.500 over 4 questions" in text
    assert "In vector search the baseline's recall@1 is 0.250" in text
    assert "In hybrid search the baseline's recall@1 is 0.500" in text
    assert "`no-heading` -0.250" in text
    assert "Only `no-heading` differs by 0.06 or more." in text

    near = result(NO_HEADING, [(1, 1), (2, 1), (6, 3), (None, 2)])
    quiet = exp.what_this_shows([result(BASELINE, [(1, 1), (2, 1), (6, 3), (None, 2)]), near])
    assert "None differs by 0.06 or more" in quiet
    assert "`no-heading` +0.000" in quiet


def test_what_this_shows_with_only_the_baseline() -> None:
    text = exp.what_this_shows(sample_results()[:1])
    assert "change from the baseline" not in text


def test_timing_table_describes_each_kind_of_embedding_time() -> None:
    derived = exp.VARIANTS_BY_NAME["nomic-256"]
    partial = result(
        exp.VARIANTS_BY_NAME["bge-m3"],
        [(1, 1)],
        exp.EmbedStats(seconds=90.0, chunks_embedded=5, chunks_total=10),
    )
    table = exp.timing_table(
        [
            *sample_results(),
            result(derived, [(1, 1)], exp.EmbedStats(0.4, 10, 10)),
            partial,
            result(exp.VARIANTS_BY_NAME["no-task-prefix"], [(1, 1)], None),
        ]
    )
    assert "| `baseline` | production vectors reused | 12 |" in table
    assert "| `no-heading` | 300 s (5.0 min) | 12 |" in table
    assert "| `nomic-256` | 0.4 s, derived with no model calls | 12 |" in table
    assert "| `bge-m3` | 90 s (1.5 min) for 5 of 10 chunks | 12 |" in table
    assert "| `no-task-prefix` | not measured | 12 |" in table


def run_info() -> exp.RunInfo:
    return exp.RunInfo(
        generated_at=datetime(2026, 11, 3, 20, 0, tzinfo=UTC),
        git_commit="abc1234+uncommitted",
        documents=70,
        chunks=1978,
        production_embeddings=1978,
        golden_questions=45,
        scored_questions=36,
    )


def test_render_report_states_the_noise_caveat_and_every_section() -> None:
    report = exp.render_report(run_info(), sample_results())
    assert report.startswith("# Embedding strategy results")
    assert "2026-11-03" in report and "`abc1234+uncommitted`" in report
    assert "1,978 chunks" in report and "36 standalone answerable questions" in report
    assert "one question moves recall by about 0.028" in report
    assert "Differences under 0.06" in report and "are not meaningful" in report
    for heading in (
        "## Variants",
        "## Vector search",
        "## Hybrid search",
        "## Embedding time",
        "## What this shows",
    ):
        assert heading in report
    assert "(../plans/mvp-design.md#15-embedding-strategy-experiment)" in report
    assert "Heading path + body" in report and "Body only" in report


def test_raw_results_round_trip_embed_times(tmp_path: Path) -> None:
    info = run_info()
    path = exp.results_path(tmp_path, info.generated_at)
    assert path.name == "embeddings-20261103T200000Z.json"
    exp.write_raw_results(path, info, sample_results())
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["run"]["git_commit"] == "abc1234+uncommitted"
    assert document["variants"]["no-heading"]["embed"]["seconds"] == 300.0
    assert document["variants"]["baseline"]["embed"] is None
    assert len(document["variants"]["baseline"]["questions"]) == 4

    stats = exp.load_previous_embed_stats(tmp_path)
    assert stats == {"no-heading": exp.EmbedStats(300.0, 10, 10)}


def test_previous_embed_stats_prefer_the_newest_run_and_skip_unmeasured(tmp_path: Path) -> None:
    def write(name: str, variants: dict[str, object]) -> None:
        (tmp_path / name).write_text(json.dumps({"variants": variants}), encoding="utf-8")

    entry = {"embed": {"seconds": 5.0, "chunks_embedded": 2, "chunks_total": 2}}
    newer = {"embed": {"seconds": 9.0, "chunks_embedded": 2, "chunks_total": 2}}
    write("embeddings-20261103T100000Z.json", {"a": entry, "b": entry})
    write(
        "embeddings-20261103T110000Z.json",
        {"a": newer, "b": {"embed": {"seconds": 0.0, "chunks_embedded": 0, "chunks_total": 2}}},
    )
    (tmp_path / "embeddings-20261103T120000Z.json").write_text("not json", encoding="utf-8")
    stats = exp.load_previous_embed_stats(tmp_path)
    assert stats == {"a": exp.EmbedStats(9.0, 2, 2), "b": exp.EmbedStats(5.0, 2, 2)}
    assert exp.load_previous_embed_stats(tmp_path / "missing") == {}


def test_select_questions_matches_the_retrieval_evaluation() -> None:
    questions = [
        golden("a", "paraphrase"),
        golden("b", "unanswerable"),
        golden("c", "exact_term", follow_up_of="a"),
        golden("d", "exact_term"),
    ]
    assert [q.id for q in exp.select_questions(questions)] == ["a", "d"]


# --- command line -----------------------------------------------------------------------------


def parse(*argv: str) -> argparse.Namespace:
    return evaluate_cli.build_parser().parse_args(["embeddings", *argv])


def test_embeddings_command_is_registered_and_parses_options() -> None:
    assert "embeddings" in {command.name for command in evaluate_cli.COMMANDS}
    defaults = parse()
    assert (defaults.variants, defaults.skip_embed, defaults.write_report, defaults.drop) == (
        None,
        False,
        False,
        False,
    )
    args = parse("--variants", "no-heading,bge-m3", "--skip-embed", "--write-report")
    assert args.variants == "no-heading,bge-m3" and args.skip_embed and args.write_report
    assert args.module == "campus_rag.evaluate.embeddings"
    assert parse("--drop").drop


def test_unknown_variant_and_conflicting_drop_exit_with_usage_errors(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert evaluate_cli.main(["embeddings", "--variants", "nope"]) == exp.EXIT_USAGE
    assert "unknown variant" in capsys.readouterr().err
    assert evaluate_cli.main(["embeddings", "--drop", "--write-report"]) == exp.EXIT_USAGE
    assert "--drop cannot be combined" in capsys.readouterr().err


# --- storage and evaluation on the throwaway database -------------------------------------------

PRODUCTION = "nomic-embed-text"
URL = "https://example.test/experiment"
TOPICS = ("alpha", "beta", "gamma")


def golden(
    question_id: str, type: str, *, question: str = "q", follow_up_of: str | None = None
) -> GoldenQuestion:
    target = GoldenTarget(URL, "Policy > 1. Part alpha")
    return GoldenQuestion(
        id=question_id,
        question=question,
        type=type,  # type: ignore[arg-type]
        expected=() if type == "unanswerable" else (target,),
        reference_answer="reference",
        follow_up_of=follow_up_of,
    )


class TopicEmbedder:
    """Embeds text as a vector that peaks at the topic word it contains."""

    def __init__(self, model: str, dimensions: int = 4) -> None:
        self.model = model
        self.dimensions = dimensions
        self.document_batches: list[list[str]] = []

    def _vector(self, text: str) -> list[float]:
        vector = [0.1] * self.dimensions
        for position, topic in enumerate(TOPICS):
            if topic in text.lower():
                vector[position] = 1.0
        return vector

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        self.document_batches.append(list(texts))
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


@pytest.fixture
def conn(db_url: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(db_url, autocommit=True) as connection:
        apply_migrations(connection, discover_migrations())
        yield connection


def small_variants() -> tuple[exp.Variant, exp.Variant, exp.Variant]:
    baseline = exp.Variant("baseline", model="prod-model", dimensions=4, production=True)
    alternate = exp.Variant("alt", model="alt-model", dimensions=4, body_only=True)
    half = exp.Variant(
        "half", model="prod-model", dimensions=4, truncate_to=2, derive_from="baseline"
    )
    return baseline, alternate, half


def seed_corpus(conn: psycopg.Connection) -> list[int]:
    chunk_ids = add_document(
        conn, URL, *[(f"1. Part {topic}", f"The {topic} rule") for topic in TOPICS]
    )
    embedder = TopicEmbedder("prod-model")
    rows = conn.execute("SELECT id, content, content_hash FROM chunks ORDER BY id").fetchall()
    vectors = embedder.embed_documents([row[1] for row in rows])
    save_embeddings(
        conn, "prod-model", 4, [(r[0], r[2], v) for r, v in zip(rows, vectors, strict=True)]
    )
    return chunk_ids


def experiment_rows(conn: psycopg.Connection) -> int:
    row = conn.execute(
        "SELECT count(*) FROM chunk_embeddings WHERE model LIKE 'experiment:%'"
    ).fetchone()
    assert row is not None
    return row[0]


@pytest.mark.db
def test_embed_variant_stores_body_only_vectors_under_the_experiment_key(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_corpus(conn)
    monkeypatch.setattr(exp, "MAX_BATCH_SIZE", 2)
    _, alternate, _ = small_variants()
    embedder = TopicEmbedder("alt-model")
    progress: list[str] = []

    stats = exp.embed_variant(conn, alternate, embedder, progress=progress.append)

    assert (stats.chunks_embedded, stats.chunks_total) == (3, 3)
    assert [len(batch) for batch in embedder.document_batches] == [2, 1]
    sent = [text for batch in embedder.document_batches for text in batch]
    assert all(text.startswith("The ") and "Policy" not in text for text in sent)
    assert len(progress) == 2 and "embedded 3/3 chunks" in progress[-1]
    assert count_embeddings(conn, "experiment:alt") == 3
    dims = conn.execute(
        "SELECT DISTINCT dimensions, vector_dims(embedding) FROM chunk_embeddings "
        "WHERE model = 'experiment:alt'"
    ).fetchall()
    assert dims == [(4, 4)]
    hashes = conn.execute(
        "SELECT count(*) FROM chunk_embeddings e JOIN chunks c ON c.id = e.chunk_id "
        "WHERE e.model = 'experiment:alt' AND e.content_hash = c.content_hash"
    ).fetchone()
    assert hashes == (3,)


@pytest.mark.db
def test_embed_variant_skips_current_rows_and_reembeds_changed_content(
    conn: psycopg.Connection,
) -> None:
    chunk_ids = seed_corpus(conn)
    _, alternate, _ = small_variants()
    exp.embed_variant(conn, alternate, TopicEmbedder("alt-model"), progress=lambda _: None)

    again = TopicEmbedder("alt-model")
    stats = exp.embed_variant(conn, alternate, again, progress=lambda _: None)
    assert (stats.chunks_embedded, again.document_batches) == (0, [])

    conn.execute("UPDATE chunks SET content_hash = 'changed' WHERE id = %s", (chunk_ids[1],))
    refreshed = TopicEmbedder("alt-model")
    stats = exp.embed_variant(conn, alternate, refreshed, progress=lambda _: None)
    assert stats.chunks_embedded == 1 and len(refreshed.document_batches[0]) == 1


@pytest.mark.db
def test_embed_variant_refuses_to_write_production_rows(conn: psycopg.Connection) -> None:
    seed_corpus(conn)
    baseline, _, _ = small_variants()
    before = count_embeddings(conn, "prod-model")
    with pytest.raises(exp.ExperimentError, match="not an experiment key"):
        exp.embed_variant(conn, baseline, TopicEmbedder("prod-model"), progress=lambda _: None)
    assert count_embeddings(conn, "prod-model") == before


@pytest.mark.db
def test_failed_embedding_keeps_completed_batches(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_corpus(conn)
    monkeypatch.setattr(exp, "MAX_BATCH_SIZE", 2)
    _, alternate, _ = small_variants()
    flaky = FakeEmbedder("alt-model", dimensions=4)
    flaky.fail_after_batches = 1
    with pytest.raises(EmbeddingError):
        exp.embed_variant(conn, alternate, flaky, progress=lambda _: None)
    assert count_embeddings(conn, "experiment:alt") == 2
    stats = exp.embed_variant(conn, alternate, TopicEmbedder("alt-model"), progress=lambda _: None)
    assert stats.chunks_embedded == 1


@pytest.mark.db
def test_derive_variant_truncates_and_renormalizes_source_vectors(
    conn: psycopg.Connection,
) -> None:
    seed_corpus(conn)
    baseline, _, half = small_variants()

    stats = exp.derive_variant(conn, half, baseline)

    assert (stats.chunks_embedded, stats.chunks_total) == (3, 3)
    rows = conn.execute(
        "SELECT dimensions, vector_dims(embedding), embedding::text FROM chunk_embeddings "
        "WHERE model = 'experiment:half'"
    ).fetchall()
    assert len(rows) == 3
    for dimensions, actual_dims, text in rows:
        vector = exp.parse_vector(text)
        assert (dimensions, actual_dims, len(vector)) == (2, 2, 2)
        assert sum(x * x for x in vector) == pytest.approx(1.0, abs=1e-6)
    assert exp.derive_variant(conn, half, baseline).chunks_embedded == 0


@pytest.mark.db
def test_derive_variant_requires_current_source_vectors(conn: psycopg.Connection) -> None:
    seed_corpus(conn)
    baseline, _, half = small_variants()
    conn.execute("DELETE FROM chunk_embeddings WHERE model = 'prod-model'")
    with pytest.raises(exp.ExperimentError, match="embed the corpus first"):
        exp.derive_variant(conn, half, baseline)
    assert experiment_rows(conn) == 0


@pytest.mark.db
def test_drop_deletes_only_experiment_rows(conn: psycopg.Connection) -> None:
    seed_corpus(conn)
    _, alternate, half = small_variants()
    baseline, _, _ = small_variants()
    exp.embed_variant(conn, alternate, TopicEmbedder("alt-model"), progress=lambda _: None)
    exp.derive_variant(conn, half, baseline)
    rows = conn.execute("SELECT id, content_hash FROM chunks ORDER BY id").fetchall()
    for model in ("experimentation", "my-experiment:x", "Experiment:upper"):
        save_embeddings(conn, model, 4, [(r[0], r[1], [1.0, 0.0, 0.0, 0.0]) for r in rows])
    other_before = conn.execute(
        "SELECT model, count(*) FROM chunk_embeddings WHERE model NOT LIKE 'experiment:%' "
        "GROUP BY model ORDER BY model"
    ).fetchall()
    assert experiment_rows(conn) == 6

    assert exp.drop_experiment_rows(conn) == 6

    assert experiment_rows(conn) == 0
    other_after = conn.execute(
        "SELECT model, count(*) FROM chunk_embeddings GROUP BY model ORDER BY model"
    ).fetchall()
    assert other_after == other_before
    assert exp.drop_experiment_rows(conn) == 0


@pytest.mark.db
def test_run_variants_embeds_derives_and_scores_each_variant(conn: psycopg.Connection) -> None:
    seed_corpus(conn)
    baseline, alternate, half = small_variants()
    questions = [
        golden("q1", "paraphrase", question="alpha question"),
        golden("q2", "unanswerable", question="alpha again"),
        golden("q3", "exact_term", question="alpha follow", follow_up_of="q1"),
    ]
    scored = exp.select_questions(questions)

    def factory(variant: exp.Variant) -> TopicEmbedder:
        return TopicEmbedder(variant.model, variant.dimensions)

    results = exp.run_variants(
        conn,
        [baseline, alternate, half],
        scored,
        factory,
        skip_embed=False,
        previous={"baseline": exp.EmbedStats(1.0, 1, 1)},
        progress=lambda _: None,
    )

    assert [r.variant.name for r in results] == ["baseline", "alt", "half"]
    for r in results:
        assert [o.id for o in r.outcomes] == ["q1"]
        assert r.outcomes[0].modes["vector"].rank == 1
        assert r.outcomes[0].modes["hybrid"].rank == 1
    assert results[0].embed is None
    assert results[1].embed is not None and results[1].embed.chunks_embedded == 3
    assert results[2].embed is not None and results[2].embed.chunks_embedded == 3
    assert count_embeddings(conn, "prod-model") == 3  # production rows untouched

    rerun = exp.run_variants(
        conn,
        [baseline, alternate, half],
        scored,
        factory,
        skip_embed=True,
        previous={"alt": results[1].embed},  # type: ignore[dict-item]
        progress=lambda _: None,
    )
    assert rerun[1].embed == results[1].embed


@pytest.mark.db
def test_skip_embed_fails_when_experiment_vectors_are_missing(conn: psycopg.Connection) -> None:
    seed_corpus(conn)
    baseline, alternate, _ = small_variants()
    with pytest.raises(exp.ExperimentError, match="rerun without --skip-embed"):
        exp.run_variants(
            conn,
            [baseline, alternate],
            [golden("q1", "paraphrase", question="alpha")],
            lambda v: TopicEmbedder(v.model, v.dimensions),
            skip_embed=True,
            previous={},
            progress=lambda _: None,
        )


@pytest.mark.db
def test_variant_searcher_hybrid_fuses_keyword_and_vector_lists(conn: psycopg.Connection) -> None:
    seed_corpus(conn)
    searcher = exp.VariantSearcher(conn, TopicEmbedder("prod-model"), "prod-model")
    vector = searcher.search("beta", mode="vector", k=3)
    assert [c.heading_path for c in vector][0].endswith("Part beta")
    assert all(c.vector_rank is not None for c in vector)
    hybrid = searcher.search("beta", mode="hybrid", k=3)
    assert hybrid[0].heading_path.endswith("Part beta")
    assert hybrid[0].keyword_rank == 1 and hybrid[0].vector_rank == 1
    with pytest.raises(ValueError, match="unsupported"):
        searcher.search("beta", mode="keyword", k=3)


@pytest.mark.db
def test_embed_variant_times_only_the_model_calls(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_corpus(conn)
    monkeypatch.setattr(exp, "MAX_BATCH_SIZE", 2)
    _, alternate, _ = small_variants()
    ticks = iter([0.0, 2.0, 10.0, 13.0])  # two batches: 2 s then 3 s, with other work between

    stats = exp.embed_variant(
        conn,
        alternate,
        TopicEmbedder("alt-model"),
        progress=lambda _: None,
        clock=lambda: next(ticks),
    )

    assert stats.seconds == pytest.approx(5.0)


@pytest.mark.db
def test_drop_command_removes_experiment_rows_and_prints_the_count(
    conn: psycopg.Connection,
    db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seed_corpus(conn)
    _, alternate, _ = small_variants()
    exp.embed_variant(conn, alternate, TopicEmbedder("alt-model"), progress=lambda _: None)
    monkeypatch.setattr(
        exp, "open_connection", lambda settings: psycopg.connect(db_url, autocommit=True)
    )

    assert evaluate_cli.main(["embeddings", "--drop"]) == 0

    assert "Dropped 3 experiment embedding rows." in capsys.readouterr().out
    assert experiment_rows(conn) == 0
    assert count_embeddings(conn, "prod-model") == 3
