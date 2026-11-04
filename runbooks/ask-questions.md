# Runbook: ask questions

**Goal.** Ask a question about the indexed policies from the command line and get a short answer
that cites its sources. The command retrieves the most relevant passages, asks the local answer
model to answer only from them, removes citations to passages it was not given, and replaces any
answer without a valid citation with a fixed refusal.

## Prerequisites

| Requirement | Check |
|-------------|-------|
| [Local environment setup](local-environment-setup.md) completed, including the chat models | `docker compose exec ollama ollama list` shows `nomic-embed-text`, `qwen2.5:7b-instruct`, and `qwen2.5:3b-instruct` |
| Database running with migrations applied | `docker compose ps db` shows `healthy`; `uv run python -m campus_rag.index.migrate` prints `Database is up to date` |
| Corpus ingested and embedded | [Ingest the corpus](ingest-corpus.md) finished with exit code `0` |
| Model server running | `docker compose ps ollama` shows `healthy`; start it with `docker compose up -d --wait ollama` |

The setup script's step 6 pulls the chat models unless it ran with `-SkipChatModels`. To pull them
by hand:

```powershell
docker compose exec ollama ollama pull qwen2.5:7b-instruct
docker compose exec ollama ollama pull qwen2.5:3b-instruct
```

## Run

From the repository root:

```powershell
uv run python -m campus_rag.ask "What happens when a student's grades fall below good standing?"
```

| Option | Effect |
|--------|--------|
| `--mode keyword\|vector\|hybrid` | Retrieval mode. Default: `hybrid`. |
| `-k K` | Passages to retrieve, 1 to 50. Default: `8`. The prompt uses at most six of them. |
| `--show-passages` | Also prints every passage given to the model, with its rank and retrieval score. |
| `--json` | Prints the full answer record as JSON: text, refusal flag, citations, passages, invalid citations, and timings. |
| `--chat` | Keeps asking follow-up questions; see [follow-up questions](#follow-up-questions). |

Before answering, the command checks that the database schema is current, that the embedding
model answers, and that the chat server has the answer model (and the rewrite model with `--chat`).
A failed check stops the command before any answer is generated.

## Read the answer

```text
<answer sentence> [1]. <answer sentence> [1][3].

Sources
  [1] <policy title> > <section heading>
      https://policy.uw.edu/directory/<page>/
  [3] <policy title> > <section heading>
      https://policy.uw.edu/directory/<page>/

Timings: retrieve 0.4s, generate 52.0s, total 52.4s (qwen2.5:7b-instruct)
```

- Each `[n]` refers to passage `n` in the prompt. The **Sources** list shows only the passages the
  answer cites, in passage order, with the policy title, the section heading path, and the page
  URL. Open the URL to read the policy itself.
- When the passages do not answer the question, the command prints
  `I don't know based on the available policies.` and no sources. The model replies with that
  sentence itself, and code also substitutes it for any answer left without a valid citation.
- `Removed citations to passages that were not supplied: <n>` means the model cited a passage
  number it was not given. The marker was removed from the text.
- The answer is the model's paraphrase of the passages. Treat the cited policy as the authority.

## Timing

On a 16-core CPU without a GPU, `qwen2.5:7b-instruct` takes about 45 to 90 seconds per answer with
six passages; retrieval takes under a second. Answers are slower while another command uses the
model server. The first question after the model server starts also loads the model, which adds
several seconds. The chat timeout is `CAMPUS_RAG_CHAT_TIMEOUT_SECONDS` (default `300`).

## Follow-up questions

`--chat` starts a conversation. It answers the question on the command line, if any, then prompts
with `>` for the next one. A blank line, `exit`, or `quit` ends it.

```powershell
uv run python -m campus_rag.ask --chat "May a university employee date a student they supervise?"
```

Each follow-up is rewritten by `qwen2.5:3b-instruct` into a standalone question using up to three
earlier questions, for example `Does that also apply to graduate teaching assistants?` becomes a
question that names the topic of the earlier question. A follow-up on a new topic stays as typed.
The rewritten question drives retrieval and the answer, and is printed as `Search query:` above the
answer. The rewrite adds about 1 to 3 seconds on an otherwise idle machine, and more when other models are busy. When the rewrite fails or returns an empty or
overlong reply, the command uses the follow-up as typed.

## Exit codes

| Code | Meaning |
|------|---------|
| `0` | An answer or a refusal was printed. |
| `2` | Cannot connect to the database. Also printed by Python for invalid options, with a usage message. |
| `3` | The database schema is out of date. |
| `5` | Invalid configuration, such as a non-numeric `CAMPUS_RAG_CHAT_TIMEOUT_SECONDS`. |
| `6` | A database error occurred while searching. |
| `7` | The model server is unreachable or lacks the embedding, answer, or rewrite model, or a chat request failed while answering. |

## Troubleshooting

| Message starts with | Cause | Fix |
|---------------------|-------|-----|
| `ERROR: cannot connect to PostgreSQL at` | The database is not running, or `.env` points elsewhere. | `docker compose up -d --wait db`, then rerun. |
| `ERROR: database schema is out of date` | Migrations are not applied. | `uv run python -m campus_rag.index.migrate`, then rerun. |
| `ERROR: cannot reach the embedding server` or `ERROR: cannot reach the chat server` | The `ollama` container is stopped, or `CAMPUS_RAG_EMBEDDING_BASE_URL` or `CAMPUS_RAG_CHAT_BASE_URL` uses the wrong port. Exit code `7`. | `docker compose up -d --wait ollama`, then rerun. |
| `ERROR: the chat server ... does not have model` | The chat model is not pulled. Exit code `7`. | Run the `ollama pull` command in the message, then rerun. |
| `ERROR: chat request ... failed: HTTP 500` | The model server ran out of memory or crashed. | `docker compose logs ollama`; close other memory-heavy programs, then rerun. |
| `ERROR: chat request ... timed out` | Each of three attempts took longer than `CAMPUS_RAG_CHAT_TIMEOUT_SECONDS`, usually because another command was using the model. Exit code `7`. | Wait for the other command to finish, or raise the timeout in `.env`. |
| Every answer is the refusal | Retrieval found nothing relevant, or the corpus is empty. | Rerun with `--show-passages` to see what the model was given. Check the corpus with [ingest the corpus](ingest-corpus.md). |
| A follow-up answer is about the wrong topic | The rewrite lost the topic. | Check the `Search query:` line, then ask the question in full. |
