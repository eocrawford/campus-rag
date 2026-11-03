"""Read and write `mock_tickets`."""

from dataclasses import dataclass
from datetime import datetime

import psycopg

_COLUMNS = "id, created_at, status, category, summary, details, source_question, request_id"


class UnknownRequestError(Exception):
    """`request_id` does not name a row in `request_log`."""


@dataclass(frozen=True)
class StoredTicket:
    id: int
    created_at: datetime
    status: str
    category: str
    summary: str
    details: str
    source_question: str | None
    request_id: int | None


def create_ticket(
    conn: psycopg.Connection,
    *,
    category: str,
    summary: str,
    details: str,
    source_question: str | None = None,
    request_id: int | None = None,
) -> StoredTicket:
    """Insert an open ticket. Raises `UnknownRequestError` for a `request_id` not in the log."""
    try:
        with conn.transaction():
            row = conn.execute(
                f"INSERT INTO mock_tickets (category, summary, details, source_question, "
                f"request_id) VALUES (%s, %s, %s, %s, %s) RETURNING {_COLUMNS}",
                (category, summary, details, source_question, request_id),
            ).fetchone()
    except psycopg.errors.ForeignKeyViolation:
        raise UnknownRequestError(f"unknown request_id {request_id}") from None
    assert row is not None
    return StoredTicket(*row)


def get_ticket(conn: psycopg.Connection, ticket_id: int) -> StoredTicket | None:
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM mock_tickets WHERE id = %s", (ticket_id,)
    ).fetchone()
    return StoredTicket(*row) if row else None


def list_tickets(
    conn: psycopg.Connection, *, status: str | None = None, limit: int = 50
) -> list[StoredTicket]:
    """Tickets, newest first, optionally only those with `status`."""
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM mock_tickets WHERE (%s::text IS NULL OR status = %s) "
        f"ORDER BY created_at DESC, id DESC LIMIT %s",
        (status, status, limit),
    ).fetchall()
    return [StoredTicket(*row) for row in rows]
