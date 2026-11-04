# Walkthrough

This walkthrough takes you through campus-rag twice: first as a running system, from a clean
machine to an agent filing a help-desk ticket, then as code, in the order a question flows through
it. Use the first part to demo the system and the second to explain how it works.

## Part 1: Run the whole system

[Run the demo](../runbooks/run-demo.md) is the step-by-step guide: setting up a clean machine,
running the demo script, running every step by hand, a 10-minute interview narrative, and
troubleshooting. In short:

| Step | Command | Time |
|------|---------|------|
| 1. Install prerequisites | Git, uv, and Docker Desktop with `winget`; see [step 2](../runbooks/run-demo.md#step-2-install-the-prerequisites-about-10-minutes) | About 10 minutes |
| 2. Clone | `git clone https://github.com/eocrawford/campus-rag.git C:\src\campus-rag` | Under a minute |
| 3. Set up | `.\scripts\setup-local-environment.ps1` | 15-45 minutes the first time |
| 4. Ingest | `uv run python -m campus_rag.ingest` | About 15 minutes |
| 5. Demo | `.\scripts\demo.ps1` (`-Pause` when presenting, `-SkipSlow` for a 1- to 3-minute tour) | About 8 minutes |

[`scripts/demo.ps1`](../scripts/demo.ps1) runs these sections in order, each with a one-line
talking point and the exact command it runs:

| Section | What you see |
|---------|--------------|
| Setup and preflight | The setup script's verified steps, then checks of Docker, the database, the corpus, the models, and the ports. |
| Ingest | A five-page dry run of select, fetch, extract, and chunk, then a summary of the stored corpus. |
| Search | One paraphrased question in keyword, vector, and hybrid search, then a policy number. |
| Answers | A cited answer, a refusal, and a follow-up rewritten into a standalone query. |
| Api | `/health`, `/search`, `/answer`, and `/feedback` over HTTP, then the usage report. |
| Agent | An MCP client lists the tools, searches with `search_policies`, and files a ticket in the IT service management (ITSM) stand-in with `draft_ticket`. |
| Evaluation | A live retrieval evaluation, the recorded answer and embedding results, and one golden question answered and judged live. |

Answers take about a minute each on a CPU. A 7B model can misread a passage it cites, which is why
the demo questions were checked against their cited passages and why the evaluation measures
faithfulness.

## Part 2: Read the code

Follow a question through the system. Links point to the exact lines at the commit that holds the
final code. The [architecture diagrams](architecture.md#components) show the same flow.

### Configuration

| Stop | Code | What to notice |
|------|------|----------------|
| Settings | [`EmbeddingSettings`](https://github.com/eocrawford/campus-rag/blob/0bc8b29d9829fe501e03785e2881de21ecc64c63/src/campus_rag/config.py#L12-L23), [`ChatSettings`](https://github.com/eocrawford/campus-rag/blob/0bc8b29d9829fe501e03785e2881de21ecc64c63/src/campus_rag/config.py#L26-L39) | Every model and endpoint is configuration behind an OpenAI-compatible API, so switching providers changes settings, not code. |
| Sources | [`config/sources.toml`](https://github.com/eocrawford/campus-rag/blob/0bc8b29d9829fe501e03785e2881de21ecc64c63/config/sources.toml#L4-L15) | The corpus is defined by committed rules; fetched content is never committed. |

### Offline: build the index

| Stop | Code | What to notice |
|------|------|----------------|
| Select | [`select_urls`](https://github.com/eocrawford/campus-rag/blob/0bc8b29d9829fe501e03785e2881de21ecc64c63/src/campus_rag/ingest/sources.py#L127-L159) | A sitemap that cannot be read stops the run, so a partial selection never deletes stored documents. |
| Fetch | [`PoliteFetcher`](https://github.com/eocrawford/campus-rag/blob/0bc8b29d9829fe501e03785e2881de21ecc64c63/src/campus_rag/ingest/fetch.py#L29-L250) | robots.txt, one request per second, retries with backoff, a local cache, and conditional requests; HTTP errors become results, not exceptions. |
| Extract | [`extract.py`](https://github.com/eocrawford/campus-rag/blob/0bc8b29d9829fe501e03785e2881de21ecc64c63/src/campus_rag/ingest/extract.py) | Reads `<main>`, keeps the heading hierarchy, and drops the in-page table of contents and navigation-only blocks. |
| Chunk | [`chunk_document`](https://github.com/eocrawford/campus-rag/blob/0bc8b29d9829fe501e03785e2881de21ecc64c63/src/campus_rag/ingest/chunk.py#L38-L79), [`count_tokens`](https://github.com/eocrawford/campus-rag/blob/0bc8b29d9829fe501e03785e2881de21ecc64c63/src/campus_rag/ingest/chunk.py#L33-L35) | Heading-aware splits with a hard 800-token maximum, overlap between split pieces, and short sections merged into a neighbor. |
| Schema | [`chunk_embeddings`](https://github.com/eocrawford/campus-rag/blob/0bc8b29d9829fe501e03785e2881de21ecc64c63/src/campus_rag/index/migrations/003_embeddings_and_keyword_index.sql#L14-L23), [HNSW index](https://github.com/eocrawford/campus-rag/blob/0bc8b29d9829fe501e03785e2881de21ecc64c63/src/campus_rag/index/migrations/003_embeddings_and_keyword_index.sql#L25-L29), [`search_tsv`](https://github.com/eocrawford/campus-rag/blob/0bc8b29d9829fe501e03785e2881de21ecc64c63/src/campus_rag/index/migrations/003_embeddings_and_keyword_index.sql#L34-L47) | Embeddings are stored per model with a content hash; the HNSW index is partial per model; the full-text column weights headings above body text. |
| Embed | [`OpenAICompatibleEmbedder`](https://github.com/eocrawford/campus-rag/blob/0bc8b29d9829fe501e03785e2881de21ecc64c63/src/campus_rag/index/embed.py#L35-L169) | Passages and questions get different task prefixes, which the [embedding experiment](results/embedding-strategies.md) showed matter. Wrong-sized vectors are rejected. |

### Online: answer a question

| Stop | Code | What to notice |
|------|------|----------------|
| Rewrite | [`rewrite_query`](https://github.com/eocrawford/campus-rag/blob/0bc8b29d9829fe501e03785e2881de21ecc64c63/src/campus_rag/generate/rewrite.py#L54-L77) | Only earlier user questions are sent, with worked examples; any failure falls back to the question as typed. |
| Keyword search | [`_BM25_SQL`](https://github.com/eocrawford/campus-rag/blob/0bc8b29d9829fe501e03785e2881de21ecc64c63/src/campus_rag/retrieve/keyword.py#L46-L83), [`keyword_search`](https://github.com/eocrawford/campus-rag/blob/0bc8b29d9829fe501e03785e2881de21ecc64c63/src/campus_rag/retrieve/keyword.py#L100-L131) | BM25 computed in SQL over the weighted tsvector; it replaced `ts_rank_cd`, which lacks inverse document frequency. |
| Vector search | [`vector_search`](https://github.com/eocrawford/campus-rag/blob/0bc8b29d9829fe501e03785e2881de21ecc64c63/src/campus_rag/retrieve/vector.py#L39-L65) | The query shape matches the partial HNSW index, with `ef_search` set per query and iterative scans for filtered searches. |
| Fuse | [`reciprocal_rank_fusion`](https://github.com/eocrawford/campus-rag/blob/0bc8b29d9829fe501e03785e2881de21ecc64c63/src/campus_rag/retrieve/fusion.py#L9-L55), [`PolicySearcher.search`](https://github.com/eocrawford/campus-rag/blob/0bc8b29d9829fe501e03785e2881de21ecc64c63/src/campus_rag/retrieve/search.py#L64-L68) | Fusion uses ranks, not scores, so BM25 and cosine scales never need calibrating. Each result keeps its keyword and vector ranks. |
| Prompt | [`SYSTEM_PROMPT`](https://github.com/eocrawford/campus-rag/blob/0bc8b29d9829fe501e03785e2881de21ecc64c63/src/campus_rag/generate/prompt.py#L14-L23), [`build_prompt`](https://github.com/eocrawford/campus-rag/blob/0bc8b29d9829fe501e03785e2881de21ecc64c63/src/campus_rag/generate/prompt.py#L33-L57), [`select_passages`](https://github.com/eocrawford/campus-rag/blob/0bc8b29d9829fe501e03785e2881de21ecc64c63/src/campus_rag/generate/prompt.py#L60-L85) | At most six numbered passages within a 3,000-token budget, each with its title, heading path, and URL. |
| Generate | [`AnswerService.answer`](https://github.com/eocrawford/campus-rag/blob/0bc8b29d9829fe501e03785e2881de21ecc64c63/src/campus_rag/generate/answer.py#L45-L107), [`OpenAICompatibleChat`](https://github.com/eocrawford/campus-rag/blob/0bc8b29d9829fe501e03785e2881de21ecc64c63/src/campus_rag/generate/chat.py#L47-L204) | The whole online path in one method, with timings for each stage; no passages means a refusal without calling the model. |
| Validate | [`check_answer`](https://github.com/eocrawford/campus-rag/blob/0bc8b29d9829fe501e03785e2881de21ecc64c63/src/campus_rag/generate/citations.py#L31-L37), [`normalize_citations`](https://github.com/eocrawford/campus-rag/blob/0bc8b29d9829fe501e03785e2881de21ecc64c63/src/campus_rag/generate/citations.py#L40-L64) | Code, not the model, decides: citations to passages that were not supplied are removed, and an answer with no valid citation becomes a refusal. |

### Interfaces

| Stop | Code | What to notice |
|------|------|----------------|
| REST API | [`create_app`](https://github.com/eocrawford/campus-rag/blob/0bc8b29d9829fe501e03785e2881de21ecc64c63/src/campus_rag/api/app.py#L60-L266) | Loopback by default; unavailable models or database return 503 with the fix; refused answers include a prefilled [ticket draft](https://github.com/eocrawford/campus-rag/blob/0bc8b29d9829fe501e03785e2881de21ecc64c63/src/campus_rag/api/app.py#L273-L291). |
| MCP server | [`build_server`](https://github.com/eocrawford/campus-rag/blob/0bc8b29d9829fe501e03785e2881de21ecc64c63/src/campus_rag/mcp_server.py#L85-L210) | Two tools: [`search_policies`](https://github.com/eocrawford/campus-rag/blob/0bc8b29d9829fe501e03785e2881de21ecc64c63/src/campus_rag/mcp_server.py#L93-L157) returns citable passages, and [`draft_ticket`](https://github.com/eocrawford/campus-rag/blob/0bc8b29d9829fe501e03785e2881de21ecc64c63/src/campus_rag/mcp_server.py#L159-L208) files through the [`TicketingClient`](https://github.com/eocrawford/campus-rag/blob/0bc8b29d9829fe501e03785e2881de21ecc64c63/src/campus_rag/ticketing/client.py#L37-L110) connector only after the user agrees. |
| Analytics | [`log_request`](https://github.com/eocrawford/campus-rag/blob/0bc8b29d9829fe501e03785e2881de21ecc64c63/src/campus_rag/analytics/log.py#L25-L76), [`build_report`](https://github.com/eocrawford/campus-rag/blob/0bc8b29d9829fe501e03785e2881de21ecc64c63/src/campus_rag/analytics/report.py#L58-L120) | Logging never fails a request and stores no user identity; the report lists recent negatively rated questions as golden-set candidates. |

### Evaluation

| Stop | Code | What to notice |
|------|------|----------------|
| Golden set | [`golden_set.jsonl`](../eval/golden_set.jsonl), [`load_golden_set`](https://github.com/eocrawford/campus-rag/blob/0bc8b29d9829fe501e03785e2881de21ecc64c63/src/campus_rag/evaluate/golden.py#L54-L85) | 47 questions in original words; targets are URLs and heading prefixes, so re-chunking does not invalidate them. |
| Retrieval metrics | [`recall_at`](https://github.com/eocrawford/campus-rag/blob/0bc8b29d9829fe501e03785e2881de21ecc64c63/src/campus_rag/evaluate/retrieval.py#L108-L112), [`mean_reciprocal_rank`](https://github.com/eocrawford/campus-rag/blob/0bc8b29d9829fe501e03785e2881de21ecc64c63/src/campus_rag/evaluate/retrieval.py#L115-L119) | Deterministic and fast; the [results](results/retrieval.md) compare keyword, vector, and hybrid search. |
| Answer judge | [`Judge`](https://github.com/eocrawford/campus-rag/blob/0bc8b29d9829fe501e03785e2881de21ecc64c63/src/campus_rag/evaluate/judge.py#L272-L354) | A model from a different family, a JSON schema, and only the cited passages; [results and limits](results/answers.md). |
| Embedding experiment | [`VARIANTS`](https://github.com/eocrawford/campus-rag/blob/0bc8b29d9829fe501e03785e2881de21ecc64c63/src/campus_rag/evaluate/embeddings.py#L139-L162) | Each embedding choice as data, compared with exact search; [results](results/embedding-strategies.md). |

## Where to go next

- [Case study](case-study.md): the decisions and evidence in narrative form.
- [Design decisions](plans/mvp-design.md): every choice with its options and tradeoffs.
- [Glossary](glossary.md): RAG and agent terms, each linked to the code that implements it.
