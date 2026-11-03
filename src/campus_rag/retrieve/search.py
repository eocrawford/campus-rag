"""`PolicySearcher`: keyword, vector, and hybrid search over the stored policy chunks."""

import psycopg

from campus_rag.index.embed import Embedder
from campus_rag.retrieve.fusion import reciprocal_rank_fusion
from campus_rag.retrieve.keyword import KEYWORD_RANKINGS, KeywordRanking, keyword_search
from campus_rag.retrieve.models import RetrievedChunk, SearchFilters, SearchMode
from campus_rag.retrieve.vector import vector_search

MIN_K = 1
MAX_K = 50
RRF_K = 60


class PolicySearcher:
    """Implements the `Searcher` protocol on a database connection and an `Embedder`.

    The connection must stay open for the searcher's lifetime. Hybrid search fuses the top
    `candidate_limit` results of each search (at least `k`) with reciprocal rank fusion (k = 60).
    """

    def __init__(
        self,
        conn: psycopg.Connection,
        embedder: Embedder,
        *,
        candidate_limit: int = 50,
        ef_search: int = 100,
        keyword_ranking: KeywordRanking = "bm25",
    ) -> None:
        if candidate_limit < 1:
            raise ValueError(f"candidate_limit must be >= 1, got {candidate_limit}")
        if keyword_ranking not in KEYWORD_RANKINGS:
            raise ValueError(f"unknown keyword ranking {keyword_ranking!r}")
        self._conn = conn
        self._embedder = embedder
        self._candidate_limit = candidate_limit
        self._ef_search = ef_search
        self._keyword_ranking = keyword_ranking

    def search(
        self,
        query: str,
        *,
        mode: SearchMode = "hybrid",
        k: int = 8,
        filters: SearchFilters | None = None,
    ) -> list[RetrievedChunk]:
        """Return up to `k` chunks for `query`, best first.

        Raises ValueError for an empty query, a `k` outside 1..50, or an unknown mode, and
        `EmbeddingError` when vector or hybrid search cannot reach the embedding server.
        """
        if not query.strip():
            raise ValueError("query must not be empty")
        if not MIN_K <= k <= MAX_K:
            raise ValueError(f"k must be between {MIN_K} and {MAX_K}, got {k}")

        if mode == "keyword":
            return self._keyword(query, k, filters)
        if mode == "vector":
            return self._vector(query, k, filters)
        if mode == "hybrid":
            candidates = max(self._candidate_limit, k)
            keyword = self._keyword(query, candidates, filters)
            vector = self._vector(query, candidates, filters)
            return reciprocal_rank_fusion(keyword=keyword, vector=vector, limit=k, k=RRF_K)
        raise ValueError(f"unknown search mode {mode!r}")

    def _keyword(
        self, query: str, limit: int, filters: SearchFilters | None
    ) -> list[RetrievedChunk]:
        return keyword_search(
            self._conn, query, limit=limit, filters=filters, ranking=self._keyword_ranking
        )

    def _vector(
        self, query: str, limit: int, filters: SearchFilters | None
    ) -> list[RetrievedChunk]:
        return vector_search(
            self._conn,
            self._embedder.embed_query(query),
            model=self._embedder.model,
            dimensions=self._embedder.dimensions,
            limit=limit,
            filters=filters,
            ef_search=self._ef_search,
        )
