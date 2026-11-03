"""The retrieve command: output format, filters, and exit codes, with every dependency faked."""

from types import SimpleNamespace

import psycopg
import pytest

from campus_rag.index.embed import EmbeddingError
from campus_rag.retrieve import __main__ as cli
from campus_rag.retrieve.connect import SetupError
from campus_rag.retrieve.models import RetrievedChunk, SearchFilters


def chunk(rank: int = 1, **overrides) -> RetrievedChunk:
    fields = {
        "chunk_id": 10 + rank,
        "document_id": 1,
        "url": "https://example.test/policy/",
        "title": "Parking Policy",
        "heading_path": "Parking Policy > 1. Permits",
        "content": "Parking Policy > 1. Permits\n\nPermits expire each year.",
        "token_count": 12,
        "score": 0.0325,
        "rank": rank,
        "keyword_rank": 1,
        "vector_rank": None,
    }
    fields.update(overrides)
    return RetrievedChunk(**fields)


class FakeConnection:
    closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.closed = True


class Harness:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.setup_error: SetupError | None = None
        self.search_error: Exception | None = None
        self.results: list[RetrievedChunk] = [chunk()]
        self.search_calls: list[dict] = []
        self.connection = FakeConnection()
        harness = self

        class FakeEmbedder:
            def __init__(self, settings) -> None:
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc) -> None:
                pass

        class FakeSearcher:
            def __init__(self, conn, embedder) -> None:
                pass

            def search(self, query, *, mode, k, filters):
                harness.search_calls.append(
                    {"query": query, "mode": mode, "k": k, "filters": filters}
                )
                if harness.search_error:
                    raise harness.search_error
                return harness.results

        def open_connection(settings):
            if harness.setup_error:
                raise harness.setup_error
            return harness.connection

        monkeypatch.setattr(cli, "load_settings", lambda: SimpleNamespace(embedding=object()))
        monkeypatch.setattr(cli, "open_connection", open_connection)
        monkeypatch.setattr(cli, "OpenAICompatibleEmbedder", FakeEmbedder)
        monkeypatch.setattr(cli, "PolicySearcher", FakeSearcher)


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Harness:
    return Harness(monkeypatch)


def test_prints_each_result_with_ranks_title_heading_url_and_preview(
    harness: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    harness.results = [chunk(1), chunk(2, keyword_rank=3, vector_rank=2, score=0.01)]
    assert cli.main(["parking permits"]) == 0
    output = capsys.readouterr().out
    assert "2 result(s) for 'parking permits' (hybrid search)" in output
    assert " 1. score=0.0325  keyword rank=1  vector rank=-" in output
    assert " 2. score=0.0100  keyword rank=3  vector rank=2" in output
    assert "Parking Policy\n" in output
    assert "    Parking Policy > 1. Permits\n" in output
    assert "    https://example.test/policy/\n" in output
    assert "    Permits expire each year." in output


def test_passes_mode_k_and_filters_to_the_searcher(harness: Harness) -> None:
    code = cli.main(
        [
            "parking",
            "--mode",
            "keyword",
            "-k",
            "3",
            "--source",
            "uw-policy",
            "--url-prefix",
            "https://a.test/",
            "--url-prefix",
            "https://b.test/",
        ]
    )
    assert code == 0
    assert harness.search_calls == [
        {
            "query": "parking",
            "mode": "keyword",
            "k": 3,
            "filters": SearchFilters(
                sources=("uw-policy",), url_prefixes=("https://a.test/", "https://b.test/")
            ),
        }
    ]
    assert harness.connection.closed


def test_defaults_to_hybrid_search_with_eight_results(harness: Harness) -> None:
    cli.main(["parking"])
    assert harness.search_calls[0]["mode"] == "hybrid" and harness.search_calls[0]["k"] == 8
    assert harness.search_calls[0]["filters"] == SearchFilters()


def test_reports_no_results(harness: Harness, capsys: pytest.CaptureFixture[str]) -> None:
    harness.results = []
    assert cli.main(["parking"]) == 0
    assert capsys.readouterr().out.strip() == "No results."


def test_preview_drops_the_heading_collapses_whitespace_and_truncates() -> None:
    long_body = "word  " * 100
    item = chunk(content=f"Parking Policy > 1. Permits\n\n{long_body}")
    text = cli.preview(item)
    assert len(text) == cli.PREVIEW_CHARS
    assert text.startswith("word word") and text.endswith("...")
    assert "  " not in text
    short = chunk(content="No heading prefix here.")
    assert cli.preview(short) == "No heading prefix here."


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (SetupError("cannot connect to PostgreSQL at 127.0.0.1:5433/campus_rag", 2), 2),
        (SetupError("database schema is out of date. Run: uv run python -m ...", 3), 3),
    ],
)
def test_setup_errors_use_their_exit_codes(
    harness: Harness, capsys: pytest.CaptureFixture[str], error: SetupError, code: int
) -> None:
    harness.setup_error = error
    assert cli.main(["parking"]) == code
    assert str(error) in capsys.readouterr().err
    assert harness.search_calls == []


def test_embedding_server_failure_exits_7_and_suggests_keyword_mode(
    harness: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    harness.search_error = EmbeddingError("cannot reach the embedding server")
    assert cli.main(["parking", "--mode", "vector"]) == 7
    err = capsys.readouterr().err
    assert "cannot reach the embedding server" in err and "--mode keyword" in err


def test_database_errors_exit_6(harness: Harness, capsys: pytest.CaptureFixture[str]) -> None:
    harness.search_error = psycopg.OperationalError("server closed the connection")
    assert cli.main(["parking"]) == 6
    assert "database error: server closed the connection" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["   "], ["parking", "-k", "0"], ["parking", "-k", "51"]])
def test_invalid_arguments_exit_1_without_searching(
    harness: Harness, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    assert cli.main(argv) == 1
    assert capsys.readouterr().err.startswith("ERROR:")
    assert harness.search_calls == []


def test_unknown_mode_is_rejected_by_the_parser() -> None:
    with pytest.raises(SystemExit):
        cli.main(["parking", "--mode", "semantic"])
