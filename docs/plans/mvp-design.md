# MVP design decisions

This record tracks the design decisions for the [MVP plan](mvp.md). Each decision lists the
options considered, the recommendation, and its status. Update the status when a decision is made.

| # | Decision | Choice | Status |
|---|----------|--------|--------|
| 1 | Model provider | OpenAI-compatible interface; start with GitHub Models | Decided 2026-11-03 |
| 2 | Corpus | UW Policy Directory and UW-IT Connect, under the [terms constraints](../data-sources.md#uw-terms-of-use-constraints) | Decided 2026-11-03 |
| 3 | Python tooling | uv | Decided 2026-11-03 |
| 4 | Database access | psycopg 3 with plain SQL and numbered migrations | Decided 2026-11-03 |
| 5 | Package layout | Single `campus_rag` package organized by pipeline stage | Decided 2026-11-03 |
| 6 | Test strategy | Fake-provider unit tests, marked database tests, explicit evaluation runs | Decided 2026-11-03 |
| 7 | Capability coverage additions | Add usage analytics with user feedback; consider a mock ticketing-system tool | Proposed |

## 1. Model provider

All model calls go through the OpenAI-compatible API shape, so the provider is a configuration
value (base URL, API key, model names) rather than code.

| Option | Cost | Tradeoff |
|--------|------|----------|
| **GitHub Models** | Free with a personal GitHub account | Daily rate limits can slow corpus embedding and repeated evaluation runs. |
| **OpenAI API** | Pennies to embed the corpus; well under a dollar per full evaluation run | Requires a paid account. |
| **Ollama (local)** | Free | Slower, and answer quality is lower than hosted models. |

**Recommendation:** start with GitHub Models; switch to the OpenAI API if rate limits interfere.

**Rule:** this is a personal project. It must not use employer subscriptions, endpoints, or
credentials.

## 2. Corpus

**Recommendation:** public pages from the [UW Policy Directory](https://policy.uw.edu) and
[UW-IT Connect](https://it.uw.edu) help articles. Both are relevant to university IT, public,
and useful to staff and students.

Before ingestion, confirm each site's `robots.txt` and terms of use permit automated retrieval,
then record the result in [data sources](../data-sources.md). Both sites were reviewed on
2026-11-03; the terms limit reproduction and redistribution, so content stays local and the
golden set is written in original words.

## 3. Python tooling

| Option | Tradeoff |
|--------|----------|
| **uv** | Fast, reproducible lockfile, one tool for environments, dependencies, and running commands. |
| pip + venv | Universally familiar; no lockfile without extra tools. |

**Recommendation:** uv.

## 4. Database access

| Option | Tradeoff |
|--------|----------|
| **psycopg 3 + plain SQL** | Retrieval queries are visible and reviewable; fewer layers. |
| SQLAlchemy ORM | Portable models; hides the vector and full-text SQL that this project exists to show. |

**Recommendation:** psycopg 3 with the `pgvector` Python adapter, plain SQL, and numbered `.sql`
migration files applied in order.

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
Several are marked **Background** because the MVP does not exercise them. These additions would
cover the most valuable gaps at low cost:

| Option | Capability covered | Cost | Recommendation |
|--------|--------------------|------|----------------|
| **Usage analytics and feedback** | Monitoring performance, usage analytics, user feedback loops | Small: log each request's latency, retrieval scores, and outcome; accept a thumbs-up/down rating; report summaries | Add as a milestone after M9 |
| **Mock ticketing tool** | Enterprise-system integration, workflow automation | Medium: an MCP tool that drafts a help-desk ticket in a local mock ticketing API when the corpus cannot answer | Optional stretch goal |
| Multi-agent orchestration (CrewAI or A2A) | Agent orchestration, agent-to-agent coordination | Large; adds frameworks without improving answer quality | Skip for MVP |
| Fine-tuning | Model customization | Large; retrieval already supplies the domain knowledge | Skip |
| No-code builder | Tools for non-technical users | Large; outside this project's purpose | Skip |
