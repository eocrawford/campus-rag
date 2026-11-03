"""Embed text through an OpenAI-compatible `/embeddings` API (Ollama by default).

The client sends task prefixes (for example `search_document: ` for chunks and `search_query: `
for questions) because models such as `nomic-embed-text` expect them. It never logs or prints
the API key.
"""

import time
from collections.abc import Callable, Sequence
from types import TracebackType
from typing import Any, Protocol, Self

import httpx

from campus_rag.config import EmbeddingSettings

MAX_BATCH_SIZE = 32
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
_CHECK_TEXT = "connectivity check"


class EmbeddingError(Exception):
    """The embedding server is unreachable, rejected a request, or returned unusable vectors."""


class Embedder(Protocol):
    model: str
    dimensions: int

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class OpenAICompatibleEmbedder:
    """`Embedder` backed by `POST {base_url}/embeddings`. Use as a context manager."""

    def __init__(
        self,
        settings: EmbeddingSettings,
        *,
        client: httpx.Client | None = None,
        batch_size: int = MAX_BATCH_SIZE,
        max_attempts: int = 4,
        backoff_seconds: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not 1 <= batch_size <= MAX_BATCH_SIZE:
            raise ValueError(f"batch_size must be between 1 and {MAX_BATCH_SIZE}")
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self.model = settings.model
        self.dimensions = settings.dimensions
        self._url = f"{settings.base_url.rstrip('/')}/embeddings"
        self._document_prefix = settings.document_prefix
        self._query_prefix = settings.query_prefix
        self._headers = {"Authorization": f"Bearer {settings.api_key}"} if settings.api_key else {}
        self._batch_size = batch_size
        self._max_attempts = max_attempts
        self._backoff_seconds = backoff_seconds
        self._sleep = sleep
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=httpx.Timeout(120.0, connect=5.0))

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed document texts, in input order, in requests of up to `batch_size` texts."""
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            batch = texts[start : start + self._batch_size]
            vectors.extend(self._embed([self._document_prefix + text for text in batch]))
        return vectors

    def embed_query(self, text: str) -> list[float]:
        return self._embed([self._query_prefix + text])[0]

    def check(self) -> None:
        """Send one tiny request to prove the server is reachable and the model answers.

        Raises `EmbeddingError` with a fix for the common failures. Does not retry.
        """
        self._embed([self._document_prefix + _CHECK_TEXT], max_attempts=1)

    def _embed(self, inputs: list[str], *, max_attempts: int | None = None) -> list[list[float]]:
        try:
            return self._request(inputs, max_attempts or self._max_attempts)
        except _Unreachable as exc:
            raise EmbeddingError(
                f"cannot reach the embedding server at {self._url} ({exc}). "
                "Start it with: docker compose up -d --wait ollama"
            ) from exc.__cause__
        except _ModelNotFound as exc:
            raise EmbeddingError(
                f"the embedding server at {self._url} does not have model {self.model!r} ({exc}). "
                f"Pull it with: docker compose exec ollama ollama pull {self.model}"
            ) from None

    def _request(self, inputs: list[str], max_attempts: int) -> list[list[float]]:
        payload = {"model": self.model, "input": inputs}
        for attempt in range(1, max_attempts + 1):
            try:
                response = self._client.post(self._url, json=payload, headers=self._headers)
            except httpx.TransportError as exc:
                reason = f"{type(exc).__name__}: {exc}".rstrip(": ")
                if attempt == max_attempts:
                    error = _Unreachable(f"{reason}, after {attempt} attempt(s)")
                    raise error from exc
            else:
                if response.status_code == 404:
                    raise _ModelNotFound(f"HTTP 404: {_error_text(response)}")
                if response.status_code not in _RETRYABLE_STATUS:
                    if response.status_code >= 400:
                        raise EmbeddingError(
                            f"embedding request to {self._url} failed: "
                            f"HTTP {response.status_code}: {_error_text(response)}"
                        )
                    return self._parse(response, len(inputs))
                if attempt == max_attempts:
                    raise EmbeddingError(
                        f"embedding request to {self._url} failed: "
                        f"HTTP {response.status_code} after {attempt} attempt(s): "
                        f"{_error_text(response)}"
                    )
            self._sleep(self._backoff_seconds * 2 ** (attempt - 1))
        raise AssertionError("unreachable")  # pragma: no cover

    def _parse(self, response: httpx.Response, expected_count: int) -> list[list[float]]:
        try:
            data = response.json()["data"]
            ordered = sorted(
                (
                    (item.get("index", position), item["embedding"])
                    for position, item in enumerate(data)
                ),
                key=lambda pair: pair[0],
            )
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise EmbeddingError(
                f"embedding response from {self._url} is not an OpenAI-style embeddings "
                f"payload ({type(exc).__name__}: {exc})"
            ) from None
        if len(ordered) != expected_count or [i for i, _ in ordered] != list(range(expected_count)):
            raise EmbeddingError(
                f"embedding response has {len(ordered)} vector(s) for {expected_count} input(s)"
            )
        vectors: list[list[float]] = []
        for _, embedding in ordered:
            if not isinstance(embedding, list) or len(embedding) != self.dimensions:
                actual = len(embedding) if isinstance(embedding, list) else type(embedding).__name__
                raise EmbeddingError(
                    f"model {self.model!r} returned vectors of {actual} dimensions; expected "
                    f"{self.dimensions}. Set CAMPUS_RAG_EMBEDDING_DIMENSIONS to match the model."
                )
            vectors.append([float(x) for x in embedding])
        return vectors


class _Unreachable(Exception):
    pass


class _ModelNotFound(Exception):
    pass


def _error_text(response: httpx.Response) -> str:
    """A short description of an error response; never includes request headers."""
    try:
        body: Any = response.json()
    except ValueError:
        return response.text[:200].strip() or response.reason_phrase
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        error = error.get("message")
    return str(error or body)[:200]
