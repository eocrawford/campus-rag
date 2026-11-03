import os
import secrets
from collections.abc import Iterator

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from campus_rag.config import load_settings

SKIP_REASON = "local database not reachable; run scripts/setup-local-environment.ps1"


@pytest.fixture(scope="session")
def reachable_db_url() -> str | None:
    """Probe the configured database once per session; None if it is unreachable."""
    base_url = load_settings().database_url
    try:
        psycopg.connect(base_url, connect_timeout=3).close()
    except psycopg.OperationalError:
        return None
    return base_url


@pytest.fixture
def db_url(reachable_db_url: str | None) -> Iterator[str]:
    """URL of a throwaway database; the main campus_rag database is never modified."""
    if reachable_db_url is None:
        if os.environ.get("CAMPUS_RAG_REQUIRE_DB") == "1":
            pytest.fail(SKIP_REASON)
        pytest.skip(SKIP_REASON)
    base_url = reachable_db_url
    admin = psycopg.connect(base_url, autocommit=True, connect_timeout=5)

    name = f"campus_rag_test_{secrets.token_hex(6)}"
    try:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        yield make_conninfo(base_url, dbname=name)
    finally:
        admin.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
        )
        admin.close()


@pytest.fixture
def db_name(db_url: str) -> str:
    return conninfo_to_dict(db_url)["dbname"]
