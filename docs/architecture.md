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
