"""Configuration loaded from environment variables and an optional .env file."""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from dotenv import dotenv_values


@dataclass(frozen=True)
class Settings:
    database_url: str


def find_repo_root(start: Path | None = None) -> Path | None:
    """Return the nearest directory at or above `start` that contains pyproject.toml."""
    current = (start or Path.cwd()).resolve()
    for directory in (current, *current.parents):
        if (directory / "pyproject.toml").is_file():
            return directory
    return None


def _default_env_file() -> Path | None:
    root = find_repo_root()
    return root / ".env" if root else None


_UNSET = object()


def load_settings(
    environ: Mapping[str, str] | None = None,
    env_file: Path | None | object = _UNSET,
) -> Settings:
    """Build settings. Real environment variables override values from the .env file.

    `env_file` defaults to the repo-root .env; pass None to skip the file.
    """
    if env_file is _UNSET:
        env_file = _default_env_file()
    values: dict[str, str] = {}
    if isinstance(env_file, Path) and env_file.is_file():
        values.update({k: v for k, v in dotenv_values(env_file).items() if v is not None})
    values.update(os.environ if environ is None else environ)

    url = values.get("CAMPUS_RAG_DATABASE_URL")
    if not url:
        user = quote(values.get("CAMPUS_RAG_DB_USER", "campus_rag"), safe="")
        password = quote(values.get("CAMPUS_RAG_DB_PASSWORD", "campus_rag_local"), safe="")
        host = values.get("CAMPUS_RAG_DB_HOST", "127.0.0.1")
        port = values.get("CAMPUS_RAG_DB_PORT", "5433")
        name = values.get("CAMPUS_RAG_DB_NAME", "campus_rag")
        url = f"postgresql://{user}:{password}@{host}:{port}/{name}"
    return Settings(database_url=url)
