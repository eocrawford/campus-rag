import os
from pathlib import Path

from campus_rag.config import load_settings


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
