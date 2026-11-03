"""Data types shared by answer generation and its consumers (evaluation, API, MCP)."""

from dataclasses import dataclass, field

from campus_rag.retrieve.models import RetrievedChunk

REFUSAL_TEXT = "I don't know based on the available policies."


@dataclass(frozen=True)
class ChatTurn:
    role: str  # "user" or "assistant"
    content: str


@dataclass(frozen=True)
class Citation:
    number: int  # passage number shown to the model, 1-based
    chunk_id: int
    url: str
    title: str
    heading_path: str


@dataclass(frozen=True)
class Answer:
    question: str
    search_query: str  # standalone query used for retrieval (rewritten for follow-ups)
    text: str  # final answer text with [n] markers, or REFUSAL_TEXT
    refused: bool
    citations: tuple[Citation, ...]  # valid citations, in order of first use
    passages: tuple[RetrievedChunk, ...]  # passages given to the model, numbered from 1
    model: str
    invalid_citations: tuple[int, ...] = ()  # cited numbers that were not supplied; removed
    timings_ms: dict[str, float] = field(default_factory=dict)  # retrieve, rewrite, generate
