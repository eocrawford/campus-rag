"""The HTTP API with fake search and chat; logging and feedback run on a throwaway database."""

import logging
from collections.abc import Iterator

import psycopg
import pytest
from fakes import FakeChat, FakeSearcher, make_chunk
from fastapi.testclient import TestClient
from interface_fakes import HEALTHY, FakeServices, RaisingSearcher

from campus_rag.api import __main__ as api_cli
from campus_rag.api.app import create_app
from campus_rag.generate.chat import ChatError
from campus_rag.generate.models import REFUSAL_TEXT
from campus_rag.index.embed import EmbeddingError
from campus_rag.index.migrate import apply_migrations, discover_migrations
from campus_rag.loopback import is_loopback_host
from campus_rag.retrieve.connect import EXIT_CONNECTION_FAILED, SetupError
from campus_rag.retrieve.models import SearchFilters
from campus_rag.services import ComponentStatus, Health


@pytest.fixture
def migrated_db(db_url: str) -> Iterator[str]:
    with psycopg.connect(db_url, autocommit=True) as conn:
        apply_migrations(conn, discover_migrations())
    yield db_url


def client_for(services: FakeServices) -> TestClient:
    return TestClient(create_app(lambda: services))


def log_rows(db_url: str, columns: str = "interface, endpoint, question, refused, error"):
    with psycopg.connect(db_url, autocommit=True) as conn:
        return conn.execute(f"SELECT {columns} FROM request_log ORDER BY id").fetchall()  # type: ignore[arg-type]


# health


def test_health_ok() -> None:
    response = client_for(FakeServices()).get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["schema_version"] == 4
    assert body["database"] == {"ok": True, "detail": "schema version 4"}


def test_health_degraded_returns_503_with_the_same_body() -> None:
    degraded = Health(
        database=HEALTHY.database,
        embedding=ComponentStatus(False, "cannot reach the embedding server"),
        chat=HEALTHY.chat,
        schema_version=4,
    )
    response = client_for(FakeServices(health=degraded)).get("/health")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["embedding"]["ok"] is False
    assert "embedding server" in body["embedding"]["detail"]


# search


def test_search_returns_ranked_results_and_passes_filters() -> None:
    searcher = FakeSearcher(
        [
            make_chunk(1, content="word " * 200, keyword_rank=1, vector_rank=3),
            make_chunk(2, vector_rank=1),
        ]
    )
    response = client_for(FakeServices(searcher=searcher)).post(
        "/search",
        json={
            "query": "  grade appeal  ",
            "mode": "keyword",
            "k": 2,
            "sources": ["sample"],
            "url_prefixes": ["https://policy.example.edu/"],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["query"] == "grade appeal"
    assert body["mode"] == "keyword"
    assert [result["rank"] for result in body["results"]] == [1, 2]
    first = body["results"][0]
    assert first["keyword_rank"] == 1 and first["vector_rank"] == 3
    assert first["url"] == "https://policy.example.edu/sample-1/"
    assert first["title"] == "Sample Policy 1"
    assert first["heading_path"] == "Sample Policy 1 > Section 1"
    assert first["content_preview"].startswith("word word")  # heading line removed
    assert len(first["content_preview"]) <= 300
    assert first["content_preview"].endswith("\u2026")
    assert body["results"][1]["keyword_rank"] is None
    assert searcher.calls == [
        {
            "query": "grade appeal",
            "mode": "keyword",
            "k": 2,
            "filters": SearchFilters(
                sources=("sample",), url_prefixes=("https://policy.example.edu/",)
            ),
        }
    ]


def test_search_defaults_to_hybrid_and_eight_results() -> None:
    searcher = FakeSearcher([make_chunk(1)])
    response = client_for(FakeServices(searcher=searcher)).post("/search", json={"query": "q"})
    assert response.status_code == 200
    assert searcher.calls[0]["mode"] == "hybrid" and searcher.calls[0]["k"] == 8
    assert searcher.calls[0]["filters"] == SearchFilters()


def test_search_still_answers_when_logging_fails(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        response = client_for(FakeServices(searcher=FakeSearcher([make_chunk(1)]))).post(
            "/search", json={"query": "q"}
        )
    assert response.status_code == 200
    assert response.json()["request_id"] is None
    assert "could not write request_log row" in caplog.text


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"query": ""},
        {"query": "   "},
        {"query": "q", "k": 0},
        {"query": "q", "k": 51},
        {"query": "q", "mode": "fuzzy"},
        {"query": "q" * 2001},
        {"query": "q", "sources": "uw-policy"},
    ],
)
def test_search_rejects_invalid_requests(payload: dict) -> None:
    response = client_for(FakeServices()).post("/search", json=payload)
    assert response.status_code == 422


@pytest.mark.db
def test_search_is_logged(migrated_db: str) -> None:
    searcher = FakeSearcher([make_chunk(1, score=0.5), make_chunk(2)])
    response = client_for(FakeServices(searcher=searcher, db_url=migrated_db)).post(
        "/search", json={"query": "grade appeal", "k": 5}
    )
    body = response.json()
    assert body["request_id"] is not None
    assert log_rows(
        migrated_db, "id, interface, endpoint, question, mode, k, top_score, result_count"
    ) == [(body["request_id"], "api", "search", "grade appeal", "hybrid", 5, 0.5, 2)]


@pytest.mark.db
def test_search_embedding_failure_returns_503_and_is_logged(migrated_db: str) -> None:
    services = FakeServices(
        searcher=RaisingSearcher(EmbeddingError("cannot reach the embedding server at x")),
        db_url=migrated_db,
    )
    response = client_for(services).post("/search", json={"query": "q"})
    assert response.status_code == 503
    assert "cannot reach the embedding server" in response.json()["detail"]
    ((interface, endpoint, _, _, error),) = log_rows(migrated_db)
    assert (interface, endpoint) == ("api", "search")
    assert error.startswith("EmbeddingError: cannot reach")


def test_search_database_failure_returns_503() -> None:
    error = psycopg.OperationalError("server closed the connection\nDETAIL: secret detail")
    response = client_for(FakeServices(searcher=RaisingSearcher(error))).post(
        "/search", json={"query": "q"}
    )
    assert response.status_code == 503
    assert response.json() == {"detail": "database error: server closed the connection"}


def test_unreachable_database_returns_503_with_the_fix() -> None:
    error = SetupError("cannot connect to PostgreSQL at 127.0.0.1:5433/x", EXIT_CONNECTION_FAILED)
    response = client_for(FakeServices(open_error=error)).post("/search", json={"query": "q"})
    assert response.status_code == 503
    assert response.json() == {"detail": "cannot connect to PostgreSQL at 127.0.0.1:5433/x"}


def test_unexpected_error_returns_500_without_details(caplog: pytest.LogCaptureFixture) -> None:
    services = FakeServices(searcher=RaisingSearcher(RuntimeError("secret internal detail")))
    client = TestClient(create_app(lambda: services), raise_server_exceptions=False)
    with caplog.at_level(logging.ERROR):
        response = client.post("/search", json={"query": "q"})
    assert response.status_code == 500
    assert response.json() == {"detail": "internal server error"}
    assert "secret internal detail" not in response.text
    assert "secret internal detail" in caplog.text


# answer


def answered_services(replies: list[str], db_url: str | None = None) -> FakeServices:
    return FakeServices(
        searcher=FakeSearcher([make_chunk(1), make_chunk(2)]),
        chat=FakeChat(replies),
        db_url=db_url,
    )


@pytest.mark.db
def test_answer_with_citations_is_logged(migrated_db: str) -> None:
    services = answered_services(["Rule one applies [1]. Rule two applies too [2]."], migrated_db)
    response = client_for(services).post(
        "/answer", json={"question": "What rules apply?", "k": 4, "mode": "keyword"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["answer"] == "Rule one applies [1]. Rule two applies too [2]."
    assert body["refused"] is False and body["suggest_ticket"] is False
    assert body["citations"] == [
        {
            "number": 1,
            "title": "Sample Policy 1",
            "heading_path": "Sample Policy 1 > Section 1",
            "url": "https://policy.example.edu/sample-1/",
        },
        {
            "number": 2,
            "title": "Sample Policy 2",
            "heading_path": "Sample Policy 2 > Section 2",
            "url": "https://policy.example.edu/sample-2/",
        },
    ]
    assert body["search_query"] == "What rules apply?"
    assert body["model"] == services.settings.chat.answer_model
    assert set(body["timings_ms"]) == {"retrieve", "generate", "total"}
    assert services.searcher.calls[0]["mode"] == "keyword" and services.searcher.calls[0]["k"] == 4
    assert log_rows(
        migrated_db,
        "id, interface, endpoint, question, mode, k, refused, citation_count, result_count, "
        "answer_model, embedding_model, top_score, error",
    ) == [
        (
            body["request_id"],
            "api",
            "answer",
            "What rules apply?",
            "keyword",
            4,
            False,
            2,
            2,
            services.settings.chat.answer_model,
            "nomic-embed-text",
            1.0,
            None,
        )
    ]


@pytest.mark.db
def test_refused_answer_suggests_a_ticket(migrated_db: str) -> None:
    response = client_for(answered_services([REFUSAL_TEXT], migrated_db)).post(
        "/answer", json={"question": "Is there a bus pass?"}
    )
    body = response.json()
    assert response.status_code == 200
    assert body["refused"] is True and body["suggest_ticket"] is True
    assert body["answer"] == REFUSAL_TEXT
    assert body["citations"] == []
    request_id = body["request_id"]
    assert body["ticket_draft"] == {
        "category": "policy-question",
        "summary": "Policy question not answered: Is there a bus pass?",
        "details": (
            "The policy assistant could not answer this question from the indexed policies."
            "\n\nQuestion: Is there a bus pass?"
        ),
        "source_question": "Is there a bus pass?",
        "request_id": request_id,
    }
    assert log_rows(migrated_db)[0][3] is True


def test_ticket_draft_fits_the_ticket_limits_and_names_a_rewritten_query() -> None:
    services = FakeServices(searcher=FakeSearcher([]), chat=FakeChat(["rewritten query"]))
    question = "Is there a bus pass? " * 90
    body = (
        client_for(services)
        .post(
            "/answer",
            json={
                "question": question,
                "history": [{"role": "user", "content": "Earlier question."}],
            },
        )
        .json()
    )
    draft = body["ticket_draft"]
    assert 5 <= len(draft["summary"]) <= 200 and draft["summary"].endswith("\u2026")
    assert len(draft["details"]) <= 4000
    assert draft["source_question"] == question.strip()
    assert "Search query used: rewritten query" in draft["details"]


def test_answered_question_has_no_ticket_draft() -> None:
    body = (
        client_for(answered_services(["Rule one applies [1]."]))
        .post("/answer", json={"question": "What rules apply?"})
        .json()
    )
    assert body["refused"] is False and body["ticket_draft"] is None


def test_answer_without_passages_is_a_refusal() -> None:
    services = FakeServices(searcher=FakeSearcher([]), chat=FakeChat())
    body = client_for(services).post("/answer", json={"question": "Anything?"}).json()
    assert body["refused"] is True and body["suggest_ticket"] is True
    assert services.chat.calls == []


def test_follow_up_is_rewritten_using_the_history() -> None:
    services = answered_services(["leave of absence for graduate students", "It applies [1]."])
    response = client_for(services).post(
        "/answer",
        json={
            "question": "Does that apply to graduate students?",
            "history": [
                {"role": "user", "content": "How long may a student take a leave of absence?"},
                {"role": "assistant", "content": "Up to one year [1]."},
            ],
        },
    )
    body = response.json()
    assert body["search_query"] == "leave of absence for graduate students"
    assert services.searcher.calls[0]["query"] == "leave of absence for graduate students"
    assert "rewrite" in body["timings_ms"]


@pytest.mark.db
def test_answer_model_failure_returns_503_and_is_logged(migrated_db: str) -> None:
    services = answered_services([ChatError("cannot reach the chat server at x")], migrated_db)
    response = client_for(services).post("/answer", json={"question": "What rules apply?"})
    assert response.status_code == 503
    assert "cannot reach the chat server" in response.json()["detail"]
    ((_, endpoint, question, refused, error),) = log_rows(migrated_db)
    assert (endpoint, question, refused) == ("answer", "What rules apply?", None)
    assert error.startswith("ChatError: cannot reach")


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"question": ""},
        {"question": "q", "k": 0},
        {"question": "q", "mode": "fuzzy"},
        {"question": "q", "history": [{"role": "system", "content": "x"}]},
        {"question": "q", "history": [{"role": "user"}]},
        {"question": "q", "history": [{"role": "user", "content": "x"}] * 21},
    ],
)
def test_answer_rejects_invalid_requests(payload: dict) -> None:
    assert client_for(FakeServices()).post("/answer", json=payload).status_code == 422


# feedback


@pytest.mark.db
def test_feedback_is_stored(migrated_db: str) -> None:
    client = client_for(answered_services(["Rule one applies [1]."], migrated_db))
    request_id = client.post("/answer", json={"question": "What rules apply?"}).json()["request_id"]
    response = client.post(
        "/feedback", json={"request_id": request_id, "rating": -1, "comment": "  Wrong policy. "}
    )
    assert response.status_code == 201
    assert response.json()["request_id"] == request_id
    with psycopg.connect(migrated_db) as conn:
        rows = conn.execute(
            "SELECT id, request_id, rating, comment FROM answer_feedback"
        ).fetchall()
    assert rows == [(response.json()["feedback_id"], request_id, -1, "Wrong policy.")]


@pytest.mark.db
def test_feedback_for_an_unknown_request_returns_404(migrated_db: str) -> None:
    response = client_for(FakeServices(db_url=migrated_db)).post(
        "/feedback", json={"request_id": 12345, "rating": 1}
    )
    assert response.status_code == 404
    assert response.json() == {"detail": "unknown request_id 12345"}


@pytest.mark.parametrize(
    "payload",
    [
        {"request_id": 1, "rating": 0},
        {"request_id": 1, "rating": 2},
        {"request_id": 0, "rating": 1},
        {"request_id": 1},
        {"request_id": 1, "rating": 1, "comment": "c" * 1001},
    ],
)
def test_feedback_rejects_invalid_requests(payload: dict) -> None:
    assert client_for(FakeServices()).post("/feedback", json=payload).status_code == 422


def test_feedback_with_the_database_down_returns_503() -> None:
    error = SetupError("cannot connect to PostgreSQL", EXIT_CONNECTION_FAILED)
    response = client_for(FakeServices(open_error=error)).post(
        "/feedback", json={"request_id": 1, "rating": 1}
    )
    assert response.status_code == 503


def test_feedback_without_the_log_tables_says_to_migrate(db_url: str) -> None:
    response = client_for(FakeServices(db_url=db_url)).post(
        "/feedback", json={"request_id": 1, "rating": 1}
    )
    assert response.status_code == 503
    assert "migrate" in response.json()["detail"]


# OpenAPI


def test_every_operation_is_documented() -> None:
    client = client_for(FakeServices())
    schema = client.get("/openapi.json").json()
    operations = [
        (path, method, operation)
        for path, methods in schema["paths"].items()
        for method, operation in methods.items()
    ]
    assert {(path, method) for path, method, _ in operations} == {
        ("/health", "get"),
        ("/search", "post"),
        ("/answer", "post"),
        ("/feedback", "post"),
    }
    for path, method, operation in operations:
        assert operation.get("summary"), f"{method} {path} has no summary"
        assert operation.get("description"), f"{method} {path} has no description"
    assert schema["components"]["schemas"]["SearchRequest"]["examples"]
    assert client.get("/docs").status_code == 200


def test_services_are_closed_on_shutdown() -> None:
    services = FakeServices()
    with TestClient(create_app(lambda: services)) as client:
        assert client.get("/health").status_code == 200
        assert not services.closed
    assert services.closed


# startup guard


@pytest.mark.parametrize(
    ("host", "loopback"),
    [
        ("127.0.0.1", True),
        ("127.5.6.7", True),
        ("::1", True),
        ("[::1]", True),
        ("localhost", True),
        ("LOCALHOST", True),
        ("0.0.0.0", False),
        ("192.168.1.20", False),
        ("::", False),
        ("example.com", False),
    ],
)
def test_loopback_detection(host: str, loopback: bool) -> None:
    assert is_loopback_host(host) is loopback


def test_non_loopback_host_is_refused_without_allow_remote(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    started: list[dict] = []
    monkeypatch.setattr(api_cli.uvicorn, "run", lambda app, **kwargs: started.append(kwargs))
    with pytest.raises(SystemExit) as raised:
        api_cli.main(["--host", "0.0.0.0"])
    assert raised.value.code == 2
    error = capsys.readouterr().err
    assert "refusing to bind '0.0.0.0'" in error
    assert "UW's permission" in error
    assert started == []


def test_allow_remote_binds_with_a_warning(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    started: list[dict] = []
    monkeypatch.setattr(api_cli.uvicorn, "run", lambda app, **kwargs: started.append(kwargs))
    assert api_cli.main(["--host", "0.0.0.0", "--port", "9000", "--allow-remote"]) == 0
    assert started == [{"host": "0.0.0.0", "port": 9000}]
    assert "no authentication" in capsys.readouterr().err


def test_default_binding_is_loopback_port_8000(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[dict] = []
    monkeypatch.setattr(api_cli.uvicorn, "run", lambda app, **kwargs: started.append(kwargs))
    assert api_cli.main([]) == 0
    assert started == [{"host": "127.0.0.1", "port": 8000}]


@pytest.mark.parametrize("port", ["0", "65536", "abc"])
def test_invalid_port_is_rejected(port: str) -> None:
    with pytest.raises(SystemExit) as raised:
        api_cli.main(["--port", port])
    assert raised.value.code == 2
