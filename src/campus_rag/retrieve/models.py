"""Data types shared by retrieval and its consumers (generation, evaluation, API, MCP)."""

from dataclasses import dataclass
from typing import Literal, Protocol

SearchMode = Literal["keyword", "vector", "hybrid"]
SEARCH_MODES: tuple[SearchMode, ...] = ("keyword", "vector", "hybrid")


@dataclass(frozen=True)
class SearchFilters:
    """Optional restrictions applied before ranking. Empty tuples mean no restriction."""

    sources: tuple[str, ...] = ()  # documents.source values, e.g. "uw-policy"
    url_prefixes: tuple[str, ...] = ()  # documents.url must start with one of these


@dataclass(frozen=True)
class RetrievedChunk:
    chunk_id: int
    document_id: int
    url: str
    title: str  # documents.title
    heading_path: str
    content: str  # starts with the heading path line
    token_count: int
    score: float  # higher is better: RRF score, ts_rank_cd, or 1 - cosine distance
    rank: int  # 1-based position in the returned list
    keyword_rank: int | None = None  # 1-based rank in the keyword list, if it appeared there
    vector_rank: int | None = None  # 1-based rank in the vector list, if it appeared there


class Searcher(Protocol):
    def search(
        self,
        query: str,
        *,
        mode: SearchMode = "hybrid",
        k: int = 8,
        filters: SearchFilters | None = None,
    ) -> list[RetrievedChunk]: ...
