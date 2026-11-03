# Architecture

campus-rag answers a question in two phases. Offline, it builds a searchable index of public
documents. Online, it retrieves the most relevant passages and asks an LLM to answer only from them.

Ingestion and the index are built. Retrieval, generation, and the interfaces are planned for
milestones 6, 7, and 9 of the [MVP plan](plans/mvp.md).

## Design principles

- **Ground every answer.** The model answers only from retrieved passages and cites them. When the
  passages do not support an answer, the service says so.
- **Deterministic code owns facts; the LLM owns language.** Fetching, chunking, retrieval, ranking,
  filtering, and citation validation are deterministic and testable. The LLM rewrites queries and
  composes answers. See [foundations](agent-guidance/foundations.md).
- **Measure before changing.** Retrieval and prompt changes are compared against the
  [evaluation](evaluation.md) baseline.
- **Keep it small.** One PostgreSQL database holds text, metadata, keyword index, and vectors.

## Components

```text
                 +-----------------------------  offline  ------------------------------+
Allowed sources -> fetch -> extract text -> chunk -> embed -> PostgreSQL (pgvector + FTS)
                 +-----------------------------------------------------------------------+

                 +-----------------------------  online  -------------------------------+
Question -> query rewrite -> keyword search ---+
                          -> vector search  ---+-> RRF fusion -> rerank -> prompt -> LLM
                                                                     |
                                       answer + citations <- citation check
                 +-----------------------------------------------------------------------+
```

### Ingestion

`uv run python -m campus_rag.ingest` runs these stages; see [corpus ingestion](../runbooks/ingest-corpus.md).

- **Select.** Read each configured sitemap and keep URLs matching the rules in
  [`config/sources.toml`](../config/sources.toml). A sitemap that cannot be read stops the run
  before anything is written.
- **Fetch.** Check `robots.txt`, make at most one request per second with a descriptive user agent,
  retry transient failures, and cache pages under the ignored `data/raw/` directory. Conditional
  requests are sent when a site provides validators; the Policy Directory does not, so every run
  re-downloads its pages and content hashes decide what changed.
- **Extract.** Read the page's `<main>` element, keep the heading hierarchy, and drop the in-page
  table of contents, scripts, and navigation-only blocks. Pages under the source's minimum word
  count (section index pages) are skipped.
- **Chunk.** Split at heading boundaries, then at paragraphs and sentences for long sections, aiming
  for about 400 tokens with a hard maximum of 800 and about 50 tokens of overlap between split
  pieces. Short sections merge into a neighbor so no chunk is a fragment. Each chunk starts with its
  heading path so it stands on its own.
- **Store.** Upsert each document and its chunks in one transaction. An unchanged document keeps
  its chunks; a changed one has them replaced; on a full run, documents no longer selected are
  removed. Failed pages never delete stored data.
- **Embed.** Send each chunk that lacks a current embedding for the configured model to the
  embedding server in batches of 32, and save each batch before requesting the next. A rerun
  embeds only new or changed chunks.

### Index

PostgreSQL 17 with the pgvector extension holds the corpus. The schema is defined by the numbered
migrations in [`src/campus_rag/index/migrations/`](../src/campus_rag/index/migrations/).

| Table | One row per | Columns |
|-------|-------------|---------|
| `documents` | Fetched page | `source`, `url` (unique), `title`, fetch metadata (`etag`, `last_modified`, `fetched_at`), `content_hash`, `word_count`, `ingested_at`. |
| `chunks` | Chunk of a document | `document_id`, `ordinal`, `heading_path`, `content` (starts with the heading path), `token_count`, `content_hash`, and `search_tsv`. Deleted with its document. |
| `chunk_embeddings` | Chunk and embedding model | `chunk_id`, `model`, `dimensions`, `content_hash` of the embedded text, `embedding`, `embedded_at`. Deleted with its chunk. |

- **Keyword index.** `search_tsv` is a generated `tsvector` that weights the heading path `A` and
  the body `B`, using PostgreSQL's `english` configuration. A GIN index covers it.
- **Vector index.** A partial HNSW index (cosine distance, `m = 16`, `ef_construction = 64`) covers
  `embedding::vector(768)` for rows where `model = 'nomic-embed-text'`. HNSW needs a fixed
  dimension, so each model gets its own partial index, and vector queries filter by model and use
  the same cast.
- **Embeddings.** [`embed.py`](../src/campus_rag/index/embed.py) calls an OpenAI-compatible
  `/embeddings` endpoint, by default Ollama serving `nomic-embed-text` on this machine. Chunks are
  sent with the `search_document: ` prefix and questions with `search_query: `. Several models can
  coexist for comparison; a stored `content_hash` that differs from the chunk's marks the
  embedding as stale.

### Retrieval

Planned for M6.

1. **Query rewrite.** Turn a conversational follow-up into a standalone question.
2. **Hybrid search.** Run keyword and vector searches in parallel and take the top results from each.
3. **Fusion.** Merge result lists with reciprocal rank fusion (RRF). Keyword search catches exact
   names, codes, and acronyms; vector search catches paraphrases.
4. **Metadata filters.** Restrict by source or section when the caller requests it.
5. **Rerank (optional).** Rescore the fused candidates with a cross-encoder and keep the best few.

### Generation

Planned for M7.

- The prompt contains instructions, numbered passages with source IDs, and the question.
- The model must cite passage IDs for each claim and must decline when the passages are insufficient.
- A deterministic check rejects citations to passages that were not supplied.
- Only the top passages are included, ordered by relevance, to stay within the context window.

### Interfaces

Planned for M9.

- **FastAPI** exposes `search` and `answer` endpoints.
- **MCP tool** exposes `search` so an agent decides when to retrieve (agentic RAG).

## Technology choices

Each choice is recorded with its alternatives in the [MVP design decisions](plans/mvp-design.md).

| Concern | Choice | Reason |
|---------|--------|--------|
| Language and tooling | Python 3.12 with uv | Mature RAG ecosystem; one tool for environments, locked dependencies, and commands. |
| Store | PostgreSQL 17 + pgvector in Docker Compose | Vectors, keyword search, and metadata in one database with SQL filtering. |
| Database access | psycopg 3 with plain SQL and numbered migrations | Retrieval queries stay visible and reviewable. |
| Embeddings | Ollama with `nomic-embed-text` (768 dimensions) in Docker Compose, through an OpenAI-compatible client | Free and local, so no corpus text leaves the machine; another model or provider is a configuration change. |
| LLM | Chosen in M7: a local Ollama chat model or the Gemini API free tier | See [decision 10g](plans/mvp-design.md#10g-answer-generation-m7). |
| API | FastAPI (M9) | Simple typed HTTP API with generated OpenAPI documentation. |
| Evaluation | Golden set, deterministic retrieval metrics, and an LLM judge for faithfulness | Separates retrieval failures from generation failures. |

## Open questions

- Which embedding model gives the best recall@5 on the golden set for its cost?
- Does reranking improve answer faithfulness enough to justify its latency?
- What chunk size and overlap work best for policy documents with long numbered sections?