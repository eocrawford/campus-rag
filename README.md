# campus-rag

campus-rag answers questions about public university policy documents with cited sources. It
retrieves relevant passages, grounds an LLM answer in those passages, and declines to answer when
the sources do not support one.

> **Status: early development.** The full question-answering pipeline works locally: 247 UW
> Policy Directory documents are split into 1,978 chunks, embedded with Ollama, and searched with
> hybrid keyword (BM25) and vector retrieval; a local LLM answers with validated citations or
> declines. An HTTP API, an MCP server, usage analytics, and a mock ticketing service work
> locally; the evaluation write-up is in progress. See [the MVP plan](docs/plans/mvp.md).

![System overview: offline indexing fetches, chunks, and embeds policy pages into PostgreSQL with pgvector; the CLI, HTTP API, and MCP server answer or search through hybrid search and local Ollama models, with citation validation, a mock ticketing service, usage logs, and an evaluation harness.](docs/images/system-overview.png)

Offline indexing fills the PostgreSQL index that the CLI, HTTP API, and MCP server search, and a
local LLM writes the cited answers; see [architecture](docs/architecture.md#components) for the
legend and a step-by-step diagram of answering a question.

## Why

University staff, faculty, and students spend time searching scattered policy pages and help
articles. A grounded assistant can answer in seconds and link to the exact source, so readers can
verify the answer instead of trusting the model.

## How it works

Ingestion fetches and chunks public policy pages, embeds each chunk locally, and stores text and
vectors in PostgreSQL with pgvector. A question is rewritten when it follows up on an earlier one,
searched with BM25 keyword and vector search fused by reciprocal rank fusion, and answered by a
local LLM that must cite the passages it uses; code removes invalid citations and returns a fixed
refusal when nothing supports an answer.

| Layer | Responsibility |
|-------|----------------|
| **Ingestion** | Fetch allowed public pages, extract text, chunk with overlap, embed locally, record source metadata. |
| **Index** | Store chunks, embeddings, and metadata in PostgreSQL with pgvector and full-text search. |
| **Retrieval** | Combine BM25 keyword and vector results with reciprocal rank fusion (RRF), with optional source and URL filters. |
| **Generation** | Answer only from retrieved passages, cite each claim, and decline when evidence is missing. |
| **Evaluation** | Measure retrieval recall and answer faithfulness against a golden question set. |
| **API and tools** | Expose search and answer through a FastAPI service and an MCP server with `search_policies` and `draft_ticket` tools. |
| **Analytics** | Log requests and answer ratings locally and summarize them in a usage report. |

See [architecture](docs/architecture.md) for the diagrams, design details, and tradeoffs.

## Quick start

Prerequisites: Windows, Git, [uv](https://docs.astral.sh/uv/), Docker Desktop, and about 10 GB of
free disk space for Docker. From the repository root:

```powershell
.\scripts\setup-local-environment.ps1
```

The script installs Python and dependencies, starts PostgreSQL with pgvector and the Ollama
embedding server in Docker, pulls the `nomic-embed-text` model, applies migrations, and runs the
tests. See [local environment setup](runbooks/local-environment-setup.md) for options and
troubleshooting.

Then build the corpus and its embeddings (about 15 to 20 minutes on the first run):

```powershell
uv run python -m campus_rag.ingest
```

See [corpus ingestion](runbooks/ingest-corpus.md) for trial runs and reruns.

## Repository layout

| Path | Contents |
|------|----------|
| `src/` | Application source code (`campus_rag` package). See [`src/README.md`](src/README.md). |
| `tests/` | Unit tests and database tests. |
| `config/` | Corpus selection rules (URLs and filters only; fetched content is never committed). |
| `scripts/` | Repository entry-point scripts. See [`scripts/README.md`](scripts/README.md). |
| `docs/` | Architecture, evaluation, data-source policy, plans, and contributor guidance. |
| `runbooks/` | Repeatable operational procedures. |
| `compose.yaml` | Local PostgreSQL and Ollama services. |

## Documentation

- [Documentation index](docs/README.md): architecture, evaluation, data sources, glossary, and plans
- [Runbooks](runbooks/AGENTS.md)
- [Contributor and agent guide](AGENTS.md)

## License

[MIT](LICENSE)