# Runbook: run the demo

**Goal.** Set up campus-rag on a clean Windows machine and demonstrate the whole system with one
command: setup, ingestion, keyword, vector, and hybrid search, cited answers and refusals, the HTTP
API, the MCP server as an AI agent uses it, and evaluation. Every step also has a manual command, so
you can present without the script.

## Contents

1. [Set up a clean machine](#1-set-up-a-clean-machine)
2. [Run the demo](#2-run-the-demo)
3. [Run each step by hand](#3-run-each-step-by-hand)
4. [A 10-minute interview narrative](#4-a-10-minute-interview-narrative)
5. [Troubleshooting](#5-troubleshooting)
6. [Clean up demo data](#6-clean-up-demo-data)

## 1. Set up a clean machine

Plan about an hour the first time, mostly downloads and the first ingestion. Each step lists its
command, what success looks like, and the time it took on a 16-core CPU without a GPU.

### Step 1: check the hardware

| Requirement | Why | Check |
|-------------|-----|-------|
| Windows 10 or 11 | The scripts are PowerShell; Docker Desktop runs the services. | `winver` |
| About 25 GB of free disk | Docker images (about 9 GB) and models (about 12 GB). | `Get-PSDrive C` |
| 16 GB of RAM or more | Docker needs at least 12 GB to load the 7B and 8B models. | `(Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB` |
| A CPU; no GPU needed | Answers take about a minute each on a CPU. | |

### Step 2: install the prerequisites (about 10 minutes)

```powershell
winget install --id Git.Git -e
winget install --id astral-sh.uv -e
winget install --id Docker.DockerDesktop -e
```

1. If Docker Desktop asks for WSL 2, run `wsl --install` in an administrator terminal and restart.
2. Start Docker Desktop once and accept its terms. Wait until it shows **Engine running**.
3. Check how much memory Docker can use. WSL 2 gives Docker up to half of the machine's memory by
   default:

   ```powershell
   docker info --format '{{.MemTotal}}'
   ```

   The value is in bytes; it must be at least `12000000000` (12 GB). To raise it, set `memory=12GB`
   (or more) under `[wsl2]` in `%USERPROFILE%\.wslconfig`, then run `wsl --shutdown` and restart
   Docker Desktop. With the Hyper-V backend, use Docker Desktop **Settings > Resources**.
4. Open a new terminal so `git`, `uv`, and `docker` are on `PATH`.

**Success:** `git --version`, `uv --version`, and `docker info` all print without errors.

### Step 3: clone the repository (under a minute)

```powershell
git clone https://github.com/eocrawford/campus-rag.git C:\src\campus-rag
cd C:\src\campus-rag
```

If Windows PowerShell reports that running scripts is disabled, allow local scripts once:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

### Step 4: set up the environment (15 to 45 minutes the first time)

```powershell
.\scripts\setup-local-environment.ps1
```

The script installs Python 3.12 and the locked packages, starts PostgreSQL with pgvector and the
Ollama model server in Docker, pulls the embedding model and the three chat models (about 12 GB,
plus about 8.5 GB for the Ollama image), applies the migrations, and runs the tests. It is safe to
rerun; on a ready machine it takes about a minute and changes nothing.

On a network that blocks the Python package host (`files.pythonhosted.org`), install from a
PyPI-compatible mirror instead. The packages and hashes still come from `uv.lock`:

```powershell
.\scripts\setup-local-environment.ps1 -PackageIndexUrl https://<your-mirror>/pypi/simple/
```

**Success:** the last lines read `Setup complete in <n>s.` followed by the database, model, and
Python settings. Each of the 8 steps prints `[n/8] OK`. A failed step prints `FAILED at step <n>`,
the reason, and a `Fix:` line with the command to run; fix it and rerun the script. See
[local environment setup](local-environment-setup.md) for each step and its exit code.

### Step 5: ingest the corpus (about 15 minutes)

```powershell
uv run python -m campus_rag.ingest
```

The command fetches the allowlisted policy pages at one request per second (about 6 minutes),
splits them into chunks, and embeds every chunk (about 8 to 15 minutes the first time). It prints
one line per page and ends with a summary like this:

```text
Summary
  Pages:     313 selected, 313 fetched, 0 not modified, 0 disallowed, 0 failed, 66 too short, 0 extraction failed
  Documents: 247 inserted, 0 updated, 0 unchanged, 0 removed
  Chunks:    1978 (tokens per chunk: min <n>, median <n>, max <n>)
  Embeddings: 1978 embedded, 0 already current (<n>s, <n> chunks/s)
```

**Success:** exit code `0` (`$LASTEXITCODE`), about 247 documents and 1,978 chunks, and as many
embeddings as chunks. The counts change when the policy site changes. See
[ingest the corpus](ingest-corpus.md).

### Step 6: smoke-test the demo (1 to 3 minutes)

```powershell
.\scripts\demo.ps1 -SkipSlow
```

**Success:** every step prints `OK` or `SKIPPED (-SkipSlow)`, the finale lists each section and its
time, and the last line reads `Demo complete in <n>s.` The script stops the servers it started.

### Full flow from a fresh clone

| Step | Command | Kind | Time | Expected result |
|------|---------|------|------|-----------------|
| Clone | `git clone https://github.com/eocrawford/campus-rag.git C:\src\campus-rag` | Manual | Under a minute | A `campus-rag` folder |
| Set up | `.\scripts\setup-local-environment.ps1` | Manual | 15-45 minutes | `Setup complete`; 8 steps `OK` |
| Ingest | `uv run python -m campus_rag.ingest` | Manual | About 15 minutes | About 247 documents, 1,978 chunks, all embedded; exit code `0` |
| Demo | `.\scripts\demo.ps1` | Automated | About 8 minutes (1 to 3 with `-SkipSlow`) | `Demo complete`; exit code `0` |
| Full answer evaluation (optional) | `uv run python -m campus_rag.evaluate answers` | Manual | 90 minutes to 2 hours | A summary matching [answer results](../docs/results/answers.md) within a question or two |

A test of this flow on a second clone with its own database, sharing the existing model server,
took 2 minutes for setup, including 694 passing tests (Docker images and models were already
present, and packages came from a mirror with `-PackageIndexUrl`), 30 seconds to ingest and embed
20 pages (`--limit 20`: 6 documents and 19 chunks), and 1 minute for `.\scripts\demo.ps1 -SkipSlow`
without the Setup section. On a new machine, the downloads in step 4 and the full ingestion in step 5
take most of the time.

## 2. Run the demo

From the repository root, or with the full path from any directory:

```powershell
.\scripts\demo.ps1
```

Each step prints a numbered banner, a one-line **What this shows** talking point, and the exact
command, then runs it and prints the time it took. Before the first section that needs them, a
preflight checks Docker, the `db` and `ollama` containers (and starts them if needed), the schema,
the corpus, the models, and ports 8000 and 8001.

| Option | Effect |
|--------|--------|
| `-Pause` | Waits for Enter before each step so you can talk. Use it when presenting. |
| `-SkipSlow` | Skips the steps that generate answers with the local LLM: a 1- to 3-minute tour. |
| `-Section <names>` | Runs only these sections, in the usual order, for example `-Section Search,Agent`. Values: `All` (default), `Setup`, `Ingest`, `Search`, `Answers`, `Api`, `Agent`, `Evaluation`. |
| `-KeepServers` | Leaves the API and ticketing servers it started running and prints how to stop them. Run it directly in a terminal: a caller that pipes the output waits until the servers stop. |
| `-ResetDemoData` | At the end, deletes the request log rows, ratings, and tickets created during the run. |
| `-FullIngest` | Also runs the full ingestion in the Ingest section (about 15 minutes). |

`Get-Help .\scripts\demo.ps1 -Full` shows every option and exit code.

### What each section shows

Times are from runs on a 16-core CPU without a GPU, with the models pulled and the corpus ingested.
Each answer took 35 to 105 seconds. Answers take longer when another program uses the CPU: one run
under load took 4 minutes for a single answer. They take seconds when the same question was
answered in the last 30 minutes, because the model server keeps the model loaded and reuses the
prompt it has already read. The live evaluation step took 1.5 minutes, and 7 minutes once while the
judge model loaded under load; press `Ctrl+C` to end the demo early, and cleanup still runs.

The demo questions were each checked against their cited passages in at least two live runs. The
answer wording can still vary slightly between runs.

| Section | Steps | Time | `-SkipSlow` | Talking point |
|---------|-------|------|-------------|---------------|
| Setup | Runs `setup-local-environment.ps1 -SkipTests` | 8-22 s | 8-22 s | One idempotent command builds and verifies the environment. |
| Preflight | Checks Docker, containers, schema, corpus, models, ports | 6-12 s | 6-12 s | The demo fails fast with a fix instead of failing mid-talk. |
| Ingest | A 5-page dry run, then a SQL summary of the stored corpus | 11-13 s | 11-13 s | Polite, deterministic ingestion; documents, chunks, and vectors in one PostgreSQL database. |
| Search | One paraphrased question in keyword, vector, and hybrid search; a policy number in keyword and vector search | 6-16 s | 6-16 s | Each retrieval mode wins on different questions; hybrid search is the default. |
| Answers | A cited answer, a refusal, and a follow-up question | 4 min | skipped | Answers are grounded and cited; code removes invalid citations and refuses unsupported answers. |
| Api | Starts the API and ticketing servers; `/health`, `/search`, `/answer`, `/feedback`; usage report | 1 min | 10-15 s | The same services over HTTP, with request logging and ratings. |
| Agent | The MCP client lists tools, searches, files a ticket, and reads it back; the MCP configuration | 4-10 s | 4-10 s | An AI agent uses the tools; it files a ticket only with the user's consent. |
| Evaluation | Retrieval evaluation, the recorded answer and embedding results, one golden question live | 2 min | 12-20 s | Every change is measured against a golden set. |
| Total | | 7.5 min | 69-76 s | |

The script reuses an API or ticketing server that is already running from this repository and
leaves it running. It stops the servers it started, with their child processes, and checks that
ports 8000 and 8001 are free again. Server logs are in `data\demo\`, and each evaluation writes a
raw results file to `eval-results\`; Git ignores both.

### Stop and restart the services

The database and model server keep running in Docker after the demo. Stop them when you are done:

```powershell
docker compose stop db ollama
```

Next time, start them again with either command; the demo's preflight also starts them:

```powershell
docker compose up -d --wait db ollama
.\scripts\setup-local-environment.ps1 -SkipTests
```

### Exit codes

| Code | Meaning |
|------|---------|
| `0` | Success. |
| `1` | Unexpected error; the output shows where. |
| `2` | An unknown `-Section` value. |
| `10` to `17` | A preflight check failed: `10` repository, `11` uv, `12` Docker, `13` containers, `14` Python environment or migrations, `15` corpus, `16` model, `17` port in use by another program. |
| `21` to `27` | A section failed: `21` Setup, `22` Ingest, `23` Search, `24` Answers, `25` Api, `26` Agent, `27` Evaluation. |
| `30` | A server the demo started did not stop, so its port is still in use. |

A failed ingest dry run (it needs internet access to `policy.uw.edu`) prints a note and the demo
continues with the stored corpus.

## 3. Run each step by hand

Run these from the repository root. They are the commands the script runs, in the same order.

**Setup.** Verify the environment without the tests:

```powershell
.\scripts\setup-local-environment.ps1 -SkipTests
```

**Ingest.** Select, fetch, extract, and chunk 5 pages without writing, then count what is stored:

```powershell
uv run python -m campus_rag.ingest --limit 5 --dry-run
docker compose exec db psql -U campus_rag -d campus_rag -c "SELECT count(*) AS documents FROM documents"
docker compose exec db psql -U campus_rag -d campus_rag -c "SELECT model, count(*) FROM chunk_embeddings GROUP BY model"
```

**Search.** The same question in each mode, then a policy number:

```powershell
uv run python -m campus_rag.retrieve "Who pays my legal bills if I get sued for something I did as part of my university job?" --mode keyword -k 3
uv run python -m campus_rag.retrieve "Who pays my legal bills if I get sued for something I did as part of my university job?" --mode vector -k 3
uv run python -m campus_rag.retrieve "Who pays my legal bills if I get sued for something I did as part of my university job?" --mode hybrid -k 3
uv run python -m campus_rag.retrieve "APS 38.17" --mode keyword -k 3
uv run python -m campus_rag.retrieve "APS 38.17" --mode vector -k 3
```

**Answers.** A cited answer, a refusal, and a follow-up. `--chat` reads the follow-up from standard
input and prints the rewritten `Search query:` above the second answer:

```powershell
uv run python -m campus_rag.ask "Is a service dog allowed inside a campus cafe?"
uv run python -m campus_rag.ask "What time does the campus gym open on Sunday mornings?"
"Can they ask for proof that the dog is certified?" | uv run python -m campus_rag.ask --chat "What may university staff ask a person who brings a service animal into a building?"
```

**API.** Start the API and the mock ticketing API in two terminals. The script runs them with the
environment's Python, as below; `uv run python -m campus_rag.api` works the same way:

```powershell
.\.venv\Scripts\python.exe -m campus_rag.api          # terminal 1; docs at http://127.0.0.1:8000/docs
.\.venv\Scripts\python.exe -m campus_rag.ticketing    # terminal 2; port 8001
```

Then, in a third terminal:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
$search = Invoke-RestMethod -Method Post http://127.0.0.1:8000/search -ContentType application/json -Body '{"query": "Does the university need my permission to show a movie in a lecture hall?", "k": 3}'
$search.results | Select-Object rank, title, url
$answer = Invoke-RestMethod -Method Post http://127.0.0.1:8000/answer -ContentType application/json -Body '{"question": "Can my department give away old office furniture it no longer needs?"}' -TimeoutSec 300
$answer.answer; $answer.citations
Invoke-RestMethod -Method Post http://127.0.0.1:8000/feedback -ContentType application/json -Body (@{ request_id = $answer.request_id; rating = 1 } | ConvertTo-Json)
uv run python -m campus_rag.analytics --days 1
```

With curl, the search is:

```bash
curl -s -X POST http://127.0.0.1:8000/search -H "Content-Type: application/json" -d '{"query": "Does the university need my permission to show a movie in a lecture hall?", "k": 3}'
```

**Agent.** With the ticketing API running, the demo client starts the MCP server over stdio, lists
its tools, searches, and files a ticket. `--ticket` stands in for the user's consent. The client passes
your `CAMPUS_RAG_*` environment variables to the server, as the `env` object of an MCP configuration
would:

```powershell
uv run python scripts/demo_mcp_client.py --ticket
Invoke-RestMethod http://127.0.0.1:8001/tickets/<ticket id>
```

To connect GitHub Copilot CLI or another MCP client, use the configuration in
[run the API and MCP server](run-api-and-mcp.md#run-the-mcp-server).

**Evaluation.** Retrieval over the golden set (about 20 seconds), then one golden question with the
full answer pipeline and the LLM judge (1.5 to 7 minutes, mostly loading the judge model):

```powershell
uv run python -m campus_rag.evaluate retrieval
uv run python -m campus_rag.evaluate answers --ids q016
```

The recorded results are in [retrieval](../docs/results/retrieval.md),
[answers](../docs/results/answers.md), and
[embedding strategies](../docs/results/embedding-strategies.md).

Stop each server with `Ctrl+C` in its terminal.

## 4. A 10-minute interview narrative

Run `.\scripts\demo.ps1 -Pause` and talk at each pause. The commands take about 7.5 minutes, so keep
each talking point short. Rehearse with a full run within 30 minutes of the interview: the model
server keeps the models loaded, and repeated questions answer in seconds instead of a minute. The
last step evaluates one golden question live; let it run while you take questions, or press
`Ctrl+C` (cleanup still runs).

| Minutes | Section | Point to make |
|---------|---------|---------------|
| 0-1 | Setup, Preflight | Show the [system overview](../docs/images/system-overview.png). Everything runs on one laptop: PostgreSQL with pgvector and local Ollama models, no API keys, no data leaves the machine. One idempotent script sets it up and fails with a fix. |
| 1-2 | Ingest | Ingestion is deterministic code with an allowlist, `robots.txt`, and one request per second. The LLM never decides what is stored. |
| 2-4 | Search | Keyword search misses the paraphrase and vector search misses the policy number. Hybrid search fuses both lists with reciprocal rank fusion and ranks the right policy first for the paraphrase; for exact codes, the MCP tool lets an agent choose keyword mode. The tradeoff is measured: hybrid has the best recall@5 (1.00), vector the best MRR (0.91). |
| 4-6 | Answers | Grounding: the model answers only from retrieved passages, code removes citations to passages it was not given, and an unsupported question gets a fixed refusal. Follow-ups are rewritten into standalone queries. |
| 6-8 | Api, Agent | The same services over HTTP and MCP. The tool descriptions tell an agent to cite URLs and say when nothing answers. `draft_ticket` files into an IT service management (ITSM) stand-in only after the user agrees; logs and tickets hold no user identity. |
| 8-10 | Evaluation | Retrieval is measured live in seconds. The recorded answer run: refusal accuracy 0.98, no unanswerable question answered, citation validity 1.00, faithfulness 0.94. Close with the limits (47 questions, an LLM judge) and the next steps from the [case study](../docs/case-study.md#what-id-do-next). |

Without the slow steps (`-SkipSlow`), describe grounding and refusal from the API's `/answer`
schema at `http://127.0.0.1:8000/docs` and the recorded answer results.

## 5. Troubleshooting

| Message starts with | Cause | Fix |
|---------------------|-------|-----|
| `FAILED in Preflight (exit code 11): uv is not installed` | uv is missing or the terminal predates its install. | `winget install --id astral-sh.uv -e`, then open a new terminal. |
| `FAILED in Preflight (exit code 12): the Docker engine is not running` | Docker Desktop is stopped. | Start Docker Desktop and wait for **Engine running**, or run the setup script, which starts it. |
| `FAILED in Preflight (exit code 13): docker compose up -d --wait` | A container could not start, often because its port is busy. | `docker compose logs db ollama`; run `.\scripts\setup-local-environment.ps1`. |
| `FAILED in Preflight (exit code 14): migrations failed` | The database is out of sync with the migrations. | `uv run python -m campus_rag.index.migrate`; if it still fails, `.\scripts\setup-local-environment.ps1 -ResetDatabase` and ingest again. |
| `FAILED in Preflight (exit code 15): the corpus has 0 documents` | The corpus was never ingested, or the database was reset. | `uv run python -m campus_rag.ingest` (about 15 minutes). |
| `FAILED in Preflight (exit code 16): the answer model ... is not available` | A model is not pulled, or a `CAMPUS_RAG_*_MODEL` setting names a model that does not exist. | Run the `ollama pull` command in the `Fix:` line, or correct the setting in `.env` or the environment. |
| `FAILED in Preflight (exit code 17): port 8000 is in use by process <id>` | Another program, not a campus-rag server, uses the port. | Stop that program (`Stop-Process -Id <id>` if it is yours), then rerun. A campus-rag server on the port is reused instead. |
| `NOTE: The dry run failed` | No internet access to `policy.uw.edu`. | Nothing to fix for the demo; it continues with the stored corpus. |
| `FAILED in Api (exit code 25): the api server did not answer on port 8000` | The server failed to start. | Read `data\demo\api.stderr.log`; reproduce with `uv run python -m campus_rag.api`. |
| `FAILED in Api (exit code 25): the API reports a degraded component` | The database or a model failed its health check. | Follow the `detail` printed for that component. |
| `ERROR: draft_ticket returned an error: ... cannot reach the ticketing API` | The ticketing API is not running (when the MCP client runs by hand). | `uv run python -m campus_rag.ticketing`, then rerun the client. |
| `ERROR: the MCP session failed` | The MCP server did not start or crashed. | Run `uv run python -m campus_rag.mcp_server`; it should wait silently for input. Read `data\demo\mcp-server.log`. |
| `Port 8000 is still in use by ...` during cleanup (exit code `30`) | A server the demo started did not stop. | Stop the process id shown with `Stop-Process -Id <id>`. |
| An answer step takes several minutes | Another program uses the CPU or the model server. | Wait, or use `-SkipSlow`; close other model workloads before presenting. |
| `-Pause` does not wait | Standard input is redirected, so the script cannot read Enter. | Run the script directly in a terminal. |

## 6. Clean up demo data

Each run adds rows to the local request log (`request_log`), ratings (`answer_feedback`), and
tickets (`mock_tickets`); the cleanup lines at the end of the run count them. To view them:

```powershell
uv run python -m campus_rag.analytics --days 1
Invoke-RestMethod http://127.0.0.1:8001/tickets    # while the ticketing API runs
```

To delete only the rows a run creates, add `-ResetDemoData`. The script records the largest id in
each table at the start and deletes rows with larger ids at the end, so earlier rows stay. Rows that
another program writes during the run are deleted too.

To clear the tables completely:

```powershell
docker compose exec db psql -U campus_rag -d campus_rag -c "DELETE FROM mock_tickets; DELETE FROM request_log"
```

Deleting `request_log` rows also deletes their ratings. Server logs are in `data\demo\`; delete the
folder at any time.
