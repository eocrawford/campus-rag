# campus-rag

campus-rag answers questions about public university policy and IT documentation with cited sources.
It retrieves relevant passages, grounds an LLM answer in those passages, and declines to answer when
the sources do not support one.

> **Status: early development.** The local environment (Python, PostgreSQL with pgvector, and
> database migrations) is in place. Retrieval and answering features below describe the planned
> design; see [the MVP plan](docs/plans/mvp.md) for progress.

## Why

University staff, faculty, and students spend time searching scattered policy pages and help
articles. A grounded assistant can answer in seconds and link to the exact source, so readers can
verify the answer instead of trusting the model.

## How it works

```text
Public documents -> chunk -> embed -> PostgreSQL + pgvector
                                            |
Question -> rewrite -> hybrid search (keyword + vector) -> rerank -> grounded answer with citations
```

| Layer | Responsibility |
|-------|----------------|
| **Ingestion** | Fetch allowed public pages, extract text, chunk with overlap, record source metadata. |
| **Index** | Store chunks, embeddings, and metadata in PostgreSQL with pgvector and full-text search. |
| **Retrieval** | Combine keyword and vector results with reciprocal rank fusion (RRF), then filter by metadata. |
| **Generation** | Answer only from retrieved passages, cite each claim, and decline when evidence is missing. |
| **Evaluation** | Measure retrieval recall and answer faithfulness against a golden question set. |
| **API and tools** | Expose search and answer through a FastAPI service and a Model Context Protocol (MCP) tool. |

See [architecture](docs/architecture.md) for design details and tradeoffs.

## Quick start

Prerequisites: Windows, Git, [uv](https://docs.astral.sh/uv/), and Docker Desktop. Then, from the
repository root:

```powershell
.\scripts\setup-local-environment.ps1
```

The script installs Python and dependencies, starts PostgreSQL with pgvector, applies migrations,
and runs the tests. See [local environment setup](runbooks/local-environment-setup.md) for options
and troubleshooting.

## Repository layout

| Path | Contents |
|------|----------|
| `src/` | Application source code (`campus_rag` package). |
| `tests/` | Unit tests and database tests. |
| `scripts/` | Repository entry-point scripts for ingestion, evaluation, and local setup. |
| `docs/` | Architecture, evaluation, data-source policy, and contributor guidance. |
| `runbooks/` | Repeatable operational procedures. |

## Documentation

- [Documentation index](docs/README.md)
- [Architecture](docs/architecture.md)
- [Evaluation](docs/evaluation.md)
- [Data sources and usage policy](docs/data-sources.md)
- [Glossary](docs/glossary.md) - RAG and AI-agent terms, the problem each solves, and where it appears here
- [Runbooks](runbooks/AGENTS.md)
- [Contributor and agent guide](AGENTS.md)

## License

[MIT](LICENSE)
