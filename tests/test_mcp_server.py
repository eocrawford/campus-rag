"""The MCP server, driven in-process through the SDK client with fake search."""

import asyncio
from collections.abc import Callable, Iterator
from typing import Any

import psycopg
import pytest
from fakes import FakeSearcher, make_chunk
from interface_fakes import FakeServices, FakeTicketing, RaisingSearcher
from mcp.client import Client

from campus_rag.index.embed import EmbeddingError
from campus_rag.index.migrate import apply_migrations, discover_migrations
from campus_rag.mcp_server import build_server
from campus_rag.retrieve.connect import EXIT_CONNECTION_FAILED, SetupError
from campus_rag.ticketing.client import TicketingError


def run_client[T](
    services: FakeServices,
    action: Callable[[Client], Any],
    ticketing: FakeTicketing | None = None,
) -> T:
    async def session() -> T:
        async with Client(build_server(services, ticketing or FakeTicketing())) as client:
            return await action(client)

    return asyncio.run(session())


def call(
    services: FakeServices,
    arguments: dict[str, Any],
    *,
    tool: str = "search_policies",
    ticketing: FakeTicketing | None = None,
):
    return run_client(services, lambda client: client.call_tool(tool, arguments), ticketing)


@pytest.fixture
def migrated_db(db_url: str) -> Iterator[str]:
    with psycopg.connect(db_url, autocommit=True) as conn:
        apply_migrations(conn, discover_migrations())
    yield db_url


def test_lists_both_tools() -> None:
    listed = run_client(FakeServices(), lambda client: client.list_tools())
    assert [tool.name for tool in listed.tools] == ["search_policies", "draft_ticket"]


def test_search_tool_tells_agents_to_cite() -> None:
    listed = run_client(FakeServices(), lambda client: client.list_tools())
    tool = listed.tools[0]
    assert "cite" in tool.description.lower() and "URL" in tool.description
    properties = tool.input_schema["properties"]
    assert tool.input_schema["required"] == ["query"]
    assert properties["k"]["default"] == 5
    assert properties["mode"]["enum"] == ["keyword", "vector", "hybrid"]
    assert properties["mode"]["default"] == "hybrid"


def test_returns_ranked_passages_with_text_and_urls() -> None:
    searcher = FakeSearcher([make_chunk(1), make_chunk(2)])
    result = call(FakeServices(searcher=searcher), {"query": "  grade appeal  "})
    assert result.is_error is False
    assert result.structured_content == {
        "query": "grade appeal",
        "mode": "hybrid",
        "results": [
            {
                "rank": 1,
                "score": 1.0,
                "title": "Sample Policy 1",
                "heading_path": "Sample Policy 1 > Section 1",
                "url": "https://policy.example.edu/sample-1/",
                "text": "Placeholder rule number 1 applies.",
            },
            {
                "rank": 2,
                "score": 0.5,
                "title": "Sample Policy 2",
                "heading_path": "Sample Policy 2 > Section 2",
                "url": "https://policy.example.edu/sample-2/",
                "text": "Placeholder rule number 2 applies.",
            },
        ],
    }
    assert "https://policy.example.edu/sample-1/" in result.content[0].text
    assert searcher.calls == [{"query": "grade appeal", "mode": "hybrid", "k": 5, "filters": None}]


def test_passes_k_and_mode_to_the_searcher() -> None:
    searcher = FakeSearcher([make_chunk(1), make_chunk(2), make_chunk(3)])
    result = call(FakeServices(searcher=searcher), {"query": "q", "k": 2, "mode": "keyword"})
    assert [item["rank"] for item in result.structured_content["results"]] == [1, 2]
    assert (searcher.calls[0]["mode"], searcher.calls[0]["k"]) == ("keyword", 2)


def test_no_matches_returns_an_empty_list() -> None:
    result = call(FakeServices(searcher=FakeSearcher([])), {"query": "q"})
    assert result.is_error is False
    assert result.structured_content["results"] == []


@pytest.mark.parametrize(
    "arguments",
    [
        {"query": ""},
        {"query": "   "},
        {"query": "q", "k": 0},
        {"query": "q", "k": 51},
        {"query": "q", "mode": "fuzzy"},
        {},
    ],
)
def test_invalid_arguments_are_tool_errors(arguments: dict[str, Any]) -> None:
    searcher = FakeSearcher([make_chunk(1)])
    result = call(FakeServices(searcher=searcher), arguments)
    assert result.is_error is True
    assert searcher.calls == []


def test_embedding_failure_is_a_tool_error_with_the_fix() -> None:
    searcher = RaisingSearcher(EmbeddingError("cannot reach the embedding server at x"))
    result = call(FakeServices(searcher=searcher), {"query": "q"})
    assert result.is_error is True
    assert "cannot reach the embedding server" in result.content[0].text


def test_database_failure_is_a_tool_error() -> None:
    error = SetupError("cannot connect to PostgreSQL at x", EXIT_CONNECTION_FAILED)
    result = call(FakeServices(open_error=error), {"query": "q"})
    assert result.is_error is True
    assert "cannot connect to PostgreSQL" in result.content[0].text


def test_unexpected_failure_does_not_leak_details() -> None:
    result = call(
        FakeServices(searcher=RaisingSearcher(RuntimeError("secret detail"))), {"query": "q"}
    )
    assert result.is_error is True
    assert "secret detail" not in result.content[0].text


@pytest.mark.db
def test_calls_are_logged_with_the_mcp_interface(migrated_db: str) -> None:
    services = FakeServices(searcher=FakeSearcher([make_chunk(1, score=0.25)]), db_url=migrated_db)
    call(services, {"query": "grade appeal", "k": 3, "mode": "vector"})
    failing = FakeServices(
        searcher=RaisingSearcher(EmbeddingError("embedding down")), db_url=migrated_db
    )
    call(failing, {"query": "second"})
    with psycopg.connect(migrated_db) as conn:
        rows = conn.execute(
            "SELECT interface, endpoint, question, mode, k, top_score, result_count, error "
            "FROM request_log ORDER BY id"
        ).fetchall()
    assert rows[0] == ("mcp", "search_policies", "grade appeal", "vector", 3, 0.25, 1, None)
    assert rows[1][:5] == ("mcp", "search_policies", "second", "hybrid", 5)
    assert rows[1][7] == "EmbeddingError: embedding down"


def test_ticket_tool_is_described_as_a_last_step_with_the_users_agreement() -> None:
    listed = run_client(FakeServices(), lambda client: client.list_tools())
    tool = listed.tools[1]
    assert "found nothing relevant" in tool.description
    assert "user has agreed" in tool.description
    assert "personal data" in tool.description
    assert set(tool.input_schema["required"]) == {"question", "summary", "details"}
    properties = tool.input_schema["properties"]
    assert properties["category"]["default"] == "policy-question"
    assert properties["category"]["enum"] == ["policy-question", "it-help", "other"]
    assert properties["summary"]["minLength"] == 5 and properties["summary"]["maxLength"] == 200
    assert properties["details"]["maxLength"] == 4000


TICKET = {
    "question": "  Is there a bus pass for graduate students?  ",
    "summary": "  No policy found on graduate bus passes  ",
    "details": "The user asked about bus passes and no policy matched.",
}


def test_draft_ticket_files_through_the_ticketing_client() -> None:
    ticketing = FakeTicketing()
    result = call(FakeServices(), TICKET, tool="draft_ticket", ticketing=ticketing)
    assert result.is_error is False
    assert result.structured_content == {
        "ticket_id": 1,
        "url": "http://127.0.0.1:8001/tickets/1",
        "status": "open",
        "category": "policy-question",
        "summary": "No policy found on graduate bus passes",
    }
    assert "http://127.0.0.1:8001/tickets/1" in result.content[0].text
    assert ticketing.filed == [
        {
            "category": "policy-question",
            "summary": "No policy found on graduate bus passes",
            "details": "The user asked about bus passes and no policy matched.",
            "source_question": "Is there a bus pass for graduate students?",
            "request_id": None,
        }
    ]


def test_draft_ticket_passes_the_category() -> None:
    ticketing = FakeTicketing()
    call(FakeServices(), TICKET | {"category": "it-help"}, tool="draft_ticket", ticketing=ticketing)
    assert ticketing.filed[0]["category"] == "it-help"


@pytest.mark.parametrize(
    "overrides",
    [
        {"question": ""},
        {"summary": "abcd"},
        {"summary": "s" * 201},
        {"details": ""},
        {"details": "d" * 4001},
        {"category": "billing"},
    ],
)
def test_draft_ticket_rejects_invalid_arguments(overrides: dict[str, Any]) -> None:
    ticketing = FakeTicketing()
    result = call(FakeServices(), TICKET | overrides, tool="draft_ticket", ticketing=ticketing)
    assert result.is_error is True
    assert ticketing.filed == []


def test_draft_ticket_reports_an_unreachable_ticketing_api() -> None:
    ticketing = FakeTicketing(TicketingError("cannot reach the ticketing API at x. Start it"))
    result = call(FakeServices(), TICKET, tool="draft_ticket", ticketing=ticketing)
    assert result.is_error is True
    assert "cannot reach the ticketing API" in result.content[0].text


def test_draft_ticket_does_not_leak_unexpected_errors() -> None:
    ticketing = FakeTicketing(RuntimeError("secret detail"))
    result = call(FakeServices(), TICKET, tool="draft_ticket", ticketing=ticketing)
    assert result.is_error is True
    assert "secret detail" not in result.content[0].text


@pytest.mark.db
def test_draft_ticket_calls_are_logged(migrated_db: str) -> None:
    services = FakeServices(db_url=migrated_db)
    call(services, TICKET, tool="draft_ticket")
    call(
        services,
        TICKET,
        tool="draft_ticket",
        ticketing=FakeTicketing(TicketingError("ticketing down")),
    )
    with psycopg.connect(migrated_db) as conn:
        rows = conn.execute(
            "SELECT interface, endpoint, question, error FROM request_log ORDER BY id"
        ).fetchall()
    question = "Is there a bus pass for graduate students?"
    assert rows == [
        ("mcp", "draft_ticket", question, None),
        ("mcp", "draft_ticket", question, "TicketingError: ticketing down"),
    ]


def test_draft_ticket_works_when_the_database_is_down() -> None:
    error = SetupError("cannot connect to PostgreSQL", EXIT_CONNECTION_FAILED)
    ticketing = FakeTicketing()
    result = call(FakeServices(open_error=error), TICKET, tool="draft_ticket", ticketing=ticketing)
    assert result.is_error is False
    assert len(ticketing.filed) == 1
