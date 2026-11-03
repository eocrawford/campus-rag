"""Record one row per API request or MCP tool call, and user ratings of answers.

The log holds no user identity. `log_request` never raises, so a logging failure cannot fail the
request it describes.
"""

import logging
from typing import Literal

import psycopg

from campus_rag.index.migrate import _first_line

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 4  # the migration that creates request_log and answer_feedback

Interface = Literal["api", "mcp", "cli"]

MAX_QUESTION_CHARS = 2000
MAX_ERROR_CHARS = 500
MAX_COMMENT_CHARS = 1000  # also enforced by the answer_feedback CHECK constraint


def log_request(
    conn: psycopg.Connection,
    *,
    interface: Interface,
    endpoint: str,
    question: str | None = None,
    search_query: str | None = None,
    mode: str | None = None,
    k: int | None = None,
    latency_ms: float | None = None,
    retrieve_ms: float | None = None,
    generate_ms: float | None = None,
    top_score: float | None = None,
    result_count: int | None = None,
    refused: bool | None = None,
    citation_count: int | None = None,
    answer_model: str | None = None,
    embedding_model: str | None = None,
    error: str | None = None,
) -> int | None:
    """Insert a `request_log` row and return its id, or None (with a warning) if that fails."""
    try:
        with conn.transaction():
            row = conn.execute(
                "INSERT INTO request_log (interface, endpoint, question, search_query, mode, k, "
                "latency_ms, retrieve_ms, generate_ms, top_score, result_count, refused, "
                "citation_count, answer_model, embedding_model, error) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "RETURNING id",
                (
                    interface,
                    endpoint,
                    _clip(question, MAX_QUESTION_CHARS),
                    _clip(search_query, MAX_QUESTION_CHARS),
                    mode,
                    k,
                    _whole(latency_ms),
                    _whole(retrieve_ms),
                    _whole(generate_ms),
                    top_score,
                    result_count,
                    refused,
                    citation_count,
                    answer_model,
                    embedding_model,
                    _clip(error, MAX_ERROR_CHARS),
                ),
            ).fetchone()
    except Exception as exc:  # logging must never fail the request it describes
        logger.warning("could not write request_log row: %s", describe_error(exc))
        return None
    return int(row[0]) if row else None


def record_feedback(
    conn: psycopg.Connection, request_id: int, rating: int, comment: str | None = None
) -> int | None:
    """Store a +1 or -1 rating for a logged request; None if `request_id` is not in the log.

    Unlike `log_request`, database errors propagate: the caller asked for the rating to be saved.
    """
    with conn.transaction():
        row = conn.execute(
            "INSERT INTO answer_feedback (request_id, rating, comment) "
            "SELECT id, %s, %s FROM request_log WHERE id = %s RETURNING id",
            (rating, _clip(comment, MAX_COMMENT_CHARS), request_id),
        ).fetchone()
    return int(row[0]) if row else None


def _clip(text: str | None, limit: int) -> str | None:
    if text is None:
        return None
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


def _whole(milliseconds: float | None) -> int | None:
    return None if milliseconds is None else round(milliseconds)


def describe_error(exc: Exception) -> str:
    return _first_line(exc) if isinstance(exc, psycopg.Error) else f"{type(exc).__name__}: {exc}"
