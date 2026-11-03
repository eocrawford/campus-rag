"""Reciprocal rank fusion of the keyword and vector result lists."""

from collections.abc import Sequence
from dataclasses import replace

from campus_rag.retrieve.models import RetrievedChunk


def reciprocal_rank_fusion(
    *,
    keyword: Sequence[RetrievedChunk],
    vector: Sequence[RetrievedChunk],
    limit: int,
    k: int = 60,
) -> list[RetrievedChunk]:
    """Merge two ranked lists into one, scoring each chunk as the sum of 1 / (k + rank).

    Ranks are the 1-based positions in `keyword` and `vector`; the chunks' own score and rank
    fields are ignored. Raw scores are never compared, so the lists need no common scale. A chunk
    found by both searches outranks one found by a single search at similar ranks. Equal scores
    are ordered by the chunk's better rank in either list, then by chunk id. Each returned chunk
    carries its `keyword_rank` and `vector_rank` (None when absent) and a renumbered `rank`.
    """
    if k < 0:
        raise ValueError(f"k must be >= 0, got {k}")
    if limit < 1:
        raise ValueError(f"limit must be >= 1, got {limit}")

    ranks: dict[int, list[int | None]] = {}
    chunks: dict[int, RetrievedChunk] = {}
    for position, results in enumerate((keyword, vector)):
        for rank, chunk in enumerate(results, start=1):
            entry = ranks.setdefault(chunk.chunk_id, [None, None])
            if entry[position] is None:  # a repeated chunk keeps its first (best) rank
                entry[position] = rank
            chunks.setdefault(chunk.chunk_id, chunk)

    def score(entry: list[int | None]) -> float:
        return sum(1.0 / (k + rank) for rank in entry if rank is not None)

    def sort_key(chunk_id: int) -> tuple[float, int, int]:
        entry = ranks[chunk_id]
        return (-score(entry), min(rank for rank in entry if rank is not None), chunk_id)

    ordered = sorted(ranks, key=sort_key)[:limit]
    return [
        replace(
            chunks[chunk_id],
            score=score(ranks[chunk_id]),
            rank=rank,
            keyword_rank=ranks[chunk_id][0],
            vector_rank=ranks[chunk_id][1],
        )
        for rank, chunk_id in enumerate(ordered, start=1)
    ]
