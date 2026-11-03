# Runbook: local environment setup

**Goal.** Set up and verify a Windows workstation for campus-rag development with one command. The
command installs the Python environment, starts PostgreSQL with pgvector and the Ollama model server
in Docker, pulls and checks the embedding and chat models, applies database migrations, and runs the
tests. It either succeeds or stops at the first failed step and prints the command that fixes it.

## Prerequisites

| Requirement | Install | Check |
|-------------|---------|-------|
| Windows 10 or 11 | | `winver` |
| Git | `winget install --id Git.Git -e` | `git --version` |
| uv (installs Python 3.12 for you) | `winget install --id astral-sh.uv -e` | `uv --version` |
| Docker Desktop with the WSL 2 backend | `winget install --id Docker.DockerDesktop -e` | `docker info` |
| About 25 GB of free disk space for Docker | | Docker Desktop **Settings > Resources** |
| At least 12 GB of memory for Docker | | `docker info --format "{{.MemTotal}}"` |

Open a new terminal after installing a tool so its `PATH` change takes effect. Start Docker Desktop
once after installing it and accept its terms. The script needs no administrator rights and installs
nothing itself.

The first run downloads the PostgreSQL image (about 0.6 GB on disk), the Ollama image (about 8.5 GB
on disk), and four models (about 12 GB):

| Model | Setting | Size |
|-------|---------|------|
| `nomic-embed-text` | `CAMPUS_RAG_EMBEDDING_MODEL` | 275 MB |
| `qwen2.5:7b-instruct` | `CAMPUS_RAG_CHAT_ANSWER_MODEL` | 4.7 GB |
| `qwen2.5:3b-instruct` | `CAMPUS_RAG_CHAT_REWRITE_MODEL` | 1.9 GB |
| `llama3.1:8b` | `CAMPUS_RAG_CHAT_JUDGE_MODEL` | 4.9 GB |

Later runs reuse them. Loaded chat models use about 2.5-6 GB of memory each and stay loaded for 30
minutes after their last use. Use `-SkipChatModels` when you only need ingestion; it skips the
11.5 GB of chat models.

## Run

From the repository root:

```powershell
.\scripts\setup-local-environment.ps1
```

The script works from any directory, in Windows PowerShell 5.1 or PowerShell 7. The first run takes
15-45 minutes, mostly downloads, depending on your connection. Later runs take about a minute,
including up to 40 seconds to load the answer model for the chat check, and change nothing.

If Windows PowerShell reports that running scripts is disabled, allow local scripts once:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

## What the script does

Each step prints a numbered header and ends with `OK` or `FAILED`. On failure, the exit code is the
step number.

1. **Prerequisites.** Checks for `git`, `uv`, `docker`, and `docker compose`. If `docker` is not on
   `PATH` but Docker Desktop is installed, the script uses
   `C:\Program Files\Docker\Docker\resources\bin\docker.exe` for the current session.
2. **Docker engine.** Checks that the Docker engine responds and runs Linux containers. If it does
   not respond, the script starts Docker Desktop and waits for the engine.
3. **Environment file.** Creates `.env` from `.env.example` when `.env` is missing. An existing
   `.env` is never changed; when it lacks settings that `.env.example` defines, the script lists
   them and their defaults apply. Reads the database port, the Ollama port, and the embedding
   settings. Environment variables set in the terminal override `.env`, matching Docker Compose and
   the application.
4. **Python environment.** Runs `uv sync --locked` to install Python 3.12 and the locked
   dependencies into `.venv`. With `-PackageIndexUrl`, installs the same locked packages from a
   mirror instead (see [Restricted networks](#restricted-networks)).
5. **Database container.** Checks that the database port is free, then runs
   `docker compose up -d --wait db` to start PostgreSQL 17 with pgvector on `127.0.0.1:5433` and
   wait until it is healthy.
6. **Model server.** Checks that the Ollama port is free, then runs
   `docker compose up -d --wait ollama` to start Ollama on `127.0.0.1:11434`. Pulls each
   configured model that `ollama list` does not show (embedding, answer, rewrite, and judge; a
   model named by several settings is pulled once), printing progress. Then embeds one sample text
   and checks that the vector has `CAMPUS_RAG_EMBEDDING_DIMENSIONS` values, and asks the answer
   model for a five-token reply to prove the chat endpoint works. When a base URL points to a
   hosted provider instead of this machine, the script pulls nothing for it and only checks the
   provider.
7. **Migrations.** Runs `uv run python -m campus_rag.index.migrate` to apply pending migrations.
8. **Verification.** Runs `uv run pytest` with `CAMPUS_RAG_REQUIRE_DB=1`, so database tests fail
   instead of skipping when the database is unreachable. Tests use a temporary database and leave
   the main `campus_rag` database unchanged. Tests use a fake embedder and need no model server.

## Options

| Option | Effect |
|--------|--------|
| `-SkipTests` | Skips step 8. |
| `-SkipModelServer` | Skips step 6. Commands that embed text or call chat models fail until you rerun without it. |
| `-SkipChatModels` | Skips pulling and checking the chat models in step 6; the embedding model is still pulled and checked. The `ask`, `evaluate`, and `api` commands fail with exit code 7 until you rerun without it. |
| `-ResetDatabase` | Deletes the database container and its data volume, then creates a fresh database. All local database data is lost. Pulled models are kept. |
| `-DockerStartTimeoutSeconds <n>` | Seconds to wait for the Docker engine after starting Docker Desktop. Default: 180. |
| `-PackageIndexUrl <url>` | Installs the locked Python packages from a package mirror. See [Restricted networks](#restricted-networks). |

Full help: `Get-Help .\scripts\setup-local-environment.ps1 -Full`.

## Restricted networks

Some networks block `files.pythonhosted.org`, where PyPI serves package files. On those networks,
step 4 fails with:

```text
FAILED at step 4 (Python environment): cannot download packages from files.pythonhosted.org (this network blocks it, or the machine is offline).
```

Rerun with a PyPI mirror:

```powershell
.\scripts\setup-local-environment.ps1 -PackageIndexUrl <your package mirror URL>
```

When pip on your machine is configured with a mirror (`pip config list` shows `index-url`), the
failure message includes that URL in its `Fix:` line.

With `-PackageIndexUrl`, step 4 installs exactly the versions pinned in `uv.lock` from the mirror and
checks every file against the hashes in `uv.lock`, so the mirror must serve the original PyPI files.
It then runs `uv sync --locked --offline` to confirm that the environment matches `uv.lock`. `uv.lock`
stays unchanged and keeps its PyPI URLs. Credentials in the mirror URL are never printed.

After setup, `uv run` commands need no network access, because the environment already matches
`uv.lock`. Rerun the script with `-PackageIndexUrl` after `uv.lock` changes.

## Verify

A successful run ends with a summary like this:

```text
Setup complete in 65.5s.
  Database:   127.0.0.1:5433/campus_rag (from CAMPUS_RAG_DB_* settings)
  Embeddings: http://127.0.0.1:11434/v1, model nomic-embed-text, 768 dimensions
  Chat:       http://127.0.0.1:11434/v1, answer qwen2.5:7b-instruct, rewrite qwen2.5:3b-instruct, judge llama3.1:8b
  Python:     Python 3.12.x
  Tests:      passed
  Stop:       docker compose stop db ollama
  Reset DB:   .\scripts\setup-local-environment.ps1 -ResetDatabase   (deletes database data; keeps models)
  Runbook:    runbooks\local-environment-setup.md
```

Step 6 prints `Embedding check passed: nomic-embed-text returned 768 dimensions in <n>s.` and
`Chat check passed: qwen2.5:7b-instruct replied "OK" in <n>s.` The exit code is `0`. Check it with
`$LASTEXITCODE`.

## Troubleshooting

Each failure prints `FAILED at step N (<step>): <reason>` and a `Fix:` line. Common failures:

| Message starts with | Cause | Fix |
|---------------------|-------|-----|
| `FAILED at step 1 (Prerequisites): git is not installed` | Git is missing or not on `PATH`. | `winget install --id Git.Git -e`, then open a new terminal. |
| `FAILED at step 1 (Prerequisites): uv is not installed` | uv is missing or not on `PATH`. | `winget install --id astral-sh.uv -e`, then open a new terminal. |
| `FAILED at step 1 (Prerequisites): Docker is not installed` | Docker Desktop is missing. | `winget install --id Docker.DockerDesktop -e`, start Docker Desktop, then open a new terminal. |
| `FAILED at step 1 (Prerequisites): 'docker compose version' failed` | Docker Compose v2 is missing or broken. | `winget upgrade --id Docker.DockerDesktop -e` |
| `FAILED at step 2 (Docker engine): the Docker engine did not respond` | Docker Desktop is still starting, is waiting for you to accept its terms, or WSL 2 is not working. | Start Docker Desktop and wait for "Engine running". Check WSL 2 with `wsl --status`. Rerun with a longer wait: `-DockerStartTimeoutSeconds 300`. |
| `... DOCKER_HOST is set to ...` | `DOCKER_HOST` points the Docker CLI at an unreachable engine. | `Remove-Item Env:DOCKER_HOST`, then rerun. |
| `FAILED at step 2 (Docker engine): Docker is running windows containers` | Docker Desktop is in Windows containers mode. | Right-click the Docker tray icon and choose **Switch to Linux containers**. |
| `FAILED at step 3 (Environment file): CAMPUS_RAG_DB_PORT is` or `CAMPUS_RAG_OLLAMA_PORT is` | The port value is not a number from 1 to 65535. | Set the port in `.env` (defaults: `5433` and `11434`) and remove any environment variable with the same name. |
| `FAILED at step 3 (Environment file): CAMPUS_RAG_EMBEDDING_BASE_URL is` | The base URL is not an `http` or `https` URL. | Set `CAMPUS_RAG_EMBEDDING_BASE_URL=http://127.0.0.1:11434/v1` in `.env`. |
| `FAILED at step 3 (Environment file): CAMPUS_RAG_EMBEDDING_DIMENSIONS is` | The dimensions value is not a positive whole number. | Set `CAMPUS_RAG_EMBEDDING_DIMENSIONS=768` in `.env`. |
| `FAILED at step 3 (Environment file): CAMPUS_RAG_CHAT_BASE_URL is` | The chat base URL is not an `http` or `https` URL. | Set `CAMPUS_RAG_CHAT_BASE_URL=http://127.0.0.1:11434/v1` in `.env`. |
| `FAILED at step 3 (Environment file): CAMPUS_RAG_CHAT_TIMEOUT_SECONDS is` | The timeout is not a positive number. | Set `CAMPUS_RAG_CHAT_TIMEOUT_SECONDS=300` in `.env`. |
| `FAILED at step 4 (Python environment): uv.lock is out of date` | `pyproject.toml` changed without updating `uv.lock`. | `uv lock` on a network that can reach PyPI, rerun, and commit the updated `uv.lock`. |
| `FAILED at step 4 (Python environment): cannot download packages from files.pythonhosted.org` | The network blocks PyPI's file host, or the machine is offline. | Rerun with `-PackageIndexUrl <your package mirror URL>`. See [Restricted networks](#restricted-networks). |
| `FAILED at step 4 (Python environment): the mirror served files whose hashes differ from uv.lock.` | The mirror serves modified or rebuilt files. | Use a mirror that serves the original PyPI files. |
| `FAILED at step 4 (Python environment): cannot download packages from the mirror` | The mirror URL is wrong, unreachable, or needs credentials. | Check the URL with `pip config list` or your mirror's documentation, then rerun. |
| `FAILED at step 4 (Python environment): uv sync --locked failed` | uv could not install Python or packages for another reason. | Read the uv error above the message, then rerun. Reproduce with `uv sync --locked`. |
| `FAILED at step 5 (Database container): port 5433 is already in use by` | Another program, such as a local PostgreSQL, uses the port. | Stop that program, or set `CAMPUS_RAG_DB_PORT=5434` in `.env` and rerun. |
| `FAILED at step 5 (Database container): the database container started but failed its health check` | PostgreSQL could not start, often because of a damaged data volume. | `docker compose logs db`. If the data is disposable, rerun with `-ResetDatabase`. |
| `FAILED at step 5 (Database container): docker volume rm` | During `-ResetDatabase`, another container still uses the database volume. | Find it with `docker ps -a --filter volume=campus-rag_campus-rag-db`, remove it, then rerun. |
| `FAILED at step 6 (Model server): port 11434 is already in use by` | Another program, often a native Ollama install, uses the port. | Stop that program, or set `CAMPUS_RAG_OLLAMA_PORT=11436`, `CAMPUS_RAG_EMBEDDING_BASE_URL=http://127.0.0.1:11436/v1`, and `CAMPUS_RAG_CHAT_BASE_URL=http://127.0.0.1:11436/v1` in `.env`, then rerun. |
| `FAILED at step 6 (Model server): the model server container started but failed its health check` | Ollama could not start. | `docker compose logs ollama`, then rerun. |
| `FAILED at step 6 (Model server): 'ollama list' failed` | The Ollama container is not responding. | `docker compose logs ollama`. Restart it with `docker compose restart ollama`, then rerun. |
| `FAILED at step 6 (Model server): model <name> was not found in the Ollama library` | The setting named in the `Fix:` line names a model that Ollama does not publish. | Correct that setting in `.env` (defaults are in `.env.example`), then rerun. |
| `FAILED at step 6 (Model server): could not pull model` | The model download failed, often because the network blocks `registry.ollama.ai` or Docker ran out of disk space. | Check network access and free disk space, then rerun. To pull by hand: `docker compose exec ollama ollama pull <name>`. |
| `FAILED at step 6 (Model server): cannot reach the embeddings endpoint` | The model server is not running, or `CAMPUS_RAG_EMBEDDING_BASE_URL` uses a different port than `CAMPUS_RAG_OLLAMA_PORT`. | `docker compose ps ollama` should show `healthy`. Make the two ports match in `.env`. |
| `FAILED at step 6 (Model server): the embeddings endpoint ... returned HTTP` | The server rejected the request: the model is missing, or a hosted provider rejected the API key. | Check `CAMPUS_RAG_EMBEDDING_MODEL` and `CAMPUS_RAG_EMBEDDING_API_KEY` in `.env`. |
| `FAILED at step 6 (Model server): model <name> returns <N>-dimensional vectors, but CAMPUS_RAG_EMBEDDING_DIMENSIONS is <M>.` | The configured dimensions do not match the model. | Set `CAMPUS_RAG_EMBEDDING_DIMENSIONS=<N>` in `.env` and remove any environment variable with the same name, or choose a model that returns `<M>` dimensions. |
| `FAILED at step 6 (Model server): cannot reach the chat endpoint` | The model server is not running, or `CAMPUS_RAG_CHAT_BASE_URL` uses a different port than `CAMPUS_RAG_OLLAMA_PORT`. | `docker compose ps ollama` should show `healthy`. Make the two ports match in `.env`. |
| `FAILED at step 6 (Model server): the chat endpoint ... returned HTTP` | The server rejected the request: the answer model is missing, or a hosted provider rejected the API key. | Check `CAMPUS_RAG_CHAT_ANSWER_MODEL` and `CAMPUS_RAG_CHAT_API_KEY` in `.env`. |
| `FAILED at step 6 (Model server): the chat endpoint ... did not respond within` | Loading or running the answer model took longer than `CAMPUS_RAG_CHAT_TIMEOUT_SECONDS`. | Raise `CAMPUS_RAG_CHAT_TIMEOUT_SECONDS` in `.env`, or give Docker more CPU and memory, then rerun. |
| `FAILED at step 6 (Model server): the chat endpoint ... returned no message` | The endpoint is not an OpenAI-compatible chat API. | Check `CAMPUS_RAG_CHAT_BASE_URL`; it should end in `/v1`. |
| `FAILED at step 6 (Model server): the ... endpoint ... reports that the model needs more memory than Docker has` | Docker has too little memory to load the model. | Give Docker more memory: Docker Desktop **Settings > Resources**, or `memory=` in `%UserProfile%\.wslconfig` for the WSL 2 backend, then run `wsl --shutdown` and restart Docker Desktop. |
| `FAILED at step 7 (Migrations): cannot connect to the database` | The database is not running, or `.env` points at a different host, port, or credentials. | `docker compose ps db` should show `healthy`. Compare the `CAMPUS_RAG_DB_*` values in `.env` with the running container. |
| `FAILED at step 7 (Migrations): a migration failed` | A migration has an error, or an applied migration file was changed or removed. | Fix the migration. If the local database is out of sync, rerun with `-ResetDatabase`. |
| `FAILED at step 8 (Verification): tests failed` | One or more tests failed. | Read the pytest output. Rerun with `$env:CAMPUS_RAG_REQUIRE_DB='1'; uv run pytest`. |
| `running scripts is disabled on this system` | The PowerShell execution policy blocks local scripts. | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` |

## Everyday commands

Run these from the repository root.

| Task | Command |
|------|---------|
| Start the database and model server | `docker compose up -d --wait db ollama` |
| Stop them (keeps data and models) | `docker compose stop db ollama` |
| List pulled models | `docker compose exec ollama ollama list` |
| Show loaded models and their memory | `docker compose exec ollama ollama ps` |
| Pull a model by hand | `docker compose exec ollama ollama pull <name>` |
| Run the tests | `uv run pytest` |
| Lint | `uv run ruff check src tests` |
| Apply migrations | `uv run python -m campus_rag.index.migrate` |
| Reset the local database (deletes data, keeps models) | `.\scripts\setup-local-environment.ps1 -ResetDatabase` |
| Delete pulled models | `docker compose rm --stop --force ollama`, then `docker volume rm campus-rag_campus-rag-ollama` |

`uv run pytest` skips database tests when the database is not running. Set
`CAMPUS_RAG_REQUIRE_DB=1` to make them fail instead.

`docker compose down --volumes` deletes both the database and the pulled models. Use
`-ResetDatabase` to delete only the database.

## Secrets

`.env` holds local settings and is ignored by Git. The database password in `.env.example` is for the
local container only. The local Ollama server needs no API key. To use a hosted provider, set
`CAMPUS_RAG_EMBEDDING_API_KEY` or `CAMPUS_RAG_CHAT_API_KEY` in `.env` or an environment variable;
the script never prints them. Never commit API keys or a `.env` file.
