# Runbook: local setup

**Goal.** Prepare a Windows workstation to develop and run campus-rag locally.

> Milestone 2 of the [MVP plan](../docs/plans/mvp.md) adds the Python project files and the
> database container definition. Until then, this runbook lists prerequisites only.

## Prerequisites

| Tool | Purpose | Check |
|------|---------|-------|
| Python 3.12+ | Application, scripts, tests | `python --version` |
| Git | Source control | `git --version` |
| Docker Desktop (or Docker Engine in WSL) | Runs PostgreSQL with pgvector | `docker --version` |
| An OpenAI-compatible API key | Embeddings and answers | Set `OPENAI_API_KEY` in your environment |

Install Docker Desktop on Windows:

```powershell
winget install --id Docker.DockerDesktop -e
```

Start Docker Desktop, then open a new terminal so `docker` is on `PATH`. Verify the engine and the
pgvector image:

```powershell
docker info --format '{{.ServerVersion}}'
docker run --rm pgvector/pgvector:pg17 postgres --version
```

## Secrets

Store API keys in environment variables or an ignored `.env` file. Never commit them.
