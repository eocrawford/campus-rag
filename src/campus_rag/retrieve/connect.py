"""Open the search database and verify its schema, with the exit codes the commands share."""

import psycopg

from campus_rag.config import Settings
from campus_rag.index.migrate import _describe_target, _first_line
from campus_rag.index.store import schema_version_applied

REQUIRED_SCHEMA_VERSION = 3  # embeddings and the keyword index

EXIT_CONNECTION_FAILED = 2
EXIT_SCHEMA_OUT_OF_DATE = 3
EXIT_EMBEDDING_UNAVAILABLE = 7


class SetupError(Exception):
    """A prerequisite is missing; `exit_code` is the process exit status to report."""

    def __init__(self, message: str, exit_code: int) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def open_connection(settings: Settings) -> psycopg.Connection:
    """Connect (autocommit) and check the schema. Raises `SetupError` with a fix in the message."""
    try:
        conn = psycopg.connect(settings.database_url, autocommit=True, connect_timeout=5)
    except psycopg.OperationalError as exc:
        raise SetupError(
            f"cannot connect to PostgreSQL at {_describe_target(settings.database_url)} "
            f"({_first_line(exc)}). Start it with: docker compose up -d --wait db",
            EXIT_CONNECTION_FAILED,
        ) from None
    try:
        if not schema_version_applied(conn, REQUIRED_SCHEMA_VERSION):
            raise SetupError(
                "database schema is out of date. Run: uv run python -m campus_rag.index.migrate",
                EXIT_SCHEMA_OUT_OF_DATE,
            )
    except BaseException:
        conn.close()
        raise
    return conn
