"""The mock ticketing API on a throwaway database, and its HTTP client."""

import json
from collections.abc import Iterator

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

from campus_rag.analytics.log import log_request
from campus_rag.index.migrate import apply_migrations, discover_migrations
from campus_rag.ticketing import __main__ as ticketing_cli
from campus_rag.ticketing.app import create_app
from campus_rag.ticketing.client import TicketingClient, TicketingError

NEW_TICKET = {
    "category": "policy-question",
    "summary": "Policy question not answered: bus passes",
    "details": "The policy assistant found no policy on bus passes.",
    "source_question": "Is there a bus pass?",
}


@pytest.fixture
def migrated_db(db_url: str) -> Iterator[str]:
    with psycopg.connect(db_url, autocommit=True) as conn:
        apply_migrations(conn, discover_migrations())
    yield db_url


def app_client(url: str) -> TestClient:
    return TestClient(create_app(lambda: psycopg.connect(url, autocommit=True)))


def fetch(url: str, query: str) -> list[tuple]:
    with psycopg.connect(url, autocommit=True) as conn:
        cursor = conn.execute(query)  # type: ignore[arg-type]
        return cursor.fetchall() if cursor.description else []


# the API


@pytest.mark.db
def test_filing_a_ticket_returns_it_with_an_id_and_url(migrated_db: str) -> None:
    response = app_client(migrated_db).post("/tickets", json=NEW_TICKET)
    assert response.status_code == 201
    body = response.json()
    assert body["id"] >= 1
    assert body["url"] == f"http://testserver/tickets/{body['id']}"
    assert response.headers["location"] == body["url"]
    assert body["status"] == "open"
    assert {key: body[key] for key in NEW_TICKET} == NEW_TICKET
    assert body["request_id"] is None
    assert body["created_at"]
    assert fetch(migrated_db, "SELECT status, category, summary FROM mock_tickets") == [
        ("open", "policy-question", NEW_TICKET["summary"])
    ]


@pytest.mark.db
def test_a_ticket_can_link_to_the_request_that_prompted_it(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        request_id = log_request(conn, interface="api", endpoint="answer", question="q")
    body = (
        app_client(migrated_db)
        .post("/tickets", json=NEW_TICKET | {"request_id": request_id})
        .json()
    )
    assert body["request_id"] == request_id

    fetch(migrated_db, f"DELETE FROM request_log WHERE id = {request_id}")
    assert fetch(migrated_db, "SELECT count(*), count(request_id) FROM mock_tickets") == [(1, 0)]


@pytest.mark.db
def test_an_unknown_request_id_is_rejected(migrated_db: str) -> None:
    response = app_client(migrated_db).post("/tickets", json=NEW_TICKET | {"request_id": 4242})
    assert response.status_code == 422
    assert response.json() == {"detail": "unknown request_id 4242"}
    assert fetch(migrated_db, "SELECT count(*) FROM mock_tickets") == [(0,)]


@pytest.mark.db
def test_ticket_text_is_trimmed(migrated_db: str) -> None:
    body = (
        app_client(migrated_db)
        .post("/tickets", json=NEW_TICKET | {"summary": "  Needs a human  ", "details": " d \n"})
        .json()
    )
    assert (body["summary"], body["details"]) == ("Needs a human", "d")


@pytest.mark.parametrize(
    "overrides",
    [
        {"category": "billing"},
        {"category": None},
        {"summary": "abcd"},
        {"summary": "    "},
        {"summary": "s" * 201},
        {"details": ""},
        {"details": "d" * 4001},
        {"source_question": "q" * 2001},
        {"request_id": 0},
    ],
)
def test_invalid_tickets_are_rejected(overrides: dict) -> None:
    client = TestClient(create_app(lambda: pytest.fail("must not open a connection")))
    assert client.post("/tickets", json=NEW_TICKET | overrides).status_code == 422


@pytest.mark.db
def test_tickets_are_listed_newest_first_and_filtered_by_status(migrated_db: str) -> None:
    client = app_client(migrated_db)
    ids = [
        client.post("/tickets", json=NEW_TICKET | {"summary": f"Ticket number {n}"}).json()["id"]
        for n in range(3)
    ]
    fetch(migrated_db, f"UPDATE mock_tickets SET status = 'closed' WHERE id = {ids[0]}")

    listed = client.get("/tickets").json()["tickets"]
    assert [ticket["id"] for ticket in listed] == ids[::-1]
    assert [
        t["id"] for t in client.get("/tickets", params={"status": "open"}).json()["tickets"]
    ] == [
        ids[2],
        ids[1],
    ]
    closed = client.get("/tickets", params={"status": "closed"}).json()["tickets"]
    assert [ticket["id"] for ticket in closed] == [ids[0]]
    assert len(client.get("/tickets", params={"limit": 2}).json()["tickets"]) == 2
    assert listed[0]["url"] == f"http://testserver/tickets/{ids[2]}"


@pytest.mark.parametrize("params", [{"status": "pending"}, {"limit": 0}, {"limit": 201}])
def test_list_rejects_invalid_filters(params: dict) -> None:
    client = TestClient(create_app(lambda: pytest.fail("must not open a connection")))
    assert client.get("/tickets", params=params).status_code == 422


@pytest.mark.db
def test_a_ticket_is_read_by_id(migrated_db: str) -> None:
    client = app_client(migrated_db)
    created = client.post("/tickets", json=NEW_TICKET).json()
    assert client.get(f"/tickets/{created['id']}").json() == created
    missing = client.get("/tickets/9999")
    assert missing.status_code == 404
    assert missing.json() == {"detail": "unknown ticket 9999"}


@pytest.mark.db
def test_health_is_ok_on_a_migrated_database(migrated_db: str) -> None:
    response = app_client(migrated_db).get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.db
def test_health_and_requests_say_to_migrate_when_the_table_is_missing(db_url: str) -> None:
    client = app_client(db_url)
    health = client.get("/health")
    assert health.status_code == 503 and "migrate" in health.json()["detail"]
    response = client.post("/tickets", json=NEW_TICKET)
    assert response.status_code == 503 and "migrate" in response.json()["detail"]


def test_an_unreachable_database_returns_503_without_details() -> None:
    def connect() -> psycopg.Connection:
        raise psycopg.OperationalError("connection refused\nDETAIL: secret detail")

    client = TestClient(create_app(connect))
    for response in (
        client.post("/tickets", json=NEW_TICKET),
        client.get("/tickets"),
        client.get("/tickets/1"),
    ):
        assert response.status_code == 503
        assert response.json() == {"detail": "database error: connection refused"}
    health = client.get("/health")
    assert health.status_code == 503 and health.json()["status"] == "degraded"


def test_unexpected_errors_return_a_generic_500() -> None:
    def connect() -> psycopg.Connection:
        raise RuntimeError("secret detail")

    client = TestClient(create_app(connect), raise_server_exceptions=False)
    response = client.get("/tickets")
    assert response.status_code == 500
    assert response.json() == {"detail": "internal server error"}


def test_every_operation_is_documented() -> None:
    client = TestClient(create_app(lambda: pytest.fail("must not open a connection")))
    schema = client.get("/openapi.json").json()
    operations = {
        (path, method): operation
        for path, methods in schema["paths"].items()
        for method, operation in methods.items()
    }
    assert set(operations) == {
        ("/health", "get"),
        ("/tickets", "post"),
        ("/tickets", "get"),
        ("/tickets/{ticket_id}", "get"),
    }
    assert all(op.get("summary") and op.get("description") for op in operations.values())


# the client


def mock_client(handler) -> TicketingClient:
    transport = httpx.MockTransport(handler)
    return TicketingClient("http://tickets.test/", client=httpx.Client(transport=transport))


def test_client_posts_the_ticket_and_returns_what_was_filed() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            201,
            json={
                "id": 7,
                "url": "http://tickets.test/tickets/7",
                "status": "open",
                "category": "it-help",
                "summary": "Needs a human",
                "details": "d",
            },
        )

    filed = mock_client(handler).create_ticket(
        category="it-help", summary="Needs a human", details="d", source_question="q", request_id=3
    )
    assert (filed.id, filed.url, filed.status) == (7, "http://tickets.test/tickets/7", "open")
    (request,) = seen
    assert request.method == "POST" and str(request.url) == "http://tickets.test/tickets"
    assert json.loads(request.content) == {
        "category": "it-help",
        "summary": "Needs a human",
        "details": "d",
        "source_question": "q",
        "request_id": 3,
    }


def test_client_reports_a_rejected_ticket() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422, json={"detail": [{"loc": ["body", "summary"], "msg": "too short"}]}
        )

    with pytest.raises(TicketingError, match=r"rejected the ticket \(HTTP 422\): invalid summary"):
        mock_client(handler).create_ticket(category="other", summary="abcde", details="d")


def test_client_reports_a_plain_error_detail_and_server_failures() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": "unknown request_id 9"})

    with pytest.raises(TicketingError, match="unknown request_id 9"):
        mock_client(refuse).create_ticket(category="other", summary="abcde", details="d")

    def crash(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="<html>boom</html>")

    with pytest.raises(TicketingError, match=r"HTTP 500"):
        mock_client(crash).create_ticket(category="other", summary="abcde", details="d")


def test_client_names_the_fix_when_the_api_is_not_running() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(TicketingError) as raised:
        mock_client(handler).create_ticket(category="other", summary="abcde", details="d")
    message = str(raised.value)
    assert "cannot reach the ticketing API at http://tickets.test" in message
    assert "uv run python -m campus_rag.ticketing" in message


def test_client_reports_a_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(TicketingError, match="timed out"):
        mock_client(handler).create_ticket(category="other", summary="abcde", details="d")


def test_client_rejects_an_unreadable_success_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"unexpected": True})

    with pytest.raises(TicketingError, match="unreadable"):
        mock_client(handler).create_ticket(category="other", summary="abcde", details="d")


@pytest.mark.db
def test_client_files_a_ticket_through_the_app_into_the_database(migrated_db: str) -> None:
    http = app_client(migrated_db)  # a TestClient is an httpx.Client
    filed = TicketingClient("http://testserver", client=http).create_ticket(
        category="policy-question",
        summary="Policy question not answered: bus passes",
        details="No policy found.",
        source_question="Is there a bus pass?",
    )
    assert filed.url == f"http://testserver/tickets/{filed.id}"
    assert fetch(migrated_db, "SELECT id, status, source_question FROM mock_tickets") == [
        (filed.id, "open", "Is there a bus pass?")
    ]


# startup guard


def test_non_loopback_host_is_refused_without_allow_remote(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    started: list[dict] = []
    monkeypatch.setattr(ticketing_cli.uvicorn, "run", lambda app, **kwargs: started.append(kwargs))
    with pytest.raises(SystemExit) as raised:
        ticketing_cli.main(["--host", "0.0.0.0"])
    assert raised.value.code == 2
    assert "refusing to bind '0.0.0.0'" in capsys.readouterr().err
    assert started == []


def test_defaults_bind_loopback_port_8001_and_allow_remote_warns(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    started: list[dict] = []
    monkeypatch.setattr(ticketing_cli.uvicorn, "run", lambda app, **kwargs: started.append(kwargs))
    assert ticketing_cli.main([]) == 0
    assert started == [{"host": "127.0.0.1", "port": 8001}]
    assert ticketing_cli.main(["--host", "0.0.0.0", "--allow-remote"]) == 0
    assert started[-1] == {"host": "0.0.0.0", "port": 8001}
    assert "no authentication" in capsys.readouterr().err


@pytest.mark.parametrize("port", ["0", "65536", "abc"])
def test_invalid_port_is_rejected(port: str) -> None:
    with pytest.raises(SystemExit) as raised:
        ticketing_cli.main(["--port", port])
    assert raised.value.code == 2
