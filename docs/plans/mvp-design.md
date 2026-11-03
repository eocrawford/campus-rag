# MVP design decisions

This record tracks the design decisions for the [MVP plan](mvp.md). Each decision lists the
options considered, the choice, and its status.

| # | Decision | Choice | Status |
|---|----------|--------|--------|
| 1 | Model provider | OpenAI-compatible interface; provider set by configuration | Reopened 2026-11-03; superseded by [decision 10](#10-m4-index-embeddings-and-keyword-search) |
| 2 | Corpus | UW Policy Directory and UW-IT Connect, under the [terms constraints](../data-sources.md#uw-terms-of-use-constraints) | Decided 2026-11-03 |
| 3 | Python tooling | uv | Decided 2026-11-03 |
| 4 | Database access | psycopg 3 with plain SQL and numbered migrations | Decided 2026-11-03 |
| 5 | Package layout | Single `campus_rag` package organized by pipeline stage | Decided 2026-11-03 |
| 6 | Test strategy | Fake-provider unit tests, marked database tests, explicit evaluation runs | Decided 2026-11-03 |
| 7 | Capability coverage additions | Add usage analytics with user feedback (M11); mock ticketing-system tool as an optional stretch (M12) | Decided 2026-11-03 |
| 8 | Installs on restricted networks | Opt-in `-PackageIndexUrl` mirror install with lock hashes enforced; `uv.lock` stays canonical; `uv_build` backend so rebuilds need no download | Decided 2026-11-03 |
| 9 | M3 corpus pipeline | UW Policy Directory only; sitemap-driven selection; cached polite fetch; heading-aware extraction and chunking; documents and chunks in PostgreSQL; new `campus_rag.ingest` command | Decided 2026-11-03 |
| 10 | M4 index: embeddings and keyword search | Local embeddings with Ollama (`nomic-embed-text`, 768 dimensions) in Docker Compose through an OpenAI-compatible client; per-model embeddings table with HNSW; weighted full-text index; embedding stage in `campus_rag.ingest` | Decided 2026-11-03 |
| 11 | M5-M6 golden set and retrieval | Committed golden set with URL and heading targets; keyword (OR query, `ts_rank_cd`), vector (HNSW), and hybrid (RRF) search; no reranker | Decided 2026-11-03 (maintainer-delegated) |
| 12 | M7-M8 answers and answer evaluation | `qwen2.5:7b-instruct` answers through Ollama; `qwen2.5:3b-instruct` rewrites follow-ups; deterministic citation checks and refusals; `llama3.1:8b` judges | Decided 2026-11-03 (maintainer-delegated) |
| 13 | M9 interfaces | FastAPI on loopback (`/search`, `/answer`, `/health`); MCP server over stdio with a `search_policies` tool | Decided 2026-11-03 (maintainer-delegated) |
| 14 | M11-M12 analytics and mock ticketing | Local request log and feedback tables with a usage report; a loopback mock ticketing API and an MCP `draft_ticket` tool | Decided 2026-11-03 (maintainer-delegated) |

## 1. Model provider

**Status:** superseded by [decision 10](#10-m4-index-embeddings-and-keyword-search), which
chooses local embeddings with Ollama. The answer-generation model is chosen in M7
([10g](#10g-answer-generation-m7)).

Model calls use the OpenAI-compatible API shape, so the provider is a configuration value (base
URL, API key, model name) rather than code.

**Rule:** this is a personal project. It must not use employer subscriptions, endpoints, or
credentials.

History: the first choice, GitHub Models, was retired on 2026-07-30.

## 2. Corpus

**Decision:** public pages from the [UW Policy Directory](https://policy.uw.edu) and
[UW-IT Connect](https://it.uw.edu) help articles. Both are public, relevant to university IT, and
useful to staff and students. [Decision 9](#9a-sources) defers IT Connect.

Both sites' `robots.txt` and terms of use were reviewed on 2026-11-03 and recorded in
[data sources](../data-sources.md). The terms limit reproduction and redistribution, so content
stays local and the golden set is written in original words.

## 3. Python tooling

| Option | Tradeoff |
|--------|----------|
| **uv** | Fast, reproducible lockfile, one tool for environments, dependencies, and running commands. |
| pip + venv | Universally familiar; no lockfile without extra tools. |

**Decision:** uv.

## 4. Database access

| Option | Tradeoff |
|--------|----------|
| **psycopg 3 + plain SQL** | Retrieval queries are visible and reviewable; fewer layers. |
| SQLAlchemy ORM | Portable models; hides the vector and full-text SQL that this project exists to show. |

**Decision:** psycopg 3, plain SQL with vectors passed as pgvector text literals, and numbered
`.sql` migration files applied in order.

## 5. Package layout

```text
src/campus_rag/
  config.py        settings from environment variables
  ingest/          fetch, extract, chunk
  index/           database access, embedding
  retrieve/        keyword, vector, fusion
  generate/        prompt assembly, answer, citation checks
  evaluate/        golden set, retrieval and answer metrics
  api.py           FastAPI service
  mcp_server.py    MCP search tool
```

## 6. Test strategy

- **Unit tests** use `pytest` with fake model providers and never call paid APIs.
- **Database tests** run against the local PostgreSQL container and carry a marker so they can be skipped.
- **Evaluation** runs only when invoked explicitly; it is not part of the test suite.

## 7. Capability coverage additions

The [glossary](../glossary.md) maps common AI-agent engineering capabilities to this project.
Several are marked **Background** because the MVP does not exercise them. Options for covering
the most valuable gaps:

| Option | Capability covered | Cost | Decision |
|--------|--------------------|------|----------------|
| **Usage analytics and feedback** | Monitoring performance, usage analytics, user feedback loops | Small: log each request's latency, retrieval scores, and outcome; accept a thumbs-up/down rating; report summaries | Approved as M11 |
| **Mock ticketing tool** | Enterprise-system integration, workflow automation | Medium: an MCP tool that drafts a help-desk ticket in a local mock ticketing API when the corpus cannot answer | Approved as optional stretch M12 |
| Multi-agent orchestration (CrewAI or A2A) | Agent orchestration, agent-to-agent coordination | Large; adds frameworks without improving answer quality | Skip for MVP |
| Fine-tuning | Model customization | Large; retrieval already supplies the domain knowledge | Skip |
| No-code builder | Tools for non-technical users | Large; outside this project's purpose | Skip |

## 8. Installs on restricted networks

Some networks block `files.pythonhosted.org`, where PyPI serves package files, and require an
internal package mirror instead. A plain `uv sync` then fails with a TLS handshake error; trusting
the system certificate store (`--native-tls`) does not help because the host is blocked, not
intercepted.

| Option | Tradeoff |
|--------|----------|
| **Opt-in mirror install** (`-PackageIndexUrl`) | Installs the exact locked packages from a mirror with hashes from `uv.lock` enforced, then proves the environment matches the lock with `uv sync --locked --offline`. `uv.lock` keeps canonical PyPI URLs. Needs one extra parameter on restricted networks. |
| Lock against the mirror | Simple on a restricted network, but writes private mirror URLs into a public lockfile and breaks installs everywhere else. |
| Configure the mirror globally for uv | Makes uv treat the canonical lock as stale and rewrite it. |

**Decision:** opt-in mirror install. Without the parameter, the setup script fails at step 4
and prints the rerun command with the mirror detected from `pip config list`.

### 8b. Build backend

On a restricted network, a hatchling-built project must download hatchling from the blocked host
whenever uv rebuilds the editable install, which happens after any `pyproject.toml` change, and
hatchling arrives without a hash check because it is not pinned in `uv.lock`.

| Option | Tradeoff |
|--------|----------|
| Keep hatchling | Restricted networks need a mirror rerun after every `pyproject.toml` change, and the backend is installed unverified. |
| **Switch to `uv_build`** (`requires = ["uv_build>=0.13.0,<0.14"]`) | uv builds the project itself with no download, so rebuilds work offline and nothing unverified is installed. The version range must be raised when the project moves to a new uv minor version; outside the range uv downloads `uv_build` like any other backend. |

**Decision:** `uv_build`. The wheel builds offline and includes the migration `.sql` files, and
after a `pyproject.toml` change a plain `uv run` rebuilds without the mirror.

## 9. M3 corpus pipeline

A survey on 2026-11-03 found:

- **UW Policy Directory** (`policy.uw.edu`): 321 pages in `wp-sitemap-posts-page-1.xml`, mostly
  under `/directory/` (Administrative Policy Statements 150, Presidential Orders 78, Board of
  Regents Governance 34, Student Governance and Policies 27, Faculty Code 21, Executive Orders 6).
  Pages are server-rendered WordPress with one `<main>`, one `<h1>`, and numbered `<h2>`/`<h3>`
  sections. A sample policy has about 3,500 words in 20 sections.
- **UW-IT Connect** (`it.uw.edu`): 686 sitemap URLs, but pages are a ServiceNow portal rendered in
  the browser. The HTML holds only an application shell; article text is not present.

### 9a. Sources

| Option | Tradeoff |
|--------|----------|
| **Policy Directory only** | Clean, structured HTML; enough content for a realistic retrieval challenge. |
| Add IT Connect through a headless browser | Doubles topical coverage, but adds a browser dependency, slower and heavier requests, and fragility to portal changes. |
| Substitute another server-rendered UW site | Possible later; needs its own `robots.txt` and terms review. |

**Decision:** Policy Directory only for the MVP. IT Connect stays approved but deferred.

### 9b. Page selection

| Option | Tradeoff |
|--------|----------|
| **All `/directory/` policy pages with at least 150 words of body text** | About 300 pages, roughly 1,500-2,000 chunks; realistic distractors for retrieval; still small to fetch (about 6 minutes at one request per second) and cheap to embed. |
| Curated slice of about 50 pages | Faster to inspect, but too few distractors to show hybrid retrieval's value. |

**Decision:** all `/directory/` policy pages, filtered by a minimum word count so section
index pages are skipped. Selection rules live in a committed configuration file (sitemap URL,
include prefixes, exclusions, optional page cap); only URLs are committed, never content.

### 9c. Fetching

- Read `robots.txt` at run time and skip disallowed URLs.
- One request per second, descriptive user agent linking to this repository.
- Cache raw HTML under the ignored `data/raw/` directory with its fetch metadata.
- Re-fetch with conditional requests (`If-None-Match`, `If-Modified-Since`) when the site sends
  validators. `policy.uw.edu` sends none, so every run re-downloads its pages and content hashes
  decide what changed.
- Retry transient failures (timeouts, HTTP 429 and 5xx) with backoff; record permanent failures
  and continue.

### 9d. Extraction

| Option | Tradeoff |
|--------|----------|
| **BeautifulSoup scoped to `<main>`** | Explicit and testable; keeps the heading hierarchy that chunking needs. Site-specific. |
| Generic extractor (trafilatura) | Works on many sites without rules, but can flatten headings and drop tables. |

**Decision:** BeautifulSoup (with the `lxml` parser) scoped to `<main>`. Drop navigation
blocks such as the in-page table of contents; keep headings, paragraphs, lists, and table rows.
Tests use small saved HTML fixtures written for the tests, not copied pages.

### 9e. Chunking

- Split at `<h2>`/`<h3>` boundaries first; split long sections at paragraph, then sentence
  boundaries; merge very short adjacent sections.
- Target about 400 tokens, maximum 800, with about 50 tokens of overlap when a section is split.
- Prefix each chunk with its heading path, for example `APS 2.4 ... > 2. Roles and
  Responsibilities > D. Data Trustees`, so it stands on its own.
- Count tokens with `tiktoken` using the `cl100k_base` encoding.

### 9f. Storage

Migration `002` creates two tables. [Decision 10](#10d-storage-schema) adds embeddings and a keyword-search column.

| Table | Key columns |
|-------|-------------|
| `documents` | `id`, `source`, `url` (unique), `title`, `fetched_at`, `etag`, `last_modified`, `content_hash` |
| `chunks` | `id`, `document_id`, `ordinal`, `heading_path`, `content`, `token_count`, `content_hash` |

Ingestion is idempotent: an unchanged document keeps its chunks; a changed document has its chunks
replaced in one transaction; a document no longer selected is removed.

### 9g. Command

A new entry point, `uv run python -m campus_rag.ingest`, runs selection, fetch, extraction,
chunking, and storage, with `--limit N` for trial runs and `--dry-run` to report without writing.
The maintainer approved this entry point.

## 10. M4 index: embeddings and keyword search

Embeddings are computed locally with Ollama, so the project runs at no cost and no corpus text
leaves the machine.

The corpus has 1,978 chunks of 82-796 tokens (median 196), about 450,000 tokens in total. The
embedding model must accept chunks up to 800 tokens without truncation.

### 10a. Embedding provider and model

| Option | Cost | Tradeoffs |
|--------|------|-----------|
| **Ollama with `nomic-embed-text`** (768 dimensions) | Free | Local and private; 8,192-token context covers every chunk; small and fast on CPU. Needs task prefixes (`search_document: ` for chunks, `search_query: ` for questions). |
| Ollama with `bge-m3` (1,024 dimensions) | Free | Strong and multilingual, but larger and slower on CPU. A good second model for comparison in M6. |
| Ollama with `mxbai-embed-large` (1,024 dimensions) | Free | Strong on benchmarks, but its 512-token context would truncate longer chunks. |
| OpenAI `text-embedding-3-small` (1,536 dimensions) | About $0.01 per full embed, with a $5 minimum prepaid credit | Highest convenience and quality per dollar; requires a paid account. |
| Google Gemini free tier | Free within limits | Free-tier prompts may be used to improve Google products. |

**Decision:** Ollama with `nomic-embed-text`. The client speaks the OpenAI-compatible
`/v1/embeddings` API that Ollama serves, so another model or provider is a configuration change.

### 10b. Running Ollama

| Option | Tradeoff |
|--------|----------|
| **Ollama in Docker Compose** (pinned `ollama/ollama` image, CPU) | No software installed on the host; starts, stops, and resets with the database; models persist in a named volume. |
| Native Ollama install | Slightly faster startup; adds a background service to the host. |

**Decision:** an `ollama` service in `compose.yaml`, bound to `127.0.0.1:11434`, with a model
volume. The setup script starts it, pulls the configured embedding model when missing, and
verifies that the model returns vectors of the configured size.

### 10c. Configuration and client

- Settings: `CAMPUS_RAG_EMBEDDING_BASE_URL` (default `http://127.0.0.1:11434/v1`),
  `CAMPUS_RAG_EMBEDDING_MODEL` (default `nomic-embed-text`), `CAMPUS_RAG_EMBEDDING_DIMENSIONS`
  (default `768`), `CAMPUS_RAG_EMBEDDING_DOCUMENT_PREFIX` (default `search_document: `),
  `CAMPUS_RAG_EMBEDDING_QUERY_PREFIX` (default `search_query: `), and an optional
  `CAMPUS_RAG_EMBEDDING_API_KEY` for hosted providers. Keys are never logged.
- Client: plain `httpx` against `/embeddings`, in batches of up to 32 chunks, with retries and
  backoff on connection errors, timeouts, HTTP 429, and 5xx. Responses with the wrong vector size are rejected.
- Tests use a deterministic fake embedder and need no model server.

### 10d. Storage schema

A `chunk_embeddings` table keyed by (`chunk_id`, `model`) with `dimensions`, the embedded
chunk's `content_hash`, `embedding vector`, and `embedded_at`; chunk deletion cascades.
Several models can coexist for side-by-side evaluation in M6. Migration `003` creates the table
and a partial HNSW index (cosine distance, `m = 16`, `ef_construction = 64`) over
`embedding::vector(768)` for `nomic-embed-text`. M6 kept these parameters after measuring recall
against an exact scan (see the [retrieval results](../results/retrieval.md)).

### 10e. Embedding stage and re-embedding

- Embed only chunks with no embedding for the configured model, or whose stored `content_hash`
  differs. A rerun with no changes sends only a one-request connectivity check.
- Changed documents replace their chunks, and embeddings cascade away with them.
- The `campus_rag.ingest` command embeds after storage; `--no-embed` skips the stage. When
  embedding is enabled and the model server is unreachable, lacks the model, or returns the wrong
  vector size, the command fails before fetching (exit code `7`) and names the fix.

### 10f. Keyword index

- A generated `search_tsv` column on `chunks`: heading path weighted `A`, body weighted `B`, with
  PostgreSQL's `english` configuration and a GIN index (migration `003`).
- Ranking started as `ts_rank_cd`. Stock PostgreSQL has no BM25 function, and M6 found that
  `ts_rank_cd` limits hybrid results because it has no inverse document frequency; keyword search
  now computes BM25 in SQL ([decision 11b](#11b-search)).

### 10g. Answer generation (M7)

The chat model for grounded answers is decided in M7. Candidates are a local Ollama model (free,
about 15-40 seconds per answer on this CPU) and the Gemini API free tier.

## Maintainer-delegated decisions

On 2026-11-03 the maintainer asked for milestones 5-12 to be built autonomously. Decisions 11-14
were made under that delegation, including consent for the new commands they introduce, and are
recorded here before implementation. Each is backed by the measurements cited.

## 11. M5-M6 golden set and retrieval

### 11a. Golden set

- `eval/golden_set.jsonl`, one question per line: `id`, `question`, `type`
  (`paraphrase`, `exact_term`, or `unanswerable`), `expected` (a list of `{url, heading_prefix}`
  targets), `reference_answer`, and optional `follow_up_of` for multi-turn cases.
- About 45 questions: roughly half paraphrased (few words shared with the source), a third exact-term
  (policy numbers, titles, defined terms), and at least six unanswerable.
- Questions and reference answers are written in original words; the file holds no copied policy
  text (see [UW terms constraints](../data-sources.md#uw-terms-of-use-constraints)).
- A retrieved chunk matches a target when its document URL equals the target URL and either
  heading path is a prefix of the other. Targets use URLs and headings rather than chunk IDs, so
  re-chunking does not invalidate the golden set.

### 11b. Search

- **Keyword:** the question's stemmed lexemes (from `to_tsvector('english', ...)`, so stop words
  drop out) joined with OR, ranked by BM25 computed in SQL over the weighted `search_tsv`
  (`k1 = 1.2`, `b = 0.75`, a heading occurrence counting 2.5 times a body occurrence, the ratio of
  the A and B weights `ts_rank_cd` uses by default). AND matching (`websearch_to_tsquery`)
  returned nothing for natural-language questions in M4 because every word must match. The first
  design ranked OR matches with `ts_rank_cd`; its recall@5 was 0.39 against 0.81 for BM25 on the
  golden set, because it has no inverse document frequency and scores "university" like a rare
  word. BM25 was adopted after that measurement with its textbook constants and no parameter
  search; both rankings stay in the code and the results compare them.
- **Vector:** cosine distance through the HNSW index with `hnsw.ef_search = 100` and
  `hnsw.iterative_scan = relaxed_order`, so filters still return enough rows. Sequential scans are
  disabled for the statement: at this corpus size the planner would otherwise choose an exact scan
  and the index would go unmeasured.
- **Hybrid:** the top 50 of each list fused with reciprocal rank fusion, `k = 60`.
- **Filters:** by source and URL prefix.
- **Reranking:** not in the MVP. No local cross-encoder runs without adding PyTorch, and LLM
  reranking costs about 40 seconds per query on this CPU.

### 11c. Retrieval evaluation

`uv run python -m campus_rag.evaluate retrieval` reports recall@1/3/5/10 and MRR@10 for each mode,
overall and by question type, plus latency and HNSW recall against an exact scan. It writes raw
results to the ignored `eval-results/` directory and, with `--write-report`, the committed summary
`docs/results/retrieval.md`. `uv run python -m campus_rag.retrieve "<query>"` prints ranked results
for manual inspection.

## 12. M7-M8 answers and answer evaluation

### 12a. Models

Measured on this CPU (16 cores, no GPU) with five retrieved passages (about 1,200 prompt tokens):

| Model | Seconds per answer | Cited passages | Refused unanswerable |
|-------|--------------------|----------------|----------------------|
| `qwen2.5:3b-instruct` | 19-22 | No | Yes |
| `llama3.2:3b` | 12-27 | Invented its own format | Yes |
| **`qwen2.5:7b-instruct`** | 46-53 | Yes | Yes |

**Decision:** `qwen2.5:7b-instruct` writes answers at temperature 0. `qwen2.5:3b-instruct`
rewrites follow-up questions into standalone queries, a short task where speed matters. The judge is
`llama3.1:8b`, from a different model family so it does not grade its own family's output. All run
locally in the Compose `ollama` service, whose context length is raised to 8,192 tokens so prompts
are never silently truncated. Models are configuration (`CAMPUS_RAG_CHAT_*`).

### 12b. Grounded answers

- The prompt holds instructions, up to six numbered passages within a 3,000-token budget, ordered by
  relevance, and the question.
- The model must cite passage numbers for each claim, keep answers under 150 words, and reply with a
  fixed refusal sentence when the passages lack the answer.
- Code removes citations to passages that were not supplied. An answer left with no valid citation
  becomes a refusal, so no answer without a source reaches the user. An answer with at least one
  valid citation is kept even if a sentence lacks its own marker; M8 measures how often that
  happens.
- **Plain text, not JSON output.** Measured with the 7B model on four hand-written questions,
  plain text cited 5 of 5 sentences; a JSON schema (`answer`, `refused`) cited 5 of 7, produced
  longer answers, and added about 51 prompt tokens. Both refused the unanswerable question.
- **Follow-up rewriting** sends only the earlier user questions, not the assistant's answers. With
  answers included, the 3B model returned follow-ups unchanged; without them it resolved all four
  test cases, including leaving a new-topic question as typed. Rewrites take 1-3 seconds. A
  rewrite longer than 300 characters, or more than three times the question (minimum 100
  characters), falls back to the original question.
- `uv run python -m campus_rag.ask "<question>"` answers from the command line; `--chat` keeps
  history for follow-ups.

### 12c. Answer evaluation

`uv run python -m campus_rag.evaluate answers` answers every golden question and reports:

| Metric | Method |
|--------|--------|
| Faithfulness | Judge splits the answer into claims and marks each supported or not by the cited passages; score = supported / total. |
| Answer relevance | Judge rates 1-5 whether the answer addresses the question. |
| Citation validity | Deterministic: share of cited numbers that were supplied. |
| Correct refusal | Deterministic: unanswerable questions refused; answerable questions answered. |

Raw results go to `eval-results/`; `--write-report` writes `docs/results/answers.md`.

## 13. M9 interfaces

- `uv run python -m campus_rag.api` serves FastAPI on `127.0.0.1:8000`: `POST /search`,
  `POST /answer`, and `GET /health`, with OpenAPI docs at `/docs`. It binds to loopback only;
  hosting a public service over UW content needs UW's permission.
- `uv run python -m campus_rag.mcp_server` runs an MCP server over stdio with a `search_policies`
  tool returning ranked passages with URLs and headings, so an agent decides when to retrieve.

## 14. M11-M12 analytics and mock ticketing

- **Analytics (M11):** migration `004` adds `request_log` (time, interface, question, mode,
  latency, top retrieval scores, refused, citation count, models) and `answer_feedback` (rating +1 or
  -1, optional comment). The API logs each request and accepts `POST /feedback`.
  `uv run python -m campus_rag.analytics` reports volume, latency percentiles, refusal rate, and
  ratings. Logs stay in the local database and record no user identity.
- **Mock ticketing (M12):** migration `005` adds `mock_tickets`. A loopback mock ticketing API
  (`uv run python -m campus_rag.ticketing`, port 8001) creates and lists tickets, and the MCP server
  adds a `draft_ticket` tool. When an answer is refused, the API response suggests drafting a
  ticket; the agent or user decides whether to file it.
