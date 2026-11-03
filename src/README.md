# src

Application source code for campus-rag: ingestion, indexing, retrieval, generation, and the API.
See [architecture](../docs/architecture.md).

Current layout (a single `campus_rag` package, managed with uv from the repository root):

* `campus_rag/config.py`: settings from environment variables and the repo-root `.env`.
* `campus_rag/index/migrate.py`: applies the numbered SQL files in `campus_rag/index/migrations/`
  with `uv run python -m campus_rag.index.migrate`.

Tests live in `tests/` at the repository root. Tests marked `db` need the local database.
