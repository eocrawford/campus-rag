-- Documents fetched from allowlisted sources and their heading-prefixed chunks.
-- Search columns (full-text and embeddings) arrive in a later migration.

CREATE TABLE documents (
    id bigserial PRIMARY KEY,
    source text NOT NULL,
    url text NOT NULL UNIQUE,
    title text NOT NULL,
    etag text,
    last_modified text,
    fetched_at timestamptz NOT NULL,
    content_hash text NOT NULL,
    word_count integer NOT NULL,
    ingested_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX documents_source_idx ON documents (source);

CREATE TABLE chunks (
    id bigserial PRIMARY KEY,
    document_id bigint NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
    ordinal integer NOT NULL,
    heading_path text NOT NULL,
    content text NOT NULL,
    token_count integer NOT NULL,
    content_hash text NOT NULL,
    UNIQUE (document_id, ordinal)
);
