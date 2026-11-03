"""The HTTP API: `GET /health`, `POST /search`, `POST /answer`, and `POST /feedback`.

Every handler is synchronous, so FastAPI runs it in a worker thread, and each request opens its
own database connection (see `campus_rag.services`). Model-server and database failures return
503 with a message that names the fix; no stack trace reaches the client.
"""

import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import psycopg
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from campus_rag.analytics.log import describe_error, log_request, record_feedback
from campus_rag.api.schemas import (
    AnswerRequest,
    AnswerResponse,
    CitationOut,
    ComponentHealth,
    ErrorResponse,
    FeedbackRequest,
    FeedbackResponse,
    HealthResponse,
    SearchRequest,
    SearchResponse,
    SearchResult,
    TicketDraft,
)
from campus_rag.config import load_settings
from campus_rag.generate.answer import AnswerService
from campus_rag.generate.chat import ChatError
from campus_rag.generate.models import ChatTurn
from campus_rag.index.embed import EmbeddingError
from campus_rag.index.migrate import _first_line
from campus_rag.retrieve.connect import SetupError
from campus_rag.retrieve.models import RetrievedChunk, SearchFilters
from campus_rag.services import LiveServices, Services, elapsed_ms, passage_body
from campus_rag.ticketing.schemas import DETAILS_MAX_CHARS, SUMMARY_MAX_CHARS

logger = logging.getLogger(__name__)

PREVIEW_CHARS = 300

_UNAVAILABLE = {
    "model": ErrorResponse,
    "description": "The database or model server is unavailable.",
}

_DESCRIPTION = """\
Search and answer questions about indexed public university policies.

The service runs on this machine and binds to the loopback address. Answers cite numbered
passages; open the cited URL to read the policy itself.
"""


def create_app(services_factory: Callable[[], Services] | None = None) -> FastAPI:
    """Build the app. `services_factory` supplies the database and models; tests pass fakes."""
    services = (services_factory or _live_services)()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        services.close()

    app = FastAPI(
        title="campus-rag",
        version="0.1.0",
        description=_DESCRIPTION,
        lifespan=lifespan,
    )
    app.state.services = services
    _register_error_handlers(app)

    @app.get(
        "/health",
        response_model=HealthResponse,
        summary="Check the database and model servers",
        responses={503: {"model": HealthResponse, "description": "A component is unavailable."}},
        tags=["service"],
    )
    def health(response: Response) -> HealthResponse:
        """Report whether the database and the embedding and chat models are reachable.

        Returns 200 when everything works and 503, with the same body, otherwise. Model checks
        are cached for 30 seconds.
        """
        report = services.health()
        if not report.ok:
            response.status_code = 503
        return HealthResponse(
            status="ok" if report.ok else "degraded",
            schema_version=report.schema_version,
            database=ComponentHealth(ok=report.database.ok, detail=report.database.detail),
            embedding=ComponentHealth(ok=report.embedding.ok, detail=report.embedding.detail),
            chat=ComponentHealth(ok=report.chat.ok, detail=report.chat.detail),
        )

    @app.post(
        "/search",
        response_model=SearchResponse,
        summary="Search the policy passages",
        responses={503: _UNAVAILABLE},
        tags=["search"],
    )
    def search(body: SearchRequest) -> SearchResponse:
        """Return the best passages for a query, with ranks, scores, and source URLs.

        `keyword` mode needs only the database; `vector` and `hybrid` also call the embedding
        model.
        """
        started = time.perf_counter()
        filters = SearchFilters(
            sources=tuple(body.sources or ()), url_prefixes=tuple(body.url_prefixes or ())
        )
        embedding_model = services.settings.embedding.model
        with services.open_scope() as scope:
            try:
                chunks = scope.searcher.search(
                    body.query, mode=body.mode, k=body.k, filters=filters
                )
            except (EmbeddingError, psycopg.Error) as exc:
                log_request(
                    scope.conn,
                    interface="api",
                    endpoint="search",
                    question=body.query,
                    mode=body.mode,
                    k=body.k,
                    latency_ms=elapsed_ms(started),
                    embedding_model=embedding_model,
                    error=describe_error(exc),
                )
                raise
            latency_ms = elapsed_ms(started)
            request_id = log_request(
                scope.conn,
                interface="api",
                endpoint="search",
                question=body.query,
                mode=body.mode,
                k=body.k,
                latency_ms=latency_ms,
                retrieve_ms=latency_ms,
                top_score=chunks[0].score if chunks else None,
                result_count=len(chunks),
                embedding_model=embedding_model,
            )
        return SearchResponse(
            request_id=request_id,
            query=body.query,
            mode=body.mode,
            k=body.k,
            results=[_search_result(chunk) for chunk in chunks],
            latency_ms=round(latency_ms),
        )

    @app.post(
        "/answer",
        response_model=AnswerResponse,
        summary="Answer a question with citations",
        responses={503: _UNAVAILABLE},
        tags=["answer"],
    )
    def answer(body: AnswerRequest) -> AnswerResponse:
        """Retrieve passages and compose a short answer that cites them.

        When the passages do not support an answer, `refused` is true, `answer` is the fixed
        refusal sentence, `suggest_ticket` is true, and `ticket_draft` holds a ticket prefilled
        from the question; nothing is filed until the caller posts it. A local 7B model on a CPU
        takes about one minute to answer.
        """
        started = time.perf_counter()
        settings = services.settings
        history = tuple(ChatTurn(turn.role, turn.content) for turn in body.history)
        with services.open_scope() as scope:
            service = AnswerService(
                scope.searcher, scope.chat, settings.chat, k=body.k, search_mode=body.mode
            )
            try:
                result = service.answer(body.question, history)
            except (EmbeddingError, ChatError, psycopg.Error) as exc:
                log_request(
                    scope.conn,
                    interface="api",
                    endpoint="answer",
                    question=body.question,
                    mode=body.mode,
                    k=body.k,
                    latency_ms=elapsed_ms(started),
                    answer_model=settings.chat.answer_model,
                    embedding_model=settings.embedding.model,
                    error=describe_error(exc),
                )
                raise
            total_ms = elapsed_ms(started)
            request_id = log_request(
                scope.conn,
                interface="api",
                endpoint="answer",
                question=body.question,
                search_query=result.search_query,
                mode=body.mode,
                k=body.k,
                latency_ms=total_ms,
                retrieve_ms=result.timings_ms.get("retrieve"),
                generate_ms=result.timings_ms.get("generate"),
                top_score=result.passages[0].score if result.passages else None,
                result_count=len(result.passages),
                refused=result.refused,
                citation_count=len(result.citations),
                answer_model=result.model,
                embedding_model=settings.embedding.model,
            )
        return AnswerResponse(
            request_id=request_id,
            answer=result.text,
            refused=result.refused,
            citations=[
                CitationOut(
                    number=citation.number,
                    title=citation.title,
                    heading_path=citation.heading_path,
                    url=citation.url,
                )
                for citation in result.citations
            ],
            search_query=result.search_query,
            model=result.model,
            timings_ms={**result.timings_ms, "total": total_ms},
            suggest_ticket=result.refused,
            ticket_draft=(
                _ticket_draft(body.question, result.search_query, request_id)
                if result.refused
                else None
            ),
        )

    @app.post(
        "/feedback",
        status_code=201,
        response_model=FeedbackResponse,
        summary="Rate a search or an answer",
        responses={
            404: {"model": ErrorResponse, "description": "Unknown request_id."},
            503: _UNAVAILABLE,
        },
        tags=["answer"],
    )
    def feedback(body: FeedbackRequest) -> FeedbackResponse:
        """Record a helpful (1) or not helpful (-1) rating for a logged request.

        The rating is stored with the request id and the optional comment, and nothing that
        identifies the caller.
        """
        comment = (body.comment or "").strip() or None
        with services.open_scope() as scope:
            feedback_id = record_feedback(scope.conn, body.request_id, body.rating, comment)
        if feedback_id is None:
            raise HTTPException(status_code=404, detail=f"unknown request_id {body.request_id}")
        return FeedbackResponse(feedback_id=feedback_id, request_id=body.request_id)

    return app


def _live_services() -> Services:
    return LiveServices(load_settings())


def _ticket_draft(question: str, search_query: str, request_id: int | None) -> TicketDraft:
    """A ticket for an unanswered question, built from the question alone."""
    flat = " ".join(question.split())
    summary = f"Policy question not answered: {flat}"
    if len(summary) > SUMMARY_MAX_CHARS:
        summary = summary[: SUMMARY_MAX_CHARS - 1].rstrip() + "\u2026"
    details = (
        "The policy assistant could not answer this question from the indexed policies."
        f"\n\nQuestion: {question}"
    )
    if search_query != question:
        details += f"\nSearch query used: {search_query}"
    return TicketDraft(
        category="policy-question",
        summary=summary,
        details=details[:DETAILS_MAX_CHARS],
        source_question=question,
        request_id=request_id,
    )


def _search_result(chunk: RetrievedChunk) -> SearchResult:
    body = " ".join(passage_body(chunk).split())
    if len(body) > PREVIEW_CHARS:
        body = body[: PREVIEW_CHARS - 1].rstrip() + "\u2026"
    return SearchResult(
        rank=chunk.rank,
        score=chunk.score,
        keyword_rank=chunk.keyword_rank,
        vector_rank=chunk.vector_rank,
        title=chunk.title,
        heading_path=chunk.heading_path,
        url=chunk.url,
        content_preview=body,
    )


def _register_error_handlers(app: FastAPI) -> None:
    def unavailable(_: Request, exc: Exception) -> JSONResponse:
        if isinstance(exc, psycopg.errors.UndefinedTable):
            detail = "database tables are missing. Run: uv run python -m campus_rag.index.migrate"
        elif isinstance(exc, psycopg.Error):
            detail = f"database error: {_first_line(exc)}"
        else:
            detail = str(exc)
        return JSONResponse(status_code=503, content={"detail": detail})

    def internal(_: Request, exc: Exception) -> JSONResponse:
        logger.error("unhandled error in request", exc_info=exc)
        return JSONResponse(status_code=500, content={"detail": "internal server error"})

    for error_type in (SetupError, EmbeddingError, ChatError, psycopg.Error):
        app.add_exception_handler(error_type, unavailable)
    app.add_exception_handler(Exception, internal)
