"""Request and response models for the HTTP API. They define the OpenAPI schema."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from campus_rag.retrieve.models import SearchMode
from campus_rag.retrieve.search import MAX_K, MIN_K
from campus_rag.ticketing.schemas import TicketCategory

MAX_TEXT_CHARS = 2000  # longest question or search query
MAX_HISTORY_TURNS = 20

Text = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_TEXT_CHARS)
]


class SearchRequest(BaseModel):
    query: Text = Field(description="Question or search terms.")
    mode: SearchMode = Field(
        "hybrid", description="`keyword` (BM25), `vector` (cosine), or `hybrid` (both, fused)."
    )
    k: int = Field(8, ge=MIN_K, le=MAX_K, description="Number of passages to return.")
    sources: list[str] | None = Field(
        None, description="Only documents from these sources, such as `uw-policy`."
    )
    url_prefixes: list[str] | None = Field(
        None, description="Only documents whose URL starts with one of these prefixes."
    )

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "query": "When can a student appeal a grade?",
                    "mode": "hybrid",
                    "k": 5,
                    "url_prefixes": ["https://policy.uw.edu/directory/"],
                }
            ]
        }
    )


class SearchResult(BaseModel):
    rank: int = Field(description="1-based position in the results.")
    score: float = Field(description="Higher is better; the scale depends on the mode.")
    keyword_rank: int | None = Field(None, description="Rank in the keyword list, if present.")
    vector_rank: int | None = Field(None, description="Rank in the vector list, if present.")
    title: str
    heading_path: str
    url: str
    content_preview: str = Field(description="The start of the passage, about 300 characters.")


class SearchResponse(BaseModel):
    request_id: int | None = Field(
        None, description="Id in the request log; null if logging failed. Used by `POST /feedback`."
    )
    query: str
    mode: SearchMode
    k: int
    results: list[SearchResult]
    latency_ms: int


class HistoryTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: Annotated[str, StringConstraints(max_length=4 * MAX_TEXT_CHARS)]


class AnswerRequest(BaseModel):
    question: Text = Field(description="The question to answer from the indexed policies.")
    history: list[HistoryTurn] = Field(
        default_factory=list,
        max_length=MAX_HISTORY_TURNS,
        description="Earlier turns of the conversation; a non-empty history makes the question "
        "a follow-up that is rewritten into a standalone search query.",
    )
    mode: SearchMode = Field("hybrid", description="Retrieval mode.")
    k: int = Field(
        8, ge=MIN_K, le=MAX_K, description="Passages to retrieve (the prompt uses up to 6)."
    )

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"question": "How long may a student take a leave of absence?", "k": 8},
                {
                    "question": "Does that apply to graduate students?",
                    "history": [
                        {"role": "user", "content": "How long may a student take a leave?"},
                        {"role": "assistant", "content": "Up to one year [1]."},
                    ],
                },
            ]
        }
    )


class CitationOut(BaseModel):
    number: int = Field(description="The `[n]` marker used in the answer text.")
    title: str
    heading_path: str
    url: str


class TicketDraft(BaseModel):
    """A ticket body for `POST /tickets` on the ticketing API."""

    category: TicketCategory
    summary: str
    details: str
    source_question: str
    request_id: int | None = Field(None, description="The request that prompted the draft.")


class AnswerResponse(BaseModel):
    request_id: int | None = Field(
        None, description="Id in the request log; null if logging failed. Used by `POST /feedback`."
    )
    answer: str = Field(description="Answer text with `[n]` citation markers, or the refusal.")
    refused: bool = Field(description="True when the passages do not answer the question.")
    citations: list[CitationOut]
    search_query: str = Field(description="The standalone query used for retrieval.")
    model: str
    timings_ms: dict[str, float] = Field(description="Per stage, plus `total`.")
    suggest_ticket: bool = Field(
        description="True when the answer was refused: the caller may offer to draft a "
        "support ticket."
    )
    ticket_draft: TicketDraft | None = Field(
        None,
        description="Set when the answer was refused: a ticket prefilled from the question. "
        "Nothing is filed; show it to the user and, if they agree, POST it to the ticketing API.",
    )


class FeedbackRequest(BaseModel):
    request_id: int = Field(ge=1, description="The `request_id` from a search or answer response.")
    rating: Literal[-1, 1] = Field(description="1 for helpful, -1 for not helpful.")
    comment: str | None = Field(None, max_length=1000, description="Optional free text.")

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [{"request_id": 12, "rating": -1, "comment": "Wrong policy."}]
        }
    )


class FeedbackResponse(BaseModel):
    feedback_id: int
    request_id: int


class ComponentHealth(BaseModel):
    ok: bool
    detail: str


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    schema_version: int | None
    database: ComponentHealth
    embedding: ComponentHealth
    chat: ComponentHealth


class ErrorResponse(BaseModel):
    detail: str
