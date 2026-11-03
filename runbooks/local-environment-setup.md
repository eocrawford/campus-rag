# Runbook: local environment setup

**Goal.** Set up and verify a Windows workstation for campus-rag development with one command. The
command installs the Python environment, starts PostgreSQL with pgvector in Docker, applies database
migrations, and runs the tests. It either succeeds or stops at the first failed step and prints the
command that fixes it.

## Prerequisites

| Requirement | Install | Check |
|-------------|---------|-------|
| Windows 10 or 11 | | `winver` |
| Git | `winget install --id Git.Git -e` | `git --version` |
| uv (installs Python 3.12 for you) | `winget install --id astral-sh.uv -e` | `uv --version` |
| Docker Desktop with the WSL 2 backend | `winget install --id Docker.DockerDesktop -e` | `docker info` |

Open a new terminal after installing a tool so its `PATH` change takes effect. Start Docker Desktop
once after installing it and accept its terms. The script needs no administrator rights and installs
nothing itself.

## Run

From the repository root:

```powershell
.\scripts\setup-local-environment.ps1
```

The script works from any directory, in Windows PowerShell 5.1 or PowerShell 7. The first run
downloads Python, the dependencies, and the database image, so it takes a few minutes. Later runs
take seconds and change nothing.

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
   `.env` is never changed. Reads `CAMPUS_RAG_DB_PORT` (default `5433`). Environment variables set
   in the terminal override `.env`, matching Docker Compose and the application.
4. **Python environment.** Runs `uv sync --locked` to install Python 3.12 and the locked
   dependencies into `.venv`. With `-PackageIndexUrl`, installs the same locked packages from a
   mirror instead (see [Restricted networks](#restricted-networks)).
5. **Database container.** Checks that the database port is free, then runs
   `docker compose up -d --wait db` to start PostgreSQL 17 with pgvector on
   `127.0.0.1:5433` and wait until it is healthy.
6. **Migrations.** Runs `uv run python -m campus_rag.index.migrate` to apply pending migrations.
7. **Verification.** Runs `uv run pytest` with `CAMPUS_RAG_REQUIRE_DB=1`, so database tests fail
   instead of skipping when the database is unreachable. Tests use a temporary database and leave
   the main `campus_rag` database unchanged.

## Options

| Option | Effect |
|--------|--------|
| `-SkipTests` | Skips step 7. |
| `-ResetDatabase` | Deletes the local database container and its data volume (`docker compose down --volumes`), then creates a fresh database. All local database data is lost. |
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
Setup complete in 12.3s.
  Database:  127.0.0.1:5433/campus_rag (from CAMPUS_RAG_DB_* settings)
  Python:    Python 3.12.x
  Tests:     passed
  Stop DB:   docker compose stop db
  Reset DB:  .\scripts\setup-local-environment.ps1 -ResetDatabase   (deletes local data)
  Runbook:   runbooks\local-environment-setup.md
```

The exit code is `0`. Check it with `$LASTEXITCODE`.

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
| `FAILED at step 3 (Environment file): CAMPUS_RAG_DB_PORT is` | The port value is not a number from 1 to 65535. | Set `CAMPUS_RAG_DB_PORT=5433` in `.env` and remove any `CAMPUS_RAG_DB_PORT` environment variable. |
| `FAILED at step 4 (Python environment): uv.lock is out of date` | `pyproject.toml` changed without updating `uv.lock`. | `uv lock` on a network that can reach PyPI, rerun, and commit the updated `uv.lock`. |
| `FAILED at step 4 (Python environment): cannot download packages from files.pythonhosted.org (this network blocks it, or the machine is offline).` | The network blocks PyPI's file host. | Rerun with `-PackageIndexUrl <your package mirror URL>`. See [Restricted networks](#restricted-networks). |
| `FAILED at step 4 (Python environment): the mirror served files whose hashes differ from uv.lock.` | The mirror serves modified or rebuilt files. | Use a mirror that serves the original PyPI files. |
| `FAILED at step 4 (Python environment): cannot download packages from the mirror` | The mirror URL is wrong, unreachable, or needs credentials. | Check the URL with `pip config list` or your mirror's documentation, then rerun. |
| `FAILED at step 4 (Python environment): uv sync --locked failed` | uv could not install Python or packages for another reason. | Read the uv error above the message, then rerun. Reproduce with `uv sync --locked`. |
| `FAILED at step 5 (Database container): port 5433 is already in use by` | Another program, such as a local PostgreSQL, uses the port. | Stop that program, or set `CAMPUS_RAG_DB_PORT=5434` in `.env` and rerun. |
| `FAILED at step 5 (Database container): the database container started but failed its health check` | PostgreSQL could not start, often because of a damaged data volume. | `docker compose logs db`. If the data is disposable, rerun with `-ResetDatabase`. |
| `FAILED at step 6 (Migrations): cannot connect to the database` | The database is not running, or `.env` points at a different host, port, or credentials. | `docker compose ps db` should show `healthy`. Compare the `CAMPUS_RAG_DB_*` values in `.env` with the running container. |
| `FAILED at step 6 (Migrations): a migration failed` | A migration has an error, or an applied migration file was changed or removed. | Fix the migration. If the local database is out of sync, rerun with `-ResetDatabase`. |
| `FAILED at step 7 (Verification): tests failed` | One or more tests failed. | Read the pytest output. Rerun with `$env:CAMPUS_RAG_REQUIRE_DB='1'; uv run pytest`. |
| `running scripts is disabled on this system` | The PowerShell execution policy blocks local scripts. | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` |

## Everyday commands

Run these from the repository root.

| Task | Command |
|------|---------|
| Start the database | `docker compose up -d --wait db` |
| Stop the database (keeps data) | `docker compose stop db` |
| Run the tests | `uv run pytest` |
| Lint | `uv run ruff check src tests` |
| Apply migrations | `uv run python -m campus_rag.index.migrate` |
| Reset the local database (deletes data) | `.\scripts\setup-local-environment.ps1 -ResetDatabase` |

`uv run pytest` skips database tests when the database is not running. Set
`CAMPUS_RAG_REQUIRE_DB=1` to make them fail instead.

## Secrets

`.env` holds local settings and is ignored by Git. The database password in `.env.example` is for the
local container only. Never commit API keys or a `.env` file. The model API key
(`CAMPUS_RAG_LLM_API_KEY`) is needed from milestone 4; store it in `.env` or an environment variable.
