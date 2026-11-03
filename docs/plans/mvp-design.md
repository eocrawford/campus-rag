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
| 7 | Capability coverage additions | Add usage analytics with user feedback (M11); mock ticketing-system tool as an optional stretch (M12) | Decided 2026-11-03 |
| 8 | Installs on restricted networks | Opt-in `-PackageIndexUrl` mirror install with lock hashes enforced; `uv.lock` stays canonical | Proposed (implemented in M2 setup script) |

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

**Recommendation:** opt-in mirror install. Without the parameter, the setup script fails at step 4
and prints the rerun command with the mirror detected from `pip config list`. The build backend
(hatchling) used for the editable install comes from the mirror without a hash check because it
is not pinned in `uv.lock`.
