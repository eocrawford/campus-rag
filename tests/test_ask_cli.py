"""Tests for the ask command's output and exit codes, with every service faked."""

import json

import psycopg
import pytest
from fakes import FakeChat, FakeSearcher, make_chunk

from campus_rag import ask as cli
from campus_rag.config import Settings
from campus_rag.generate.chat import ChatError
from campus_rag.generate.models import REFUSAL_TEXT
from campus_rag.index.embed import EmbeddingError
from campus_rag.retrieve import connect as connect_module


class FakeConnection:
    closed = False

    def close(self) -> None:
        self.closed = True


class Harness:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.events: list[str] = []
        self.connection = FakeConnection()
        self.connect_error: Exception | None = None
        self.schema_version = 3
        self.embed_check_error: EmbeddingError | None = None
        self.chat_check_errors: dict[str, ChatError] = {}
        self.chat = FakeChat(["Rule one applies [1]."])
        self.searcher = FakeSearcher([make_chunk(1), make_chunk(2)])
        self.settings = Settings(database_url="postgresql://db.test/campus")
        harness = self

        class Embedder:
            def __init__(self, settings) -> None:
                self.model = settings.model

            def __enter__(self):
                return self

            def __exit__(self, *exc) -> None:
                harness.events.append("embedder closed")

            def check(self) -> None:
                harness.events.append("embedder check")
                if harness.embed_check_error:
                    raise harness.embed_check_error

        class Chat:
            def __init__(self, settings) -> None:
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc) -> None:
                harness.events.append("chat closed")

            def check(self, model: str) -> None:
                harness.events.append(f"chat check {model}")
                if model in harness.chat_check_errors:
                    raise harness.chat_check_errors[model]

            def complete(self, messages, **kwargs):
                return harness.chat.complete(messages, **kwargs)

        def connect(*args, **kwargs):
            harness.events.append("connect")
            if harness.connect_error:
                raise harness.connect_error
            return harness.connection

        monkeypatch.setattr(cli, "load_settings", lambda: harness.settings)
        monkeypatch.setattr(connect_module.psycopg, "connect", connect)
        monkeypatch.setattr(
            connect_module, "schema_version_applied", lambda c, v: v <= harness.schema_version
        )
        monkeypatch.setattr(cli, "OpenAICompatibleEmbedder", Embedder)
        monkeypatch.setattr(cli, "OpenAICompatibleChat", Chat)
        monkeypatch.setattr(cli, "_make_searcher", lambda conn, embedder: harness.searcher)


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Harness:
    return Harness(monkeypatch)


def test_answer_prints_text_sources_and_timings(harness, capsys) -> None:
    assert cli.main(["Which rule applies?"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Rule one applies [1].\n\nSources\n")
    assert "  [1] Sample Policy 1 > Section 1\n      https://policy.example.edu/sample-1/" in out
    assert "[2] Sample Policy 2" not in out  # only cited passages are listed
    assert "Timings: retrieve " in out and "generate " in out
    assert harness.events == [
        "connect",
        "embedder check",
        "chat check qwen2.5:7b-instruct",
        "chat closed",
        "embedder closed",
    ]
    assert harness.connection.closed
    assert harness.searcher.calls[0] == {
        "query": "Which rule applies?",
        "mode": "hybrid",
        "k": 8,
        "filters": None,
    }


def test_mode_and_k_reach_the_searcher(harness) -> None:
    assert cli.main(["q", "--mode", "vector", "-k", "3"]) == 0
    assert harness.searcher.calls[0]["mode"] == "vector"
    assert harness.searcher.calls[0]["k"] == 3


def test_show_passages_prints_every_passage(harness, capsys) -> None:
    assert cli.main(["q", "--show-passages"]) == 0
    out = capsys.readouterr().out
    assert "Passages given to the model (2)" in out
    assert "[2] Sample Policy 2 > Section 2\nURL: https://policy.example.edu/sample-2/" in out


def test_json_output_is_the_answer_record(harness, capsys) -> None:
    assert cli.main(["q", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["text"] == "Rule one applies [1]."
    assert data["refused"] is False
    assert data["citations"][0]["chunk_id"] == 101
    assert len(data["passages"]) == 2


def test_refusal_prints_no_sources(harness, capsys) -> None:
    harness.chat.replies = ["Nothing in the passages covers that."]
    assert cli.main(["q"]) == 0
    out = capsys.readouterr().out
    assert out.startswith(REFUSAL_TEXT)
    assert "Sources" not in out


def test_removed_citations_are_reported(harness, capsys) -> None:
    harness.chat.replies = ["Rule one applies [1][5]."]
    assert cli.main(["q"]) == 0
    assert "Removed citations to passages that were not supplied: 5" in capsys.readouterr().out


def test_unreachable_database_exits_2(harness, capsys) -> None:
    harness.connect_error = psycopg.OperationalError("connection refused")
    assert cli.main(["q"]) == cli.EXIT_CONNECTION_FAILED == 2
    assert "docker compose up -d --wait db" in capsys.readouterr().err


def test_old_schema_exits_3(harness, capsys) -> None:
    harness.schema_version = 2
    assert cli.main(["q"]) == cli.EXIT_SCHEMA_OUT_OF_DATE == 3
    assert "campus_rag.index.migrate" in capsys.readouterr().err
    assert harness.connection.closed


def test_embedding_server_unavailable_exits_7_before_answering(harness, capsys) -> None:
    harness.embed_check_error = EmbeddingError("cannot reach the embedding server")
    assert cli.main(["q"]) == cli.EXIT_MODEL_UNAVAILABLE == 7
    assert "cannot reach the embedding server" in capsys.readouterr().err
    assert harness.searcher.calls == [] and harness.chat.calls == []


def test_missing_answer_model_exits_7_with_the_pull_command(harness, capsys) -> None:
    harness.chat_check_errors["qwen2.5:7b-instruct"] = ChatError(
        "Pull it with: docker compose exec ollama ollama pull qwen2.5:7b-instruct"
    )
    assert cli.main(["q"]) == 7
    assert "ollama pull qwen2.5:7b-instruct" in capsys.readouterr().err
    assert harness.searcher.calls == []


def test_chat_failure_while_answering_exits_7(harness, capsys) -> None:
    harness.chat.replies = [ChatError("chat request failed: HTTP 500")]
    assert cli.main(["q"]) == 7
    assert "HTTP 500" in capsys.readouterr().err


def test_database_error_while_answering_exits_6(harness, capsys) -> None:
    def broken_search(*args, **kwargs):
        raise psycopg.errors.UndefinedTable("relation missing")

    harness.searcher.search = broken_search
    assert cli.main(["q"]) == cli.EXIT_DATABASE_ERROR == 6
    assert "database error" in capsys.readouterr().err


def test_invalid_configuration_exits_5(harness, monkeypatch, capsys) -> None:
    def bad_settings():
        raise ValueError("CAMPUS_RAG_CHAT_TIMEOUT_SECONDS must be a positive number")

    monkeypatch.setattr(cli, "load_settings", bad_settings)
    assert cli.main(["q"]) == cli.EXIT_CONFIG_ERROR == 5


def test_question_is_required_without_chat(harness) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main([])
    assert excinfo.value.code == 2


def test_chat_mode_checks_the_rewrite_model_and_rewrites_follow_ups(harness, capsys) -> None:
    harness.chat.replies = [
        "Rule one applies [1].",
        "Does rule one apply to staff?",
        "Yes, staff are covered [2].",
    ]
    lines = iter(["And for staff?", ""])
    assert cli.main(["--chat", "Which rule applies?"], read_line=lambda prompt: next(lines)) == 0

    assert "chat check qwen2.5:3b-instruct" in harness.events
    assert [call["query"] for call in harness.searcher.calls] == [
        "Which rule applies?",
        "Does rule one apply to staff?",
    ]
    rewrite_request = harness.chat.calls[1]["messages"][-1]["content"]
    assert "Earlier question: Which rule applies?" in rewrite_request
    assert "Follow-up question: And for staff?" in rewrite_request
    out = capsys.readouterr().out
    assert "Search query: Does rule one apply to staff?" in out
    assert "Yes, staff are covered [2]." in out


def test_chat_mode_stops_at_end_of_input(harness) -> None:
    def eof(prompt: str) -> str:
        raise EOFError

    assert cli.main(["--chat"], read_line=eof) == 0
    assert harness.searcher.calls == []


def test_sources_are_listed_by_passage_number(harness, capsys) -> None:
    harness.chat.replies = ["Rule two applies [2]. Rule one applies [1]."]
    assert cli.main(["q"]) == 0
    out = capsys.readouterr().out
    assert out.index("  [1] Sample Policy 1") < out.index("  [2] Sample Policy 2")
