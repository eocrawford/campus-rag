"""Vector search: cosine distance through the partial HNSW index, plus an exact-scan baseline.

Migration 003 builds one partial HNSW index per embedding model over `embedding::vector(N)`.
A query uses it only when it filters `model = '<name>'` and orders by the same cast expression.
Distances are cosine distances; the returned score is `1 - distance`, so higher is better.
"""

from collections.abc import Sequence

import psycopg

from campus_rag.index.store import vector_literal
from campus_rag.retrieve.models import RetrievedChunk, SearchFilters
from campus_rag.retrieve.sql import chunk_from_row, filter_conditions

MAX_DIMENSIONS = 16_000  # pgvector's limit for the vector type
MAX_EF_SEARCH = 1_000  # pgvector's limit for hnsw.ef_search

# `nearest` is materialized so the relaxed-order index scan is re-sorted by exact distance
# afterward, as the pgvector documentation recommends. The cast dimension is an integer that
# `_validated_dimensions` checked, so formatting it into the text is safe.
_SQL = """
WITH nearest AS MATERIALIZED (
    SELECT c.id, d.id AS document_id, d.url, d.title, c.heading_path, c.content, c.token_count,
           {distance_expression} AS distance
    FROM chunk_embeddings ce
    JOIN chunks c ON c.id = ce.chunk_id
    JOIN documents d ON d.id = c.document_id
    WHERE ce.model = %s {filters}
    ORDER BY distance
    LIMIT %s
)
SELECT id, document_id, url, title, heading_path, content, token_count, 1 - distance
FROM nearest
ORDER BY distance, id
"""


def vector_search(
    conn: psycopg.Connection,
    query_vector: Sequence[float],
    *,
    model: str,
    dimensions: int,
    limit: int,
    filters: SearchFilters | None = None,
    ef_search: int = 100,
) -> list[RetrievedChunk]:
    """Return the `limit` chunks nearest to `query_vector` using the HNSW index.

    `hnsw.iterative_scan = relaxed_order` keeps scanning the index until `limit` rows pass the
    filters (pgvector 0.8 or later), so filters do not shrink the result. Sequential scans are
    disabled for the statement: on a corpus this small the planner prefers an exact scan, which
    would make the index's recall and latency unmeasurable. Without an index for `model`, the
    statement still falls back to an exact scan.
    """
    if not 1 <= ef_search <= MAX_EF_SEARCH:
        raise ValueError(f"ef_search must be between 1 and {MAX_EF_SEARCH}, got {ef_search}")
    dims = _validated_dimensions(dimensions)
    expression = f"ce.embedding::vector({dims}) <=> %s::vector({dims})"
    with conn.transaction():
        conn.execute("SELECT set_config('hnsw.ef_search', %s, true)", (str(ef_search),))
        conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
        conn.execute("SET LOCAL enable_seqscan = off")
        return _run(conn, expression, query_vector, model, dims, limit, filters)


def exact_vector_search(
    conn: psycopg.Connection,
    query_vector: Sequence[float],
    *,
    model: str,
    dimensions: int,
    limit: int,
    filters: SearchFilters | None = None,
) -> list[RetrievedChunk]:
    """Return the `limit` nearest chunks by scanning every vector, bypassing the HNSW index.

    The recall baseline for `vector_search`. The distance expression omits the `::vector(N)` cast
    on the stored column, so it cannot match the index; index scans are also disabled.
    """
    dims = _validated_dimensions(dimensions)
    expression = f"ce.embedding <=> %s::vector({dims})"
    with conn.transaction():
        conn.execute("SET LOCAL enable_indexscan = off")
        conn.execute("SET LOCAL enable_bitmapscan = off")
        return _run(conn, expression, query_vector, model, dims, limit, filters)


def _run(
    conn: psycopg.Connection,
    distance_expression: str,
    query_vector: Sequence[float],
    model: str,
    dimensions: int,
    limit: int,
    filters: SearchFilters | None,
) -> list[RetrievedChunk]:
    if limit < 1:
        raise ValueError(f"limit must be >= 1, got {limit}")
    if len(query_vector) != dimensions:
        raise ValueError(f"query vector has {len(query_vector)} dimensions, expected {dimensions}")
    conditions, filter_params = filter_conditions(filters)
    sql = _SQL.format(
        distance_expression=distance_expression,
        filters="".join(f" AND {condition}" for condition in conditions),
    )
    rows = conn.execute(
        sql, (vector_literal(query_vector), model, *filter_params, limit)
    ).fetchall()
    return [
        chunk_from_row(row, rank, keyword=False, vector=True)
        for rank, row in enumerate(rows, start=1)
    ]


def _validated_dimensions(dimensions: int) -> int:
    if isinstance(dimensions, bool) or not isinstance(dimensions, int):
        raise ValueError(f"dimensions must be an integer, got {dimensions!r}")
    if not 1 <= dimensions <= MAX_DIMENSIONS:
        raise ValueError(f"dimensions must be between 1 and {MAX_DIMENSIONS}, got {dimensions}")
    return dimensions
