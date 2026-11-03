"""Tests for the ingest command's embedding flags and exit codes, with every stage faked."""

import pytest

from campus_rag.config import Settings
from campus_rag.index.embed import EmbeddingError
from campus_rag.ingest import __main__ as cli
from campus_rag.ingest.models import SourceConfig
from campus_rag.ingest.pipeline import EmbedReport, IngestReport


class FakeConnection:
    closed = False

    def close(self) -> None:
        self.closed = True


class Harness:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.events: list[str] = []
        self.connection = FakeConnection()
        self.check_error: EmbeddingError | None = None
        self.embed_error: EmbeddingError | None = None
        self.embed_report = EmbedReport(embedded=5, already_current=2, elapsed_seconds=1.0)
        self.schema_version = 3
        self.settings = Settings(database_url="postgresql://db.test/campus")
        harness = self

        class FakeEmbedder:
            def __init__(self, settings) -> None:
                harness.events.append("construct")
                self.model = settings.model
                self.dimensions = settings.dimensions

            def __enter__(self):
                return self

            def __exit__(self, *exc) -> None:
                harness.events.append("close")

            def check(self) -> None:
                harness.events.append("check")
                if harness.check_error:
                    raise harness.check_error

        def fake_run_embedding(conn, embedder, *, log):
            harness.events.append("embed")
            log("embedded 5/5 chunks")
            if harness.embed_error:
                raise harness.embed_error
            return harness.embed_report

        def fake_ingest(conn, configs, args):
            harness.events.append("ingest")
            return IngestReport(selected=1, fetched_ok=1, chunks=5), 1, {}

        config = SourceConfig("src", "https://x.test/s.xml", ("https://x.test/",), (), 20, None)
        monkeypatch.setattr(cli, "load_source_configs", lambda: [config])
        monkeypatch.setattr(cli, "load_settings", lambda: harness.settings)
        monkeypatch.setattr(cli.psycopg, "connect", self._connect)
        monkeypatch.setattr(cli, "schema_version_applied", lambda c, v: v <= harness.schema_version)
        monkeypatch.setattr(cli, "OpenAICompatibleEmbedder", FakeEmbedder)
        monkeypatch.setattr(cli, "run_embedding", fake_run_embedding)
        monkeypatch.setattr(cli, "_ingest", fake_ingest)

    def _connect(self, *args, **kwargs) -> FakeConnection:
        self.events.append("connect")
        return self.connection


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Harness:
    return Harness(monkeypatch)


def test_embedding_is_checked_before_ingest_then_run_after_it(harness, capsys) -> None:
    assert cli.main([]) == 0
    assert harness.events == ["connect", "construct", "check", "ingest", "embed", "close"]
    out = capsys.readouterr().out
    assert "embedded 5/5 chunks" in out
    assert "Embeddings: 5 embedded, 2 already current (1.0s, 5.0 chunks/s)" in out
    assert harness.connection.closed


def test_unreachable_model_server_exits_7_before_anything_is_fetched(harness, capsys) -> None:
    harness.check_error = EmbeddingError(
        "cannot reach the embedding server. Start it with: docker compose up -d --wait ollama"
    )
    assert cli.main([]) == cli.EXIT_EMBEDDING_UNAVAILABLE == 7
    assert "ingest" not in harness.events and "embed" not in harness.events
    err = capsys.readouterr().err
    assert "docker compose up -d --wait ollama" in err
    assert "--no-embed" in err
    assert harness.connection.closed


def test_no_embed_skips_the_embedder_entirely(harness, capsys) -> None:
    assert cli.main(["--no-embed"]) == 0
    assert harness.events == ["connect", "ingest"]
    assert "Embeddings: skipped (--no-embed)" in capsys.readouterr().out


def test_no_embed_works_when_the_model_server_is_down(harness) -> None:
    harness.check_error = EmbeddingError("down")
    assert cli.main(["--no-embed"]) == 0


def test_dry_run_neither_connects_nor_embeds(harness, capsys) -> None:
    assert cli.main(["--dry-run"]) == 0
    assert harness.events == ["ingest"]
    out = capsys.readouterr().out
    assert "Dry run" in out and "Embeddings" not in out


def test_nothing_to_embed_still_checks_once(harness, capsys) -> None:
    harness.embed_report = EmbedReport(embedded=0, already_current=1978, elapsed_seconds=0.0)
    assert cli.main([]) == 0
    assert harness.events.count("check") == 1
    assert "Embeddings: 0 embedded, 1978 already current" in capsys.readouterr().out


def test_embedding_failure_midway_exits_6_and_says_to_rerun(harness, capsys) -> None:
    harness.embed_error = EmbeddingError("HTTP 503 after 4 attempt(s)")
    assert cli.main([]) == cli.EXIT_DATABASE_ERROR == 6
    captured = capsys.readouterr()
    assert "Embeddings: failed" in captured.out
    assert "embedding failed: HTTP 503" in captured.err
    assert "rerun" in captured.err
    assert harness.connection.closed


def test_schema_version_3_is_required(harness, capsys) -> None:
    harness.schema_version = 2
    assert cli.main([]) == cli.EXIT_SCHEMA_OUT_OF_DATE
    assert "schema is out of date" in capsys.readouterr().err
    assert "check" not in harness.events


def test_invalid_embedding_dimensions_is_a_config_error(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "load_source_configs", lambda: [])
    monkeypatch.setenv("CAMPUS_RAG_EMBEDDING_DIMENSIONS", "zero")
    assert cli.main(["--dry-run"]) == cli.EXIT_CONFIG_ERROR
    assert "CAMPUS_RAG_EMBEDDING_DIMENSIONS" in capsys.readouterr().err


def test_page_failures_still_exit_4_after_embedding(harness, monkeypatch, capsys) -> None:
    def failing_ingest(conn, configs, args):
        return (
            IngestReport(selected=1, failed=1, failures=[("https://x.test/a", "HTTP 503")]),
            1,
            {},
        )

    monkeypatch.setattr(cli, "_ingest", failing_ingest)
    assert cli.main([]) == cli.EXIT_PAGES_FAILED
    assert "Embeddings: 5 embedded" in capsys.readouterr().out
