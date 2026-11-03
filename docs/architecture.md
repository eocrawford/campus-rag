# Architecture

campus-rag answers a question in two phases. Offline, it builds a searchable index of public
documents. Online, it retrieves the most relevant passages and asks an LLM to answer only from them.

> This document describes the planned design. Sections become current as milestones in the
> [MVP plan](plans/mvp.md) complete.

## Design principles

- **Ground every answer.** The model answers only from retrieved passages and cites them. When the
  passages do not support an answer, the service says so.
- **Deterministic code owns facts; the LLM owns language.** Fetching, chunking, retrieval, ranking,
  filtering, and citation validation are deterministic and testable. The LLM rewrites queries and
  composes answers.
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

- Fetch only sources listed in [data sources](data-sources.md), honoring `robots.txt` and rate limits.
- Extract main-body text and section headings; drop navigation and boilerplate.
- Split text into chunks of roughly 300-800 tokens with overlap, preferring heading and paragraph
  boundaries. Prefix each chunk with its document title and section heading so it remains
  meaningful on its own.
- Record metadata: source URL, title, section, retrieval date, and content hash. The hash makes
  re-ingestion idempotent and skips unchanged chunks.

### Index

PostgreSQL with the pgvector extension stores one row per chunk:

| Column | Purpose |
|--------|---------|
| `id`, `document_id` | Chunk and parent document identity. |
| `content` | Chunk text shown to the model and the user. |
| `embedding` | Vector from the configured embedding model. |
| `content_tsv` | PostgreSQL full-text search vector for keyword retrieval. |
| `url`, `title`, `section`, `fetched_at`, `content_hash` | Citation and freshness metadata. |

Vector search uses an HNSW index with cosine distance. Keyword search uses a GIN index on
`content_tsv`. The embedding model name and dimension are recorded so a model change forces a
full re-embed.

### Retrieval

1. **Query rewrite.** Turn a conversational follow-up into a standalone question.
2. **Hybrid search.** Run keyword and vector searches in parallel and take the top results from each.
3. **Fusion.** Merge result lists with reciprocal rank fusion (RRF). Keyword search catches exact
   names, codes, and acronyms; vector search catches paraphrases.
4. **Metadata filters.** Restrict by source or section when the caller requests it.
5. **Rerank (optional).** Rescore the fused candidates with a cross-encoder and keep the best few.

### Generation

- The prompt contains instructions, numbered passages with source IDs, and the question.
- The model must cite passage IDs for each claim and must decline when the passages are insufficient.
- A deterministic check rejects citations to passages that were not supplied.
- Only the top passages are included, ordered by relevance, to stay within the context window.

### Interfaces

- **FastAPI** exposes `search` and `answer` endpoints.
- **MCP tool** exposes `search` so an agent decides when to retrieve (agentic RAG).

## Technology choices

| Concern | Choice | Reason |
|---------|--------|--------|
| Language and API | Python, FastAPI | Mature RAG ecosystem; simple typed HTTP API. |
| Store | PostgreSQL + pgvector | Vectors, keyword search, and metadata in one database with SQL filtering. |
| Embeddings | Configurable OpenAI-compatible API; local sentence-transformers option | Compare quality and cost without code changes. |
| LLM | Configurable OpenAI-compatible chat API | Swap providers through configuration. |
| Evaluation | Golden set plus deterministic retrieval metrics and an LLM judge for faithfulness | Separates retrieval failures from generation failures. |

## Open questions

- Which embedding model gives the best recall@5 on the golden set for its cost?
- Does reranking improve answer faithfulness enough to justify its latency?
- What chunk size and overlap work best for policy documents with long numbered sections?
