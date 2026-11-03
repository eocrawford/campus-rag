"""Keyword search over the stemmed `chunks.search_tsv`, ranked by BM25 (or `ts_rank_cd`).

A question's stemmed lexemes (stop words dropped) are joined with OR: AND matching returned
nothing for natural-language questions because every word must match.

PostgreSQL's `ts_rank_cd` has no inverse document frequency, so a word found in most chunks
("university", "policy", "student") counts as much as a rare one ("military"), and keyword
search ranked poorly on the golden set. BM25 weights each lexeme by how rare it is and
normalizes for chunk length; docs/results/retrieval.md compares the two. It is computed in SQL
from the `search_tsv` positions, with the textbook constants `k1 = 1.2` and `b = 0.75`. A
heading occurrence counts 2.5 times a body occurrence, the ratio of the A and B weights that
`ts_rank_cd` uses by default (1.0 and 0.4). `ts_rank_cd` stays available as `ranking=` so
evaluation can show the difference.
"""

from typing import Literal

import psycopg

from campus_rag.retrieve.models import RetrievedChunk, SearchFilters
from campus_rag.retrieve.sql import CHUNK_COLUMNS, chunk_from_row, filter_conditions

KeywordRanking = Literal["bm25", "ts_rank_cd"]
KEYWORD_RANKINGS: tuple[KeywordRanking, ...] = ("bm25", "ts_rank_cd")

BM25_K1 = 1.2
BM25_B = 0.75
HEADING_WEIGHT = 2.5

# Lexemes come from to_tsvector, so they are already stemmed and free of stop words. The tsquery
# is rebuilt from them with every lexeme quoted (a backslash or quote inside a lexeme is escaped);
# no usable lexeme leaves the query NULL, which matches nothing. Document frequency is counted over
# the whole corpus through the GIN index, so a filter does not change a term's weight.
_TERMS_SQL = """
WITH terms AS (
    SELECT lexeme FROM unnest(tsvector_to_array(to_tsvector('english', %s))) AS lexeme
),
q AS (
    SELECT string_agg(
        '''' || replace(replace(lexeme, '\\', '\\\\'), '''', '''''') || '''', ' | '
    )::tsquery AS tsq
    FROM terms
)
"""

_BM25_SQL = (
    _TERMS_SQL
    + """,
stats AS MATERIALIZED (
    SELECT count(*)::float8 AS total, avg(token_count)::float8 AS avg_length FROM chunks
),
idf AS MATERIALIZED (
    SELECT terms.lexeme,
           ln(1 + (stats.total - n.docs + 0.5) / (n.docs + 0.5)) AS weight
    FROM terms
    CROSS JOIN stats
    CROSS JOIN LATERAL (
        SELECT count(*)::float8 AS docs FROM chunks
        WHERE search_tsv @@ ('''' || replace(replace(terms.lexeme, '\\', '\\\\'), '''', '''''')
                             || '''')::tsquery
    ) AS n
)
SELECT {columns}, sum(
    idf.weight * tf.weighted_tf * ({k1} + 1)
    / (tf.weighted_tf + {k1} * (1 - {b} + {b} * c.token_count / stats.avg_length))
) AS score
FROM q
JOIN chunks c ON c.search_tsv @@ q.tsq
JOIN documents d ON d.id = c.document_id
CROSS JOIN stats
CROSS JOIN LATERAL (
    SELECT u.lexeme,
           {heading} * cardinality(array_positions(u.weights, 'A'))
               + cardinality(array_positions(u.weights, 'B')) AS weighted_tf
    FROM unnest(c.search_tsv) AS u(lexeme, positions, weights)
) AS tf
JOIN idf ON idf.lexeme = tf.lexeme
WHERE true {filters}
GROUP BY c.id, d.id
ORDER BY score DESC, c.id
LIMIT %s
"""
)

# Normalization 32 maps ranks into [0, 1) as rank / (rank + 1).
_TS_RANK_CD_SQL = (
    _TERMS_SQL
    + """
SELECT {columns}, ts_rank_cd(c.search_tsv, q.tsq, 32) AS score
FROM q
JOIN chunks c ON c.search_tsv @@ q.tsq
JOIN documents d ON d.id = c.document_id
WHERE true {filters}
ORDER BY score DESC, c.id
LIMIT %s
"""
)


def keyword_search(
    conn: psycopg.Connection,
    query: str,
    *,
    limit: int,
    filters: SearchFilters | None = None,
    ranking: KeywordRanking = "bm25",
) -> list[RetrievedChunk]:
    """Return up to `limit` chunks sharing any stemmed term with `query`, best first.

    A query with no searchable terms (empty, punctuation, or only stop words) returns [].
    Equal scores order by chunk id.
    """
    if limit < 1:
        raise ValueError(f"limit must be >= 1, got {limit}")
    if ranking not in KEYWORD_RANKINGS:
        raise ValueError(f"unknown keyword ranking {ranking!r}")
    conditions, params = filter_conditions(filters)
    template = _BM25_SQL if ranking == "bm25" else _TS_RANK_CD_SQL
    sql = template.format(
        columns=CHUNK_COLUMNS,
        filters="".join(f" AND {condition}" for condition in conditions),
        k1=BM25_K1,
        b=BM25_B,
        heading=HEADING_WEIGHT,
    )
    # PostgreSQL text cannot hold NUL characters.
    rows = conn.execute(sql, (query.replace("\x00", " "), *params, limit)).fetchall()
    return [
        chunk_from_row(row, rank, keyword=True, vector=False)
        for rank, row in enumerate(rows, start=1)
    ]
