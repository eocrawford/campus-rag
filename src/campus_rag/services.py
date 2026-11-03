"""Services the API and the MCP server share: a database scope per request, and health checks.

Each request opens its own database connection. psycopg has no connection pool installed here, a
local connect takes a few milliseconds, and a connection per request is correct under the
threadpool that runs synchronous handlers. Model clients are thread-safe and live as long as the
`Services` object.
"""

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import Protocol

import psycopg

from campus_rag.analytics.log import SCHEMA_VERSION as LOGGING_SCHEMA_VERSION
from campus_rag.config import Settings
from campus_rag.generate.chat import ChatClient, ChatError, OpenAICompatibleChat
from campus_rag.index.embed import EmbeddingError, OpenAICompatibleEmbedder
from campus_rag.index.migrate import _first_line
from campus_rag.retrieve.connect import REQUIRED_SCHEMA_VERSION, open_connection
from campus_rag.retrieve.models import RetrievedChunk, Searcher
from campus_rag.retrieve.search import PolicySearcher

HEALTH_OK_SECONDS = 30.0  # how long a successful model check is reused
HEALTH_FAILURE_SECONDS = 5.0  # a failed check is retried sooner so a fixed server shows up


@dataclass(frozen=True)
class Scope:
    """What one request uses: its connection, a searcher on it, and the chat client."""

    conn: psycopg.Connection
    searcher: Searcher
    chat: ChatClient


@dataclass(frozen=True)
class ComponentStatus:
    ok: bool
    detail: str


@dataclass(frozen=True)
class Health:
    database: ComponentStatus
    embedding: ComponentStatus
    chat: ComponentStatus
    schema_version: int | None = None

    @property
    def ok(self) -> bool:
        return self.database.ok and self.embedding.ok and self.chat.ok


class Services(Protocol):
    """The seam tests fake: model names for the log, a scope per request, and health."""

    settings: Settings

    def open_scope(self) -> AbstractContextManager[Scope]:
        """Open a connection scope. Raises `SetupError` when the database is not usable."""
        ...

    def health(self) -> Health: ...

    def close(self) -> None: ...


class LiveServices:
    """`Services` on the configured PostgreSQL database and model server."""

    def __init__(self, settings: Settings, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.settings = settings
        self._embedder = OpenAICompatibleEmbedder(settings.embedding)
        self._chat = OpenAICompatibleChat(settings.chat)
        self._clock = clock
        self._lock = threading.Lock()
        self._model_checks: dict[str, tuple[float, ComponentStatus]] = {}

    @contextmanager
    def open_scope(self) -> Iterator[Scope]:
        conn = open_connection(self.settings)
        try:
            yield Scope(conn, PolicySearcher(conn, self._embedder), self._chat)
        finally:
            conn.close()

    def health(self) -> Health:
        database, version = self._check_database()
        return Health(
            database=database,
            embedding=self._cached_check("embedding", self._check_embedding),
            chat=self._cached_check("chat", self._check_chat),
            schema_version=version,
        )

    def close(self) -> None:
        self._embedder.close()
        self._chat.close()

    def _check_database(self) -> tuple[ComponentStatus, int | None]:
        try:
            with psycopg.connect(
                self.settings.database_url, autocommit=True, connect_timeout=3
            ) as conn:
                has_table = conn.execute("SELECT to_regclass('schema_migrations')").fetchone()
                row = (
                    conn.execute("SELECT max(version) FROM schema_migrations").fetchone()
                    if has_table and has_table[0]
                    else None
                )
        except psycopg.Error as exc:
            return ComponentStatus(False, f"cannot connect: {_first_line(exc)}"), None
        version = row[0] if row else None
        if version is None or version < REQUIRED_SCHEMA_VERSION:
            return (
                ComponentStatus(
                    False,
                    f"schema version {version}; run: uv run python -m campus_rag.index.migrate",
                ),
                version,
            )
        if version < LOGGING_SCHEMA_VERSION:
            note = f"schema version {version}; request logging is off until you run migrate"
            return ComponentStatus(True, note), version
        return ComponentStatus(True, f"schema version {version}"), version

    def _check_embedding(self) -> ComponentStatus:
        try:
            self._embedder.check()
        except EmbeddingError as exc:
            return ComponentStatus(False, str(exc))
        return ComponentStatus(True, f"model {self.settings.embedding.model} answers")

    def _check_chat(self) -> ComponentStatus:
        model = self.settings.chat.answer_model
        try:
            self._chat.check(model)
        except ChatError as exc:
            return ComponentStatus(False, str(exc))
        return ComponentStatus(True, f"model {model} is available")

    def _cached_check(self, name: str, check: Callable[[], ComponentStatus]) -> ComponentStatus:
        now = self._clock()
        with self._lock:
            cached = self._model_checks.get(name)
            if cached and cached[0] > now:
                return cached[1]
        status = check()
        ttl = HEALTH_OK_SECONDS if status.ok else HEALTH_FAILURE_SECONDS
        with self._lock:
            self._model_checks[name] = (now + ttl, status)
        return status


def elapsed_ms(started: float) -> float:
    """Milliseconds since `started`, a `time.perf_counter()` reading."""
    return round((time.perf_counter() - started) * 1000, 1)


def passage_body(chunk: RetrievedChunk) -> str:
    """The chunk text without the heading-path line it starts with."""
    prefix = f"{chunk.heading_path}\n\n"
    return chunk.content[len(prefix) :] if chunk.content.startswith(prefix) else chunk.content
