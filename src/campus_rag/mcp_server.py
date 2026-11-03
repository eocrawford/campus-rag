"""MCP server (stdio): `search_policies` retrieves passages and `draft_ticket` files a help ticket.

Run with: python -m campus_rag.mcp_server. See runbooks/run-api-and-mcp.md.
The server writes only MCP messages to stdout; log lines go to stderr.
"""

import logging
import sys
import time
from typing import Annotated

import psycopg
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field

from campus_rag.analytics.log import describe_error, log_request
from campus_rag.config import load_settings
from campus_rag.index.embed import EmbeddingError
from campus_rag.index.migrate import _first_line
from campus_rag.retrieve.connect import SetupError
from campus_rag.retrieve.models import SearchMode
from campus_rag.retrieve.search import MAX_K, MIN_K
from campus_rag.services import LiveServices, Services, elapsed_ms, passage_body
from campus_rag.ticketing.client import Ticketing, TicketingClient, TicketingError
from campus_rag.ticketing.schemas import (
    DETAILS_MAX_CHARS,
    QUESTION_MAX_CHARS,
    SUMMARY_MAX_CHARS,
    SUMMARY_MIN_CHARS,
    TicketCategory,
)

logger = logging.getLogger(__name__)

EXIT_CONFIG_ERROR = 5

INSTRUCTIONS = (
    "Searches indexed public university policies (University of Washington Policy Directory). "
    "Call search_policies when a question depends on a university policy, then answer only from "
    "the passages it returns and cite each source by its URL. If nothing relevant is found and "
    "the user agrees, draft_ticket files a help-desk ticket."
)

TOOL_DESCRIPTION = (
    "Search university policy documents and return the best-matching passages, ranked, each with "
    "its policy title, section heading path, source URL, score, and passage text. Answer only "
    "from the returned passages and cite the URL of every passage you use. If no passage "
    "answers the question, say so instead of guessing. Modes: 'hybrid' (default) combines keyword "
    "and vector search; 'keyword' matches exact terms, names, and codes; 'vector' matches "
    "paraphrases."
)

TICKET_TOOL_DESCRIPTION = (
    "File a help-desk ticket in the ticketing system. Use it only when search_policies found "
    "nothing relevant AND the user has agreed to open a ticket; never file one on your own. "
    "Write the summary and details from what the user typed, and include no personal data "
    "beyond that. Returns the ticket id and URL; tell the user both."
)


class PolicyPassage(BaseModel):
    rank: int = Field(description="1-based position; 1 is the best match.")
    score: float = Field(description="Higher is better; the scale depends on the mode.")
    title: str = Field(description="Policy title.")
    heading_path: str = Field(description="Section heading path inside the policy.")
    url: str = Field(description="Source page; cite it.")
    text: str = Field(description="The passage text.")


class PolicySearchResults(BaseModel):
    query: str
    mode: SearchMode
    results: list[PolicyPassage]


class DraftedTicket(BaseModel):
    ticket_id: int = Field(description="Id of the filed ticket; tell the user.")
    url: str = Field(description="Where to read the ticket; tell the user.")
    status: str
    category: str
    summary: str


def build_server(services: Services, ticketing: Ticketing | None = None) -> MCPServer:
    """Create the MCP server on `services`; tests pass fakes.

    `ticketing` files tickets; by default it is a client for the configured ticketing API.
    """
    ticketing = ticketing or TicketingClient(services.settings.ticketing.base_url)
    server = MCPServer("campus-rag", instructions=INSTRUCTIONS)

    @server.tool(name="search_policies", description=TOOL_DESCRIPTION)
    def search_policies(
        query: Annotated[
            str, Field(min_length=1, max_length=2000, description="Question or search terms.")
        ],
        k: Annotated[
            int, Field(ge=MIN_K, le=MAX_K, description="Number of passages to return.")
        ] = 5,
        mode: Annotated[SearchMode, Field(description="Search mode.")] = "hybrid",
    ) -> PolicySearchResults:
        query = query.strip()
        if not query:
            raise ToolError("query must not be empty")
        started = time.perf_counter()
        embedding_model = services.settings.embedding.model
        try:
            with services.open_scope() as scope:
                try:
                    chunks = scope.searcher.search(query, mode=mode, k=k)
                except (EmbeddingError, psycopg.Error) as exc:
                    log_request(
                        scope.conn,
                        interface="mcp",
                        endpoint="search_policies",
                        question=query,
                        mode=mode,
                        k=k,
                        latency_ms=elapsed_ms(started),
                        embedding_model=embedding_model,
                        error=describe_error(exc),
                    )
                    raise
                latency_ms = elapsed_ms(started)
                log_request(
                    scope.conn,
                    interface="mcp",
                    endpoint="search_policies",
                    question=query,
                    mode=mode,
                    k=k,
                    latency_ms=latency_ms,
                    retrieve_ms=latency_ms,
                    top_score=chunks[0].score if chunks else None,
                    result_count=len(chunks),
                    embedding_model=embedding_model,
                )
        except (SetupError, EmbeddingError) as exc:
            raise ToolError(str(exc)) from exc
        except psycopg.Error as exc:
            raise ToolError(f"database error: {_first_line(exc)}") from exc
        return PolicySearchResults(
            query=query,
            mode=mode,
            results=[
                PolicyPassage(
                    rank=chunk.rank,
                    score=chunk.score,
                    title=chunk.title,
                    heading_path=chunk.heading_path,
                    url=chunk.url,
                    text=passage_body(chunk),
                )
                for chunk in chunks
            ],
        )

    @server.tool(name="draft_ticket", description=TICKET_TOOL_DESCRIPTION)
    def draft_ticket(
        question: Annotated[
            str,
            Field(
                min_length=1,
                max_length=QUESTION_MAX_CHARS,
                description="The user's question that the policies could not answer.",
            ),
        ],
        summary: Annotated[
            str,
            Field(
                min_length=SUMMARY_MIN_CHARS,
                max_length=SUMMARY_MAX_CHARS,
                description="One-line ticket summary.",
            ),
        ],
        details: Annotated[
            str,
            Field(
                min_length=1,
                max_length=DETAILS_MAX_CHARS,
                description="What the user asked and what they need, in their own words.",
            ),
        ],
        category: Annotated[
            TicketCategory, Field(description="Which queue the ticket belongs to.")
        ] = "policy-question",
    ) -> DraftedTicket:
        question = question.strip()
        started = time.perf_counter()
        try:
            filed = ticketing.create_ticket(
                category=category,
                summary=summary.strip(),
                details=details.strip(),
                source_question=question,
            )
        except TicketingError as exc:
            _log_ticket_call(services, question, started, error=describe_error(exc))
            raise ToolError(str(exc)) from exc
        _log_ticket_call(services, question, started)
        return DraftedTicket(
            ticket_id=filed.id,
            url=filed.url,
            status=filed.status,
            category=filed.category,
            summary=filed.summary,
        )

    return server


def _log_ticket_call(
    services: Services, question: str, started: float, error: str | None = None
) -> None:
    """Record a `draft_ticket` call; a database that is down only skips the log row."""
    try:
        with services.open_scope() as scope:
            log_request(
                scope.conn,
                interface="mcp",
                endpoint="draft_ticket",
                question=question,
                latency_ms=elapsed_ms(started),
                error=error,
            )
    except (SetupError, psycopg.Error) as exc:
        logger.warning("could not log draft_ticket call: %s", describe_error(exc))


def main() -> int:
    try:
        settings = load_settings()
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    services = LiveServices(settings)
    ticketing = TicketingClient(settings.ticketing.base_url)
    try:
        build_server(services, ticketing).run("stdio")
    finally:
        ticketing.close()
        services.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
