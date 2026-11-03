"""Test doubles shared by the embedding and generation tests."""

import hashlib
import math
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from campus_rag.generate.chat import ChatResult
from campus_rag.index.embed import EmbeddingError
from campus_rag.retrieve.models import RetrievedChunk, SearchFilters, SearchMode


class FakeEmbedder:
    """Deterministic embedder: each text maps to a unit vector derived from its SHA-256 hash."""

    def __init__(self, model: str = "nomic-embed-text", dimensions: int = 768) -> None:
        self.model = model
        self.dimensions = dimensions
        self.document_batches: list[list[str]] = []
        self.queries: list[str] = []
        self.fail_after_batches: int | None = None

    @property
    def documents_embedded(self) -> int:
        return sum(len(batch) for batch in self.document_batches)

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        if self.fail_after_batches is not None and len(self.document_batches) >= (
            self.fail_after_batches
        ):
            raise EmbeddingError("fake embedding server went away")
        self.document_batches.append(list(texts))
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        self.queries.append(text)
        return self._vector(text)

    def _vector(self, text: str) -> list[float]:
        raw: list[float] = []
        counter = 0
        while len(raw) < self.dimensions:
            digest = hashlib.sha256(f"{counter}:{text}".encode()).digest()
            raw.extend((byte - 127.5) / 127.5 for byte in digest)
            counter += 1
        raw = raw[: self.dimensions]
        norm = math.sqrt(sum(x * x for x in raw)) or 1.0
        return [x / norm for x in raw]


class FakeChat:
    """Scripted chat client: returns queued replies in order and records every call.

    A queued `Exception` is raised instead of returned. With `respond`, replies are computed from
    the messages and model instead of taken from the queue.
    """

    def __init__(
        self,
        replies: Sequence[str | Exception] = (),
        *,
        respond: Callable[[Sequence[Mapping[str, str]], str], str] | None = None,
    ) -> None:
        self.replies: list[str | Exception] = list(replies)
        self.respond = respond
        self.calls: list[dict[str, Any]] = []

    def complete(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        model: str,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        response_format: Mapping[str, Any] | None = None,
    ) -> ChatResult:
        self.calls.append(
            {
                "messages": [dict(message) for message in messages],
                "model": model,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "response_format": response_format,
            }
        )
        if self.respond is not None:
            reply: str | Exception = self.respond(messages, model)
        else:
            reply = self.replies.pop(0) if self.replies else ""
        if isinstance(reply, Exception):
            raise reply
        return ChatResult(
            text=reply, model=model, prompt_tokens=None, completion_tokens=None, elapsed_ms=0.0
        )


def make_chunk(rank: int, *, content: str | None = None, **overrides: Any) -> RetrievedChunk:
    """A retrieved chunk with hand-written placeholder text, for generation tests."""
    heading = overrides.pop("heading_path", f"Sample Policy {rank} > Section {rank}")
    fields: dict[str, Any] = {
        "chunk_id": 100 + rank,
        "document_id": 10 + rank,
        "url": f"https://policy.example.edu/sample-{rank}/",
        "title": heading.split(" > ")[0],
        "heading_path": heading,
        "content": f"{heading}\n\n" + (content or f"Placeholder rule number {rank} applies."),
        "token_count": 20,
        "score": 1.0 / rank,
        "rank": rank,
    }
    fields.update(overrides)
    return RetrievedChunk(**fields)


class FakeSearcher:
    """`Searcher` that returns fixed chunks and records each query."""

    def __init__(self, chunks: Sequence[RetrievedChunk] = ()) -> None:
        self.chunks = list(chunks)
        self.calls: list[dict[str, Any]] = []

    def search(
        self,
        query: str,
        *,
        mode: SearchMode = "hybrid",
        k: int = 8,
        filters: SearchFilters | None = None,
    ) -> list[RetrievedChunk]:
        self.calls.append({"query": query, "mode": mode, "k": k, "filters": filters})
        return self.chunks[:k]
