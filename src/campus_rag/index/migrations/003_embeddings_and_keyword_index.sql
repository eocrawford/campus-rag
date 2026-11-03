-- Embeddings and keyword search for chunks.
--
-- chunk_embeddings holds one vector per (chunk, model) so several models can coexist for
-- side-by-side evaluation. `embedding` has no fixed dimension; each row records its model's
-- `dimensions` and a CHECK keeps the two consistent. HNSW needs a fixed dimension, so each model
-- gets a partial index over `embedding::vector(N)`. Queries must use the same expression and a
-- `model = '<name>'` filter to use it:
--
--   SELECT chunk_id FROM chunk_embeddings WHERE model = 'nomic-embed-text'
--   ORDER BY embedding::vector(768) <=> '[...]'::vector(768) LIMIT 20;
--
-- Index parameters and hnsw.ef_search are tuned in M6.

CREATE TABLE chunk_embeddings (
    chunk_id bigint NOT NULL REFERENCES chunks (id) ON DELETE CASCADE,
    model text NOT NULL,
    dimensions integer NOT NULL CHECK (dimensions > 0),
    content_hash text NOT NULL,
    embedding vector NOT NULL,
    embedded_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (chunk_id, model),
    CHECK (vector_dims(embedding) = dimensions)
);

CREATE INDEX chunk_embeddings_nomic_embed_text_hnsw_idx
    ON chunk_embeddings
    USING hnsw ((embedding::vector(768)) vector_cosine_ops)
    WITH (m = 16, ef_construction = 64)
    WHERE model = 'nomic-embed-text';

-- Keyword search: the heading path is weighted A and the body B. A chunk's `content` starts with
-- its heading path and a blank line; that prefix is dropped from the body so the heading is not
-- counted twice.
ALTER TABLE chunks ADD COLUMN search_tsv tsvector GENERATED ALWAYS AS (
    setweight(to_tsvector('english', heading_path), 'A')
    || setweight(
        to_tsvector(
            'english',
            CASE
                WHEN starts_with(content, heading_path || E'\n\n')
                    THEN substr(content, length(heading_path) + 3)
                ELSE content
            END
        ),
        'B'
    )
) STORED;

CREATE INDEX chunks_search_tsv_idx ON chunks USING gin (search_tsv);
