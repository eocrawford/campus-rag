"""Configuration loaded from environment variables and an optional .env file."""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from dotenv import dotenv_values


@dataclass(frozen=True)
class EmbeddingSettings:
    base_url: str = "http://127.0.0.1:11434/v1"
    model: str = "nomic-embed-text"
    dimensions: int = 768
    document_prefix: str = "search_document: "
    query_prefix: str = "search_query: "
    api_key: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.dimensions <= 0:
            raise ValueError(f"embedding dimensions must be > 0, got {self.dimensions}")


@dataclass(frozen=True)
class Settings:
    database_url: str
    embedding: EmbeddingSettings = field(default_factory=EmbeddingSettings)


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
    return Settings(database_url=url, embedding=_load_embedding_settings(values))


def _load_embedding_settings(values: Mapping[str, str]) -> EmbeddingSettings:
    defaults = EmbeddingSettings()

    def text(name: str, default: str) -> str:
        return values.get(f"CAMPUS_RAG_EMBEDDING_{name}") or default

    def prefix(name: str, default: str) -> str:
        # An explicitly empty prefix is valid for models that need none.
        return values.get(f"CAMPUS_RAG_EMBEDDING_{name}", default)

    raw_dimensions = text("DIMENSIONS", str(defaults.dimensions)).strip()
    try:
        dimensions = int(raw_dimensions)
    except ValueError:
        dimensions = 0
    if dimensions <= 0:
        raise ValueError(
            f"CAMPUS_RAG_EMBEDDING_DIMENSIONS must be a positive integer, got {raw_dimensions!r}"
        )
    return EmbeddingSettings(
        base_url=text("BASE_URL", defaults.base_url).rstrip("/"),
        model=text("MODEL", defaults.model),
        dimensions=dimensions,
        document_prefix=prefix("DOCUMENT_PREFIX", defaults.document_prefix),
        query_prefix=prefix("QUERY_PREFIX", defaults.query_prefix),
        api_key=values.get("CAMPUS_RAG_EMBEDDING_API_KEY") or None,
    )
