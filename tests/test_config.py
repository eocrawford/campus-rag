import os
from pathlib import Path

import pytest

from campus_rag.config import EmbeddingSettings, load_settings


def test_defaults(tmp_path: Path) -> None:
    settings = load_settings(environ={}, env_file=tmp_path / ".env")
    assert (
        settings.database_url
        == "postgresql://campus_rag:campus_rag_local@127.0.0.1:5433/campus_rag"
    )


def test_missing_env_file_is_fine(tmp_path: Path) -> None:
    assert load_settings(environ={}, env_file=tmp_path / "missing.env").database_url
    assert load_settings(environ={}, env_file=None).database_url


def test_env_file_values_are_used(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("CAMPUS_RAG_DB_PORT=6000\nCAMPUS_RAG_DB_NAME=from_file\n")
    settings = load_settings(environ={}, env_file=env_file)
    assert settings.database_url.endswith("@127.0.0.1:6000/from_file")


def test_environment_overrides_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("CAMPUS_RAG_DB_PORT=6000\nCAMPUS_RAG_DB_HOST=filehost\n")
    settings = load_settings(environ={"CAMPUS_RAG_DB_PORT": "7000"}, env_file=env_file)
    assert settings.database_url.endswith("@filehost:7000/campus_rag")


def test_database_url_overrides_parts(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("CAMPUS_RAG_DB_PORT=6000\n")
    url = "postgresql://u:p@example:1234/other"
    settings = load_settings(environ={"CAMPUS_RAG_DATABASE_URL": url}, env_file=env_file)
    assert settings.database_url == url


def test_password_and_user_are_url_encoded(tmp_path: Path) -> None:
    settings = load_settings(
        environ={"CAMPUS_RAG_DB_USER": "a b", "CAMPUS_RAG_DB_PASSWORD": "p@ss/w:rd#?"},
        env_file=tmp_path / ".env",
    )
    assert settings.database_url == (
        "postgresql://a%20b:p%40ss%2Fw%3Ard%23%3F@127.0.0.1:5433/campus_rag"
    )


def test_does_not_mutate_os_environ(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("CAMPUS_RAG_DB_PORT", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("CAMPUS_RAG_DB_PORT=6000\n")
    load_settings(environ={}, env_file=env_file)
    assert "CAMPUS_RAG_DB_PORT" not in os.environ


def test_embedding_defaults(tmp_path: Path) -> None:
    embedding = load_settings(environ={}, env_file=tmp_path / ".env").embedding
    assert embedding == EmbeddingSettings(
        base_url="http://127.0.0.1:11434/v1",
        model="nomic-embed-text",
        dimensions=768,
        document_prefix="search_document: ",
        query_prefix="search_query: ",
        api_key=None,
    )


def test_embedding_values_come_from_env_file_and_environment_wins(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "CAMPUS_RAG_EMBEDDING_BASE_URL=http://file.test/v1/\n"
        "CAMPUS_RAG_EMBEDDING_MODEL=from-file\n"
        "CAMPUS_RAG_EMBEDDING_DIMENSIONS=1024\n"
        'CAMPUS_RAG_EMBEDDING_DOCUMENT_PREFIX="doc: "\n'
        'CAMPUS_RAG_EMBEDDING_QUERY_PREFIX="query: "\n'
        "CAMPUS_RAG_EMBEDDING_API_KEY=file-key\n"
    )
    environ = {"CAMPUS_RAG_EMBEDDING_MODEL": "from-env", "CAMPUS_RAG_EMBEDDING_API_KEY": "env-key"}
    embedding = load_settings(environ=environ, env_file=env_file).embedding
    assert embedding.base_url == "http://file.test/v1"
    assert embedding.model == "from-env"
    assert embedding.dimensions == 1024
    assert embedding.document_prefix == "doc: "  # the trailing space survives quoting
    assert embedding.query_prefix == "query: "
    assert embedding.api_key == "env-key"


def test_embedding_prefixes_can_be_empty_and_blank_key_is_none(tmp_path: Path) -> None:
    environ = {
        "CAMPUS_RAG_EMBEDDING_DOCUMENT_PREFIX": "",
        "CAMPUS_RAG_EMBEDDING_QUERY_PREFIX": "",
        "CAMPUS_RAG_EMBEDDING_API_KEY": "",
        "CAMPUS_RAG_EMBEDDING_MODEL": "",
    }
    embedding = load_settings(environ=environ, env_file=tmp_path / ".env").embedding
    assert (embedding.document_prefix, embedding.query_prefix) == ("", "")
    assert embedding.api_key is None
    assert embedding.model == "nomic-embed-text"


@pytest.mark.parametrize("value", ["0", "-5", "abc", " "])
def test_embedding_dimensions_must_be_positive_integer(tmp_path: Path, value: str) -> None:
    with pytest.raises(ValueError, match="CAMPUS_RAG_EMBEDDING_DIMENSIONS"):
        load_settings(
            environ={"CAMPUS_RAG_EMBEDDING_DIMENSIONS": value}, env_file=tmp_path / ".env"
        )


def test_embedding_settings_validate_dimensions_and_hide_key() -> None:
    with pytest.raises(ValueError, match="dimensions"):
        EmbeddingSettings(dimensions=0)
    assert "secret" not in repr(EmbeddingSettings(api_key="secret"))
