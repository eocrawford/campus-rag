---
language: python
python-version: "3.12"
setup: scripts/setup-local-environment.ps1
lint: uv run ruff check src tests
format: uv run ruff format --check src tests
test: uv run pytest
---

# campus-rag contributor guide

campus-rag is a retrieval-augmented generation (RAG) service that answers questions about public
university documents with cited sources. This guide is for agents and humans making changes.

## Getting started

* Read and follow the 'Always' and 'Never' rules in [`docs/agent-guidance/RoE.md`](docs/agent-guidance/RoE.md).
* Read [foundations](docs/agent-guidance/foundations.md) before designing a change. It defines the
  boundary between deterministic code and LLM judgment, and the project's Python conventions.
* Read [architecture](docs/architecture.md) before changing ingestion, retrieval, or generation.
* Prefer the simplest complete design. New modules and abstractions need a demonstrated
  responsibility, not a naming rule.
* Retrieval and prompt changes need evaluation evidence. Follow [evaluation](docs/evaluation.md)
  and report before/after metrics in the pull request.
* Only ingest sources permitted by [data sources and usage policy](docs/data-sources.md).
* Keep the [glossary](docs/glossary.md) current. When a change implements a glossary term, update
  its row in the same change: link to the code or measured results and set its status to
  **Implemented**.

## File locations

* Application source goes in `src/campus_rag/`. Database migrations go in
  `src/campus_rag/index/migrations/` as `NNN_snake_name.sql`; never edit a migration after it merges.
* Tests go in `tests/`. Mark tests that need the local database with `@pytest.mark.db`.
* Repository entry-point scripts go in `scripts/`.
* Documentation goes in `docs/`. Update the table of contents in [`docs/AGENTS.md`](docs/AGENTS.md)
  when adding a topic.
* Runbooks go in `runbooks/`. Update the table of contents in [`runbooks/AGENTS.md`](runbooks/AGENTS.md)
  when adding a runbook.
* Durable architecture, design, and implementation plans go in `docs/plans/`. Keep them current:
  remove obsolete plans and update outdated information.

## Secrets and data

* Never commit API keys, connection strings, or `.env` files. Read secrets from environment variables.
* Never commit downloaded corpus files or database dumps. Store them under the ignored `data/` directory.

## Validation

Set up the environment once with
[local environment setup](runbooks/local-environment-setup.md):

```powershell
.\scripts\setup-local-environment.ps1
```

Run lint and tests before publishing a change:

```powershell
uv run ruff check src tests
uv run ruff format --check src tests
uv run pytest
```

Never run `uv lock` with a package mirror configured; `uv.lock` must keep its canonical PyPI URLs.

## Commit trailer

```
Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
```
