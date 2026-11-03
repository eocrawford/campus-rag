"""Fake `Services` for the API and MCP tests: fixed search results, scripted chat, real or no DB."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from fakes import FakeChat, FakeSearcher

from campus_rag.config import Settings
from campus_rag.retrieve.models import RetrievedChunk, SearchFilters, SearchMode
from campus_rag.services import ComponentStatus, Health, Scope
from campus_rag.ticketing.client import FiledTicket

HEALTHY = Health(
    database=ComponentStatus(True, "schema version 4"),
    embedding=ComponentStatus(True, "model nomic-embed-text answers"),
    chat=ComponentStatus(True, "model qwen2.5:7b-instruct is available"),
    schema_version=4,
)


class BrokenConnection:
    """A connection whose every operation fails, like a database that went away."""

    def transaction(self) -> Any:
        raise psycopg.OperationalError("connection lost\nDETAIL: fake")

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        raise psycopg.OperationalError("connection lost\nDETAIL: fake")

    def close(self) -> None:
        pass


class RaisingSearcher:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def search(
        self,
        query: str,
        *,
        mode: SearchMode = "hybrid",
        k: int = 8,
        filters: SearchFilters | None = None,
    ) -> list[RetrievedChunk]:
        raise self.error


class FakeTicketing:
    """`Ticketing` that records filed tickets, or raises `error`."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.filed: list[dict[str, Any]] = []

    def create_ticket(
        self,
        *,
        category: str,
        summary: str,
        details: str,
        source_question: str | None = None,
        request_id: int | None = None,
    ) -> FiledTicket:
        if self.error is not None:
            raise self.error
        self.filed.append(
            {
                "category": category,
                "summary": summary,
                "details": details,
                "source_question": source_question,
                "request_id": request_id,
            }
        )
        number = len(self.filed)
        return FiledTicket(
            id=number,
            url=f"http://127.0.0.1:8001/tickets/{number}",
            status="open",
            category=category,
            summary=summary,
        )


class FakeServices:
    """Opens a connection to `db_url` (a migrated throwaway database), or a broken one."""

    def __init__(
        self,
        *,
        searcher: Any = None,
        chat: FakeChat | None = None,
        db_url: str | None = None,
        open_error: Exception | None = None,
        health: Health = HEALTHY,
    ) -> None:
        self.settings = Settings(database_url=db_url or "postgresql://unused.invalid/none")
        self.searcher = searcher if searcher is not None else FakeSearcher()
        self.chat = chat if chat is not None else FakeChat()
        self.db_url = db_url
        self.open_error = open_error
        self._health = health
        self.closed = False

    @contextmanager
    def open_scope(self) -> Iterator[Scope]:
        if self.open_error is not None:
            raise self.open_error
        conn: Any = (
            psycopg.connect(self.db_url, autocommit=True) if self.db_url else BrokenConnection()
        )
        try:
            yield Scope(conn, self.searcher, self.chat)
        finally:
            conn.close()

    def health(self) -> Health:
        return self._health

    def close(self) -> None:
        self.closed = True
