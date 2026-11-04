"""A mock IT service management (ITSM) ticketing API: file, list, and read help-desk tickets.

It stands in for a system such as ServiceNow so the MCP `draft_ticket` tool integrates over HTTP,
the way it would with a real one. It has no authentication, so it binds to loopback only.
"""

import logging
from collections.abc import Callable
from contextlib import contextmanager
from typing import Annotated

import psycopg
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse

from campus_rag.config import load_settings
from campus_rag.index.migrate import _first_line
from campus_rag.ticketing import store
from campus_rag.ticketing.schemas import (
    ErrorResponse,
    HealthResponse,
    Ticket,
    TicketCreate,
    TicketList,
    TicketStatus,
)

logger = logging.getLogger(__name__)

MAX_LIST_LIMIT = 200

_UNAVAILABLE = {"model": ErrorResponse, "description": "The database is unavailable."}


def create_app(connect: Callable[[], psycopg.Connection] | None = None) -> FastAPI:
    """Build the app. `connect` opens an autocommit connection; tests pass a throwaway database."""
    open_connection = connect or _default_connect()

    @contextmanager
    def connection():
        conn = open_connection()
        try:
            yield conn
        finally:
            conn.close()

    app = FastAPI(
        title="campus-rag mock ticketing",
        version="0.1.0",
        description=(
            "A local stand-in for an IT service management (ITSM) system. "
            "Tickets are stored in `mock_tickets`."
        ),
    )

    @app.exception_handler(psycopg.Error)
    def database_error(_: Request, exc: Exception) -> JSONResponse:
        if isinstance(exc, psycopg.errors.UndefinedTable):
            detail = "database tables are missing. Run: uv run python -m campus_rag.index.migrate"
        else:
            detail = f"database error: {_first_line(exc)}"
        return JSONResponse(status_code=503, content={"detail": detail})

    @app.exception_handler(Exception)
    def internal_error(_: Request, exc: Exception) -> JSONResponse:
        logger.error("unhandled error in request", exc_info=exc)
        return JSONResponse(status_code=500, content={"detail": "internal server error"})

    @app.get(
        "/health",
        response_model=HealthResponse,
        summary="Check the ticket store",
        responses={503: {"model": HealthResponse, "description": "The store is unavailable."}},
    )
    def health(response: Response) -> HealthResponse:
        """Report whether the database and the `mock_tickets` table are reachable."""
        try:
            with connection() as conn:
                row = conn.execute("SELECT to_regclass('mock_tickets')").fetchone()
        except psycopg.Error as exc:
            response.status_code = 503
            return HealthResponse(status="degraded", detail=f"cannot connect: {_first_line(exc)}")
        if not row or not row[0]:
            response.status_code = 503
            return HealthResponse(
                status="degraded",
                detail="mock_tickets is missing; run: uv run python -m campus_rag.index.migrate",
            )
        return HealthResponse(status="ok", detail="mock_tickets is available")

    @app.post(
        "/tickets",
        status_code=201,
        response_model=Ticket,
        summary="File a ticket",
        responses={
            422: {"model": ErrorResponse, "description": "Invalid ticket or unknown request_id."},
            503: _UNAVAILABLE,
        },
    )
    def create_ticket(body: TicketCreate, request: Request, response: Response) -> Ticket:
        """Create an open ticket and return it with its id and URL."""
        try:
            with connection() as conn:
                stored = store.create_ticket(
                    conn,
                    category=body.category,
                    summary=body.summary,
                    details=body.details,
                    source_question=body.source_question,
                    request_id=body.request_id,
                )
        except store.UnknownRequestError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        ticket = _ticket(stored, request)
        response.headers["Location"] = ticket.url
        return ticket

    @app.get(
        "/tickets",
        response_model=TicketList,
        summary="List tickets",
        responses={503: _UNAVAILABLE},
    )
    def list_tickets(
        request: Request,
        status: TicketStatus | None = None,
        limit: Annotated[int, Query(ge=1, le=MAX_LIST_LIMIT)] = 50,
    ) -> TicketList:
        """List tickets, newest first, optionally only those that are `open` or `closed`."""
        with connection() as conn:
            stored = store.list_tickets(conn, status=status, limit=limit)
        return TicketList(tickets=[_ticket(item, request) for item in stored])

    @app.get(
        "/tickets/{ticket_id}",
        name="get_ticket",
        response_model=Ticket,
        summary="Read a ticket",
        responses={
            404: {"model": ErrorResponse, "description": "Unknown ticket."},
            503: _UNAVAILABLE,
        },
    )
    def get_ticket(ticket_id: int, request: Request) -> Ticket:
        """Return one ticket."""
        with connection() as conn:
            stored = store.get_ticket(conn, ticket_id)
        if stored is None:
            raise HTTPException(status_code=404, detail=f"unknown ticket {ticket_id}")
        return _ticket(stored, request)

    return app


def _default_connect() -> Callable[[], psycopg.Connection]:
    database_url = load_settings().database_url

    def connect() -> psycopg.Connection:
        return psycopg.connect(database_url, autocommit=True, connect_timeout=5)

    return connect


def _ticket(stored: store.StoredTicket, request: Request) -> Ticket:
    return Ticket(
        id=stored.id,
        url=str(request.url_for("get_ticket", ticket_id=stored.id)),
        created_at=stored.created_at,
        status=stored.status,
        category=stored.category,
        summary=stored.summary,
        details=stored.details,
        source_question=stored.source_question,
        request_id=stored.request_id,
    )
