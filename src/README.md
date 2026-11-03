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

Tests live in `tests/` at the repository root. Tests marked `db` need the local database; unit
tests use fakes and need no model server.