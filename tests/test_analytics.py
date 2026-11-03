"""Request log, feedback, and the usage report on a throwaway database."""

import re
from collections.abc import Iterator

import psycopg
import pytest
from interface_fakes import BrokenConnection

from campus_rag.analytics import __main__ as analytics_cli
from campus_rag.analytics.log import log_request, record_feedback
from campus_rag.analytics.report import build_report, format_report
from campus_rag.config import Settings
from campus_rag.index.migrate import apply_migrations, discover_migrations

pytestmark = pytest.mark.db


@pytest.fixture
def conn(db_url: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(db_url, autocommit=True) as connection:
        apply_migrations(connection, discover_migrations())
        yield connection


def rows(conn: psycopg.Connection, query: str) -> list[tuple]:
    return conn.execute(query).fetchall()  # type: ignore[arg-type]


def answer_row(conn: psycopg.Connection, **overrides) -> int | None:
    fields = {
        "interface": "api",
        "endpoint": "answer",
        "question": "May I appeal a grade?",
        "mode": "hybrid",
        "k": 8,
        "latency_ms": 50_000.4,
        "retrieve_ms": 300.2,
        "generate_ms": 49_700.0,
        "top_score": 0.031,
        "result_count": 6,
        "refused": False,
        "citation_count": 2,
        "answer_model": "qwen2.5:7b-instruct",
        "embedding_model": "nomic-embed-text",
    }
    fields.update(overrides)
    return log_request(conn, **fields)


def test_log_request_stores_every_field(conn: psycopg.Connection) -> None:
    request_id = answer_row(conn, search_query="grade appeal")
    assert request_id is not None
    row = conn.execute(
        "SELECT interface, endpoint, question, search_query, mode, k, latency_ms, retrieve_ms, "
        "generate_ms, top_score, result_count, refused, citation_count, answer_model, "
        "embedding_model, error FROM request_log WHERE id = %s",
        (request_id,),
    ).fetchone()
    assert row == (
        "api",
        "answer",
        "May I appeal a grade?",
        "grade appeal",
        "hybrid",
        8,
        50_000,
        300,
        49_700,
        pytest.approx(0.031),
        6,
        False,
        2,
        "qwen2.5:7b-instruct",
        "nomic-embed-text",
        None,
    )


def test_log_request_clips_long_text(conn: psycopg.Connection) -> None:
    request_id = log_request(
        conn, interface="mcp", endpoint="search_policies", question="q" * 5000, error="e" * 5000
    )
    question, error = conn.execute(
        "SELECT question, error FROM request_log WHERE id = %s", (request_id,)
    ).fetchone()
    assert len(question) == 2000 and question.endswith("\u2026")
    assert len(error) == 500


def test_log_request_never_raises(
    conn: psycopg.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("WARNING"):
        assert log_request(conn, interface="web", endpoint="x") is None  # type: ignore[arg-type]
        assert log_request(BrokenConnection(), interface="api", endpoint="x") is None  # type: ignore[arg-type]
    assert "could not write request_log row" in caplog.text
    assert rows(conn, "SELECT count(*) FROM request_log") == [(0,)]


def test_log_request_survives_a_missing_table(
    db_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    with psycopg.connect(db_url, autocommit=True) as bare, caplog.at_level("WARNING"):
        assert log_request(bare, interface="api", endpoint="search") is None
    assert "request_log" in caplog.text


def test_feedback_is_stored_and_cascades(conn: psycopg.Connection) -> None:
    request_id = answer_row(conn)
    feedback_id = record_feedback(conn, request_id, -1, "Wrong policy.")
    assert feedback_id is not None
    assert rows(conn, "SELECT request_id, rating, comment FROM answer_feedback") == [
        (request_id, -1, "Wrong policy.")
    ]
    conn.execute("DELETE FROM request_log WHERE id = %s", (request_id,))
    assert rows(conn, "SELECT count(*) FROM answer_feedback") == [(0,)]


def test_feedback_for_unknown_request_returns_none(conn: psycopg.Connection) -> None:
    assert record_feedback(conn, 999, 1) is None
    assert rows(conn, "SELECT count(*) FROM answer_feedback") == [(0,)]


def test_feedback_rejects_a_rating_outside_plus_or_minus_one(conn: psycopg.Connection) -> None:
    request_id = answer_row(conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        record_feedback(conn, request_id, 5)


def seed_usage(conn: psycopg.Connection) -> None:
    ok = answer_row(conn, question="How do I appeal?", latency_ms=40_000, citation_count=2)
    refused = answer_row(
        conn, question="Is there a bus pass?", latency_ms=60_000, refused=True, citation_count=0
    )
    answer_row(conn, question="Failed ask", latency_ms=100, refused=None, error="ChatError: down")
    log_request(conn, interface="api", endpoint="search", question="grades", latency_ms=200)
    log_request(conn, interface="api", endpoint="search", question="leave", latency_ms=400)
    log_request(conn, interface="mcp", endpoint="search_policies", question="fees", latency_ms=300)
    record_feedback(conn, ok, 1)
    record_feedback(conn, refused, -1, "No source.")
    record_feedback(conn, refused, -1)


def test_report_summarizes_usage(conn: psycopg.Connection) -> None:
    seed_usage(conn)
    report = build_report(conn, 30)

    assert report.total_requests == 6
    assert sum(row.requests for row in report.volume) == 6
    assert {(row.interface, row.endpoint, row.requests) for row in report.volume} == {
        ("api", "answer", 3),
        ("api", "search", 2),
        ("mcp", "search_policies", 1),
    }
    latency = {(row.interface, row.endpoint): row for row in report.latency}
    assert latency[("api", "answer")].requests == 2  # the errored request is excluded
    assert latency[("api", "answer")].p50_ms == pytest.approx(50_000)
    assert latency[("api", "answer")].p95_ms == pytest.approx(59_000)
    assert latency[("api", "search")].p50_ms == pytest.approx(300)
    assert (report.answers, report.refused) == (2, 1)
    assert report.refusal_rate == pytest.approx(0.5)
    assert report.mean_citations == pytest.approx(2.0)
    assert report.errors == 1
    assert (report.feedback, report.positive_feedback) == (3, 1)
    assert report.positive_share == pytest.approx(1 / 3)
    assert [item.question for item in report.negative_questions] == [
        "Is there a bus pass?",
        "Is there a bus pass?",
    ]
    assert report.negative_questions[1].comment == "No source."


def test_report_limits_negative_questions_to_the_five_most_recent(
    conn: psycopg.Connection,
) -> None:
    for number in range(7):
        request_id = answer_row(conn, question=f"question {number}")
        record_feedback(conn, request_id, -1)
    report = build_report(conn, 30)
    assert [item.question for item in report.negative_questions] == [
        f"question {number}" for number in (6, 5, 4, 3, 2)
    ]


def test_report_only_counts_the_requested_period(conn: psycopg.Connection) -> None:
    old = answer_row(conn, question="old")
    conn.execute("UPDATE request_log SET created_at = now() - interval '40 days'")
    record_feedback(conn, old, 1)
    conn.execute("UPDATE answer_feedback SET created_at = now() - interval '40 days'")
    answer_row(conn, question="recent")
    assert build_report(conn, 30).total_requests == 1
    assert build_report(conn, 60).total_requests == 2
    assert build_report(conn, 30).feedback == 0


def test_report_rejects_a_period_below_one_day(conn: psycopg.Connection) -> None:
    with pytest.raises(ValueError, match="days"):
        build_report(conn, 0)


def test_format_report_for_an_empty_log(conn: psycopg.Connection) -> None:
    text = format_report(build_report(conn, 7))
    assert "last 7 day(s): 0 request(s)" in text
    assert "No requests logged" in text


def test_format_report_prints_each_section(conn: psycopg.Connection) -> None:
    seed_usage(conn)
    text = format_report(build_report(conn, 30))
    for expected in (
        "Requests by day",
        "Latency of successful requests (ms)",
        "refusal rate: 50% (1 of 2)",
        "mean citations per answered question: 2.00",
        "errors: 1",
        "3 rating(s), 33% positive",
        "Is there a bus pass?",
        "comment: No source.",
    ):
        assert expected in text
    assert re.search(r"api\s+answer\s+3\n", text)
    assert re.search(r"api\s+answer\s+2\s+50000\s+59000\n", text)


def test_cli_prints_the_report(
    conn: psycopg.Connection,
    db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seed_usage(conn)
    monkeypatch.setattr(analytics_cli, "load_settings", lambda: Settings(database_url=db_url))
    assert analytics_cli.main(["--days", "7"]) == 0
    assert "last 7 day(s): 6 request(s)" in capsys.readouterr().out


def test_cli_reports_an_out_of_date_schema(
    db_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(analytics_cli, "load_settings", lambda: Settings(database_url=db_url))
    assert analytics_cli.main([]) == analytics_cli.EXIT_SCHEMA_OUT_OF_DATE
    assert "migrate" in capsys.readouterr().err


def test_cli_reports_an_unreachable_database(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    unreachable = Settings(database_url="postgresql://u:p@127.0.0.1:1/none")
    monkeypatch.setattr(analytics_cli, "load_settings", lambda: unreachable)
    assert analytics_cli.main([]) == analytics_cli.EXIT_CONNECTION_FAILED
    assert "cannot connect to PostgreSQL" in capsys.readouterr().err


@pytest.mark.parametrize("value", ["0", "-3", "abc"])
def test_cli_rejects_an_invalid_period(value: str) -> None:
    with pytest.raises(SystemExit) as raised:
        analytics_cli.main(["--days", value])
    assert raised.value.code == 2
