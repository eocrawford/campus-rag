"""`LiveServices`: scopes, health, and the model-check cache, with the model servers faked."""

from dataclasses import replace

import psycopg
import pytest
from fakes import make_chunk

from campus_rag.config import Settings
from campus_rag.index.migrate import apply_migrations, discover_migrations
from campus_rag.retrieve.connect import SetupError
from campus_rag.retrieve.search import PolicySearcher
from campus_rag.services import (
    HEALTH_FAILURE_SECONDS,
    HEALTH_OK_SECONDS,
    ComponentStatus,
    LiveServices,
    passage_body,
)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def services_for(url: str, clock: Clock | None = None) -> LiveServices:
    return LiveServices(Settings(database_url=url), clock=clock or Clock())


def stub_model_checks(services: LiveServices, embedding_ok: bool = True, chat_ok: bool = True):
    services._check_embedding = lambda: ComponentStatus(embedding_ok, "embedding detail")  # type: ignore[method-assign]
    services._check_chat = lambda: ComponentStatus(chat_ok, "chat detail")  # type: ignore[method-assign]


def test_model_checks_are_cached_briefly() -> None:
    clock = Clock()
    services = services_for("postgresql://u:p@127.0.0.1:1/none", clock)
    calls: list[int] = []

    def check() -> ComponentStatus:
        calls.append(1)
        return ComponentStatus(True, "ok")

    services._cached_check("chat", check)
    clock.now += HEALTH_OK_SECONDS - 1
    services._cached_check("chat", check)
    assert len(calls) == 1
    clock.now += 2
    services._cached_check("chat", check)
    assert len(calls) == 2
    services.close()


def test_failed_model_checks_are_retried_sooner() -> None:
    clock = Clock()
    services = services_for("postgresql://u:p@127.0.0.1:1/none", clock)
    calls: list[int] = []

    def check() -> ComponentStatus:
        calls.append(1)
        return ComponentStatus(False, "down")

    services._cached_check("embedding", check)
    clock.now += HEALTH_FAILURE_SECONDS - 1
    services._cached_check("embedding", check)
    assert len(calls) == 1
    clock.now += 2
    services._cached_check("embedding", check)
    assert len(calls) == 2
    services.close()


def test_health_reports_an_unreachable_database() -> None:
    services = services_for("postgresql://u:p@127.0.0.1:1/none")
    stub_model_checks(services)
    report = services.health()
    assert not report.ok
    assert report.database.ok is False and "cannot connect" in report.database.detail
    assert report.schema_version is None
    assert report.embedding.ok and report.chat.ok
    services.close()


def test_health_reports_a_failed_model_check() -> None:
    services = services_for("postgresql://u:p@127.0.0.1:1/none")
    stub_model_checks(services, chat_ok=False)
    report = services.health()
    assert report.chat == ComponentStatus(False, "chat detail")
    assert report.embedding.ok
    services.close()


@pytest.mark.db
def test_health_on_a_migrated_database(db_url: str) -> None:
    with psycopg.connect(db_url, autocommit=True) as conn:
        apply_migrations(conn, discover_migrations())
    services = services_for(db_url)
    stub_model_checks(services)
    report = services.health()
    assert report.ok
    assert report.schema_version == max(m.version for m in discover_migrations())
    assert report.database.detail == f"schema version {report.schema_version}"
    services.close()


@pytest.mark.db
def test_health_on_an_unmigrated_database(db_url: str) -> None:
    services = services_for(db_url)
    stub_model_checks(services)
    report = services.health()
    assert report.database.ok is False
    assert "migrate" in report.database.detail
    assert report.schema_version is None
    services.close()


@pytest.mark.db
def test_scope_gives_a_connection_and_closes_it(db_url: str) -> None:
    with psycopg.connect(db_url, autocommit=True) as conn:
        apply_migrations(conn, discover_migrations())
    services = services_for(db_url)
    with services.open_scope() as scope:
        assert isinstance(scope.searcher, PolicySearcher)
        assert scope.conn.execute("SELECT 1").fetchone() == (1,)
    assert scope.conn.closed
    services.close()


@pytest.mark.db
def test_scope_requires_a_migrated_database(db_url: str) -> None:
    services = services_for(db_url)
    with pytest.raises(SetupError, match="schema is out of date"):
        with services.open_scope():
            pass
    services.close()


def test_scope_reports_an_unreachable_database() -> None:
    services = services_for("postgresql://u:p@127.0.0.1:1/none")
    with pytest.raises(SetupError, match="cannot connect to PostgreSQL"):
        with services.open_scope():
            pass
    services.close()


def test_passage_body_drops_the_heading_line() -> None:
    chunk = make_chunk(1, content="Body text.")
    assert passage_body(chunk) == "Body text."
    assert passage_body(replace(chunk, content="No heading prefix.")) == "No heading prefix."
