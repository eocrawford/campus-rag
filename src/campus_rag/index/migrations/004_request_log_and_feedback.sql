-- Usage analytics: one request_log row per API request or MCP tool call, and optional ratings.
--
-- The tables hold no user identity: no address, session, or account. Rows stay in this database.
-- Timings are whole milliseconds. top_score is the best retrieval score (RRF, BM25, or cosine
-- similarity, depending on the mode), result_count the passages returned or given to the model.

CREATE TABLE request_log (
    id bigserial PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now(),
    interface text NOT NULL CHECK (interface IN ('api', 'mcp', 'cli')),
    endpoint text NOT NULL,
    question text,
    search_query text,
    mode text,
    k integer,
    latency_ms integer,
    retrieve_ms integer,
    generate_ms integer,
    top_score real,
    result_count integer,
    refused boolean,
    citation_count integer,
    answer_model text,
    embedding_model text,
    error text
);

CREATE INDEX request_log_created_at_idx ON request_log (created_at);

CREATE TABLE answer_feedback (
    id bigserial PRIMARY KEY,
    request_id bigint NOT NULL REFERENCES request_log (id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    rating smallint NOT NULL CHECK (rating IN (-1, 1)),
    comment text CHECK (char_length(comment) <= 1000)
);

CREATE INDEX answer_feedback_request_id_idx ON answer_feedback (request_id);
