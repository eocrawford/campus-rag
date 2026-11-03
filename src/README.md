# src

Application source code: the `campus_rag` package, managed with uv from the repository root. See
[architecture](../docs/architecture.md) for how the parts fit together.

| Module | Responsibility |
|--------|----------------|
| `campus_rag/config.py` | Settings from environment variables and the repo-root `.env`: database URL and embedding settings. |
| `campus_rag/index/migrate.py` | Applies the numbered SQL files in `campus_rag/index/migrations/`: `uv run python -m campus_rag.index.migrate`. |
| `campus_rag/index/store.py` | Writes documents, chunks, and embeddings to PostgreSQL idempotently, and finds chunks that need embedding. |
| `campus_rag/index/embed.py` | Embedding client for an OpenAI-compatible `/embeddings` API (Ollama by default), with task prefixes, batching, and retries. |
| `campus_rag/ingest/__main__.py` | The ingest command: `uv run python -m campus_rag.ingest`. See [corpus ingestion](../runbooks/ingest-corpus.md). |
| `campus_rag/ingest/pipeline.py` | Runs the stages for each source, and the embedding stage, and builds the run summary. |
| `campus_rag/ingest/sources.py` | Loads `config/sources.toml` and selects page URLs from sitemaps. |
| `campus_rag/ingest/fetch.py` | Polite fetching: `robots.txt`, rate limiting, retries, and the `data/raw/` cache. |
| `campus_rag/ingest/extract.py` | Turns a policy page's `<main>` element into headed sections of text. |
| `campus_rag/ingest/chunk.py` | Splits documents into heading-prefixed chunks measured in `cl100k_base` tokens. |
| `campus_rag/ingest/models.py` | Data types shared by the ingestion stages. |
| `campus_rag/retrieve/search.py` | `PolicySearcher`: keyword, vector, and hybrid search behind the `Searcher` protocol. |
| `campus_rag/retrieve/keyword.py`, `vector.py`, `fusion.py`, `sql.py` | BM25 keyword search over `search_tsv`, HNSW and exact vector search, reciprocal rank fusion, and the filter SQL they share. |
| `campus_rag/retrieve/__main__.py` | The search command: `uv run python -m campus_rag.retrieve "<query>"`. See [evaluate retrieval](../runbooks/evaluate-retrieval.md). |
| `campus_rag/evaluate/retrieval.py` | Retrieval metrics (recall@k, MRR, latency, HNSW recall) and the `retrieval` evaluation command. |
| `campus_rag/evaluate/__main__.py` | The evaluation command line: `uv run python -m campus_rag.evaluate <command>`; commands register in one list. |
| `campus_rag/services.py` | What the API and MCP server share: a per-request database scope, model clients, and health checks. |
| `campus_rag/api/` | The FastAPI app (`app.py`), its request and response models (`schemas.py`), and the server command: `uv run python -m campus_rag.api`. See [run the API and MCP server](../runbooks/run-api-and-mcp.md). |
| `campus_rag/mcp_server.py` | The MCP server with the `search_policies` and `draft_ticket` tools: `uv run python -m campus_rag.mcp_server`. |
| `campus_rag/ticketing/` | The mock ticketing API (`app.py`, `store.py`, `schemas.py`), its HTTP client for the MCP tool (`client.py`), and the server command: `uv run python -m campus_rag.ticketing`. |
| `campus_rag/loopback.py` | The loopback-only bind rule the API and ticketing servers share. |
| `campus_rag/analytics/` | Request log and ratings (`log.py`), the usage report (`report.py`), and its command: `uv run python -m campus_rag.analytics`. |

Tests live in `tests/` at the repository root. Tests marked `db` need the local database; unit
tests use fakes and need no model server.