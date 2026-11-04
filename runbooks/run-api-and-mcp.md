# Runbook: run the API and the MCP server

**Goal.** Search and ask questions about the indexed policies over HTTP (FastAPI) or from an AI
agent (MCP), file a help-desk ticket when the policies do not answer, record usage and ratings, and
read the usage report. The interfaces run on this machine, call the same retrieval and answer code
as the command line, and log each request to the local database without recording who made it.

## Prerequisites

| Requirement | Check |
|-------------|-------|
| [Local environment setup](local-environment-setup.md) completed | `uv run python -m campus_rag.index.migrate` prints `Database is up to date` |
| Migration `004` applied (request log and ratings) | The same command lists `004_request_log_and_feedback.sql` once. Without it, requests still work but nothing is logged, and `GET /health` says so. |
| Migration `005` applied (tickets) | The same command lists `005_mock_tickets.sql` once. The ticketing API needs it. |
| Corpus ingested and embedded | [Ingest the corpus](ingest-corpus.md) finished with exit code `0` |
| Model server running with `nomic-embed-text` and `qwen2.5:7b-instruct` | `GET /health` reports `ok` for `embedding` and `chat` |

## Start the API

From the repository root:

```powershell
uv run python -m campus_rag.api
```

The server listens on `http://127.0.0.1:8000`. Open `http://127.0.0.1:8000/docs` for the interactive
OpenAPI documentation, with an example request for each route. Press `Ctrl+C` to stop it.

| Option | Effect |
|--------|--------|
| `--host HOST` | Address to bind. Default: `127.0.0.1`. |
| `--port PORT` | Port to bind. Default: `8000`. |
| `--allow-remote` | Allows a non-loopback host. |

The API has no authentication and serves passages of University of Washington policies. It refuses
any host except `localhost` and loopback addresses, because hosting a public service over that
content needs UW's permission. `--allow-remote` is for a host you control and have that permission
for.

The command exits `2` with a usage message for an invalid option or a refused host, and `5` when a
setting in `.env` is invalid. The ticketing server uses the same codes, and the MCP server exits `5`
for an invalid setting.

To run it in the background and stop it later, keep the process id that `Start-Process` returns.
`uv run` starts the server as a child process, so stop the Python process too:

```powershell
$api = Start-Process -FilePath uv -ArgumentList 'run', 'python', '-m', 'campus_rag.api' -PassThru -WindowStyle Hidden
Invoke-RestMethod http://127.0.0.1:8000/health
Get-CimInstance Win32_Process -Filter "ParentProcessId=$($api.Id)" | Select-Object ProcessId, Name
Stop-Process -Id <python process id>
Stop-Process -Id $api.Id
```

## Call the API

Check health first. The response is `200` when the database, embedding model, and chat model all
work, and `503` with the same body otherwise. Model checks are cached for 30 seconds.

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
```

```json
{"status": "ok", "schema_version": 5,
 "database": {"ok": true, "detail": "schema version 5"},
 "embedding": {"ok": true, "detail": "model nomic-embed-text answers"},
 "chat": {"ok": true, "detail": "model qwen2.5:7b-instruct is available"}}
```

Search for passages. `mode` is `keyword`, `vector`, or `hybrid` (default); `k` is 1 to 50 (default
8). `sources` and `url_prefixes` are optional filters.

```powershell
$body = @{ query = 'How can a student appeal a grade?'; mode = 'hybrid'; k = 3 } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/search -ContentType 'application/json' -Body $body
```

```bash
curl -s -X POST http://127.0.0.1:8000/search -H "Content-Type: application/json" \
  -d '{"query": "How can a student appeal a grade?", "k": 3}'
```

Each result has `rank`, `score`, `keyword_rank`, `vector_rank`, `title`, `heading_path`, `url`, and
`content_preview` (about 300 characters). `keyword_rank` or `vector_rank` is `null` when that search
did not return the passage. `keyword` mode needs only the database.

Ask a question. A local 7B model on a CPU takes about 45 to 95 seconds, so give the client a long
timeout:

```powershell
$body = @{ question = 'How can a student request a change of grade after it is recorded?' } | ConvertTo-Json
$answer = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/answer -ContentType 'application/json' -Body $body -TimeoutSec 300
$answer.answer
$answer.citations
```

| Field | Meaning |
|-------|---------|
| `answer` | Text with `[n]` markers, or `I don't know based on the available policies.` |
| `refused` | `true` when the passages do not answer the question. |
| `suggest_ticket` | `true` when `refused` is `true`; a client may offer to draft a support ticket. |
| `citations` | The cited passages: `number`, `title`, `heading_path`, `url`. |
| `search_query` | The standalone query used for retrieval. |
| `timings_ms` | Milliseconds per stage (`rewrite` for follow-ups, `retrieve`, `generate`) and `total`. |
| `request_id` | The request's id in the log; `null` if the log write failed. |

For a follow-up question, send the earlier turns in `history`; the question is rewritten into a
standalone query first:

```powershell
$body = @{
    question = 'Does that apply to graduate students?'
    history  = @(
        @{ role = 'user'; content = 'How long may a student take a leave of absence?' },
        @{ role = 'assistant'; content = 'Up to one year [1].' }
    )
} | ConvertTo-Json -Depth 4
```

Rate a search or an answer with the `request_id` from its response. `rating` is `1` (helpful) or
`-1` (not helpful); `comment` is optional and at most 1,000 characters:

```powershell
$body = @{ request_id = $answer.request_id; rating = -1; comment = 'Cited the wrong section.' } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/feedback -ContentType 'application/json' -Body $body
```

### Status codes

| Status | Meaning |
|--------|---------|
| `200`, `201` | Success. `/feedback` returns `201`. |
| `404` | `/feedback` got a `request_id` that is not in the log. |
| `422` | The request is invalid: empty `query` or `question`, `k` outside 1 to 50, an unknown `mode`, a rating other than `1` or `-1`, or too long a text. |
| `503` | The database or a model server is unavailable. The `detail` message names the fix. |
| `500` | An unexpected error. The response is `{"detail": "internal server error"}`; the trace is in the server's console. |

## Read the usage report

```powershell
uv run python -m campus_rag.analytics --days 30
```

The report covers the period you give (default 30 days):

- Requests by day, interface (`api` or `mcp`), and endpoint.
- Latency p50 and p95 per endpoint, over successful requests.
- Refusal rate and mean citations per answered question, over successful answers.
- Error count.
- Rating count, share of positive ratings, and the five most recently rated-down questions with their comments.

The log lives in the `request_log` and `answer_feedback` tables of the local database. It stores the
question text, retrieval and timing figures, model names, and errors, and no address, account, or
session. Questions longer than 2,000 characters are clipped. Delete the rows with
`DELETE FROM request_log` (ratings go with them) to clear the history.

Exit codes: `0` printed, `2` cannot connect to the database, `3` migration `004` is not applied,
`5` invalid configuration, `6` database error.

## Run the MCP server

The MCP server speaks the Model Context Protocol over stdio, so an MCP client starts it; you do not
run it by hand. It offers two tools:

| Tool | Arguments | Returns |
|------|-----------|---------|
| `search_policies` | `query` (required), `k` (1 to 50, default 5), `mode` (`hybrid` default, `keyword`, `vector`) | Ranked passages, each with `rank`, `score`, `title`, `heading_path`, `url`, and `text`. |
| `draft_ticket` | `question`, `summary` (5 to 200 characters), `details` (up to 4,000), `category` (`policy-question` default, `it-help`, `other`) | The filed ticket's `ticket_id`, `url`, `status`, `category`, and `summary`. See [file a help-desk ticket](#file-a-help-desk-ticket). |

The `search_policies` description tells the agent to answer only from the passages and to cite their
URLs. Each call to either tool is logged with interface `mcp`.

Add the server to your MCP client. For GitHub Copilot CLI, run `/mcp` in an interactive session, or
edit `~/.copilot/mcp-config.json` (Windows: `%USERPROFILE%\.copilot\mcp-config.json`):

```json
{
  "mcpServers": {
    "campus-rag": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--directory", "C:\\src\\campus-rag", "python", "-m", "campus_rag.mcp_server"],
      "tools": ["*"]
    }
  }
}
```

Other MCP clients, such as Claude Desktop (`claude_desktop_config.json`), take the same `command`
and `args` in their own config format; check the client's documentation for the file location and
required fields.

Use `--directory`, not `--project`. The server finds `.env` from its working directory, and
`--directory` makes the repository the working directory; `--project` leaves it unchanged, so the
server would ignore `.env`. Alternatively, pass settings in the entry's `env` object, for example
`"env": {"CAMPUS_RAG_DATABASE_URL": "..."}`. Do not put secrets in a file you commit.

To test the server without an AI client, start it from a script with the MCP SDK:

```powershell
@'
import asyncio
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters

async def main():
    params = StdioServerParameters(command="uv", args=["run", "python", "-m", "campus_rag.mcp_server"])
    async with Client(params) as client:
        result = await client.call_tool("search_policies", {"query": "How do I appeal a grade?", "k": 2})
        for item in result.structured_content["results"]:
            print(item["rank"], item["heading_path"], item["url"])

asyncio.run(main())
'@ | uv run python -
```

The server writes only protocol messages to stdout. Log lines, including the embedding requests,
go to stderr, which most clients show in their MCP log.

## File a help-desk ticket

When the policies do not answer a question, the services offer a ticket. Nothing files itself: a
person or an agent decides, and a ticket goes to the mock ticketing API, a loopback stand-in for an
IT service management (ITSM) system such as ServiceNow. It stores tickets in the `mock_tickets`
table of the local database.

Start it in its own terminal, next to the API if you use both:

```powershell
uv run python -m campus_rag.ticketing
```

It listens on `http://127.0.0.1:8001` (options `--host`, `--port`, `--allow-remote`; it refuses a
non-loopback host the same way the API does), with OpenAPI docs at `/docs`. To stop it, press
`Ctrl+C`, or stop the Python process by id as described in [start the API](#start-the-api).

| Route | Purpose |
|-------|---------|
| `POST /tickets` | File an open ticket: `category`, `summary` (5 to 200 characters), `details` (up to 4,000), and optional `source_question` and `request_id`. Returns `201` with the ticket, its `id`, and its `url`. `request_id` must be an id from the request log, or the API returns `422`. |
| `GET /tickets` | List tickets, newest first. Optional `status` (`open` or `closed`) and `limit` (1 to 200, default 50). |
| `GET /tickets/{id}` | Read one ticket; `404` if unknown. |
| `GET /health` | `200` when `mock_tickets` is reachable, otherwise `503`. |

```powershell
Invoke-RestMethod 'http://127.0.0.1:8001/tickets?status=open'
```

**From the API.** A refused `POST /answer` response has `suggest_ticket: true` and a `ticket_draft`
whose `summary`, `details`, `source_question`, and `request_id` are filled from the question. Show
it to the user and, if they agree, post it to the ticketing API:

```powershell
if ($answer.suggest_ticket) {
    $answer.ticket_draft | ConvertTo-Json | Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8001/tickets -ContentType 'application/json'
}
```

**From an agent.** With both MCP tools configured, the flow is:

1. The agent calls `search_policies` and finds no relevant passage.
2. The agent tells the user and asks whether to open a ticket.
3. The user agrees. The agent calls `draft_ticket` with the user's question, a one-line summary, and
   details written from what the user typed, and no other personal data.
4. The agent reports the returned `ticket_id` and `url`.

The MCP server reaches the ticketing API over HTTP at `CAMPUS_RAG_TICKETING_BASE_URL` (default
`http://127.0.0.1:8001`; set it in `.env` or the MCP client's `env` object). If the ticketing server
is not running, `draft_ticket` returns an error that says how to start it. Calls to `draft_ticket`
appear in the [usage report](#read-the-usage-report) under the endpoint `draft_ticket`.

To clear the tickets while testing, run `DELETE FROM mock_tickets` in the database.

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `refusing to bind '<host>'` | The host is not a loopback address. | Use `127.0.0.1` or `localhost`. Pass `--allow-remote` only with UW's permission. |
| `[Errno 10048]` or `address already in use` when starting the API | Another process uses the port. | Stop the other server, or pass `--port`. |
| `/health` returns `503` with `cannot connect` under `database` | The database is not running, or `.env` points elsewhere. | `docker compose up -d --wait db`. |
| `/health` returns `503` with `schema version ... run: ...migrate` | Migrations are not applied. | `uv run python -m campus_rag.index.migrate`. |
| `/health` returns `503` with `cannot reach the embedding server` or `the chat server` | The `ollama` container is stopped, or a base URL in `.env` is wrong. | `docker compose up -d --wait ollama`, then retry; a failed check is retried after 5 seconds. |
| `/health` is `ok` but `database` says `request logging is off` | Migration `004` is not applied. | `uv run python -m campus_rag.index.migrate`. |
| `request_id` is `null` and the server prints `could not write request_log row` | The log write failed; the request itself succeeded. | Read the warning's reason; usually migration `004` is missing. |
| `/answer` returns `503` with `timed out` | Three attempts each took longer than `CAMPUS_RAG_CHAT_TIMEOUT_SECONDS`, usually because another command was using the model. | Wait for it to finish, or raise the timeout in `.env`. |
| `/feedback` returns `404` | The `request_id` is not in the log, or the log was cleared. | Use the id from a response of the current database. |
| The MCP client shows no tools or an error on start | The command or directory in the client config is wrong. | Run the `uv run --directory ... python -m campus_rag.mcp_server` command in a terminal; it should wait silently for input. Press `Ctrl+C` and fix the path. |
| `search_policies` returns an error naming the embedding server | The model server is stopped. | `docker compose up -d --wait ollama`. Use `mode: "keyword"` meanwhile. |
| `draft_ticket` returns `cannot reach the ticketing API` | The ticketing server is not running, or `CAMPUS_RAG_TICKETING_BASE_URL` is wrong. | `uv run python -m campus_rag.ticketing`, then retry. |
| `POST /tickets` returns `422 unknown request_id` | The `request_id` is not in the request log. | Use the id from a response of the current database, or omit it. |
| The ticketing API returns `503` with `database tables are missing` | Migration `005` is not applied. | `uv run python -m campus_rag.index.migrate`. |
