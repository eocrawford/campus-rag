"""Test doubles shared by the embedding tests."""

import hashlib
import math
from collections.abc import Sequence

from campus_rag.index.embed import EmbeddingError


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
