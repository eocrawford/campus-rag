"""SQL pieces shared by keyword and vector search.

Both searches select from `chunks c JOIN documents d` and map rows with `chunk_from_row`.
"""

from collections.abc import Sequence
from typing import Any

from campus_rag.retrieve.models import RetrievedChunk, SearchFilters

# Column order that `chunk_from_row` expects; the score expression is appended by each search.
CHUNK_COLUMNS = "c.id, d.id, d.url, d.title, c.heading_path, c.content, c.token_count"


def filter_conditions(filters: SearchFilters | None) -> tuple[list[str], list[object]]:
    """Return SQL conditions (to be ANDed) and their parameters for `filters`.

    URL prefixes match literally: `starts_with` needs no LIKE escaping for `%`, `_`, or `\\`.
    """
    conditions: list[str] = []
    params: list[object] = []
    if filters is None:
        return conditions, params
    if filters.sources:
        conditions.append("d.source = ANY(%s::text[])")
        params.append(list(filters.sources))
    if filters.url_prefixes:
        conditions.append(
            "EXISTS (SELECT 1 FROM unnest(%s::text[]) AS prefix WHERE starts_with(d.url, prefix))"
        )
        params.append(list(filters.url_prefixes))
    return conditions, params


def chunk_from_row(row: Sequence[Any], rank: int, *, keyword: bool, vector: bool) -> RetrievedChunk:
    """Build a chunk from `CHUNK_COLUMNS` plus a trailing score; set the rank in its list."""
    return RetrievedChunk(
        chunk_id=row[0],
        document_id=row[1],
        url=row[2],
        title=row[3],
        heading_path=row[4],
        content=row[5],
        token_count=row[6],
        score=float(row[7]),
        rank=rank,
        keyword_rank=rank if keyword else None,
        vector_rank=rank if vector else None,
    )
