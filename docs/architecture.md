# Architecture

campus-rag answers a question in two phases. Offline, it builds a searchable index of public
documents. Online, it retrieves the most relevant passages and asks an LLM to answer only from them.

Ingestion, the index, retrieval, and grounded answers are built. The interfaces are planned for
milestone 9 of the [MVP plan](plans/mvp.md).

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

`uv run python -m campus_rag.retrieve "<query>"` prints ranked passages for inspection, and
`uv run python -m campus_rag.evaluate retrieval` measures them against the golden set; see
[evaluate retrieval](../runbooks/evaluate-retrieval.md) and the
[results](results/retrieval.md). The code is in
[`src/campus_rag/retrieve/`](../src/campus_rag/retrieve/). `PolicySearcher` implements the
`Searcher` protocol with three modes, and every mode returns up to 50 `RetrievedChunk` rows.

1. **Keyword search.** The question's stemmed words, minus stop words, are joined with OR. Chunks
   that share any word are ranked by BM25 computed in SQL over `search_tsv`: each word is weighted
   by how rare it is in the corpus, a heading occurrence counts 2.5 times a body occurrence, and
   long chunks are normalized (`k1 = 1.2`, `b = 0.75`). PostgreSQL's own `ts_rank_cd` has no
   inverse document frequency and ranks common words like "university" as highly as rare ones, so
   it is kept only as a comparison baseline. Questions with no searchable words return nothing.
2. **Vector search.** The question is embedded with the `search_query: ` prefix and compared with
   the stored vectors by cosine distance through the partial HNSW index at `hnsw.ef_search = 100`.
   The score is `1 - distance`. The statement disables sequential scans so the planner uses the
   index even on a corpus this small. With filters, `hnsw.iterative_scan = relaxed_order` keeps
   scanning the index until enough rows pass them. An exact scan of the same vectors is the recall
   baseline for the index.
3. **Hybrid search.** The top 50 results of each search are merged with reciprocal rank fusion
   (RRF, `k = 60`). Keyword search catches exact names, codes, and acronyms; vector search catches
   paraphrases. Each result records its keyword and vector ranks.
4. **Metadata filters.** A caller can restrict any mode to documents from given sources or whose
   URL starts with a given prefix. Both searches apply the filter before ranking.
5. **Rerank.** Not part of the MVP; no local cross-encoder runs without adding PyTorch, and LLM
   reranking costs about 40 seconds per query on this CPU.

### Generation

`uv run python -m campus_rag.ask "<question>"` answers from the command line; see
[ask questions](../runbooks/ask-questions.md). The code is in
[`src/campus_rag/generate/`](../src/campus_rag/generate/), and the model choices are recorded in
[decision 12a](plans/mvp-design.md#12a-models).

1. **Rewrite.** A follow-up question is rewritten into a standalone query by `qwen2.5:3b-instruct`,
   which sees up to three earlier user questions but not the answers. An empty or overlong
   rewrite, or a failed call, falls back to the question as typed. A first question is never
   rewritten.
2. **Retrieve.** Hybrid search returns the top eight chunks.
3. **Assemble the prompt.** Up to six passages, in rank order, fit a 3,000-token budget counted with
   `cl100k_base`. A passage that would exceed the budget is skipped and smaller later passages
   still fit; the top passage is always included, truncated if it alone exceeds the budget. Each
   passage is numbered `[n]` with its heading path and URL. The instructions require an answer only
   from the passages, a citation for every claim, under 150 words, and a fixed refusal sentence
   when the passages lack the answer.
4. **Generate.** `qwen2.5:7b-instruct` answers at temperature 0 through an OpenAI-compatible
   `/chat/completions` endpoint, in plain text. The client retries timeouts, connection errors,
   HTTP 429, and 5xx responses with backoff.
5. **Check citations.** Code parses `[1]`, `[1, 3]`, `[1][2]`, and `[2-3]` markers, removes numbers
   that match no supplied passage, and normalizes the rest to `[1][3]`. An answer that starts with
   the refusal sentence, or has no valid citation left, becomes exactly the refusal sentence, so no
   uncited claim reaches the user. The returned `Answer` lists the cited passages in order of first
   use, the removed citation numbers, and per-stage timings.

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