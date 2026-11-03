# Runbook: ingest the corpus

**Goal.** Build or refresh the local corpus with one command. The command selects policy pages from
the configured sitemaps, fetches them politely, extracts their text, splits it into chunks, stores
documents and chunks in the local database, and embeds every chunk that has no current embedding.
Rerunning it is safe: unchanged documents stay untouched and unchanged chunks are not embedded again.

## Prerequisites

| Requirement | Check |
|-------------|-------|
| [Local environment setup](local-environment-setup.md) completed | `uv run pytest` passes |
| Database running | `docker compose ps db` shows `healthy`; start it with `docker compose up -d --wait db` |
| Migrations applied | `uv run python -m campus_rag.index.migrate` prints `Database is up to date` (the command needs migration `003`) |
| Model server running with the embedding model pulled | `docker compose ps ollama` shows `healthy`; start it with `docker compose up -d --wait ollama` (the setup script's step 6 also pulls the model) |
| Internet access to `policy.uw.edu` | Open `https://policy.uw.edu/robots.txt` in a browser |

A `--dry-run` needs only internet access; it does not use the database or the model server. A
`--no-embed` run needs the database but not the model server.

## Run

From the repository root, start with a short trial that writes nothing:

```powershell
uv run python -m campus_rag.ingest --limit 10 --dry-run
```

Store those 10 pages without embedding them:

```powershell
uv run python -m campus_rag.ingest --limit 10 --no-embed
```

Ingest and embed the full corpus:

```powershell
uv run python -m campus_rag.ingest
```

| Option | Effect |
|--------|--------|
| `--source NAME` | Ingests only the named source from `config/sources.toml`. Default: all sources. |
| `--limit N` | Processes at most `N` pages in total. Never removes stored documents. The embedding stage still covers every stored chunk that needs it, not only this run's pages. |
| `--dry-run` | Fetches, extracts, and chunks, then reports counts without writing to the database. Never embeds. |
| `--no-embed` | Skips the embedding stage and does not contact the model server. |

## What the command does

1. **Select.** Reads the source rules in `config/sources.toml`, then reads the source's sitemaps
   and keeps the URLs that match the include prefixes and none of the exclude patterns, in sorted
   order. Today this selects about 313 Policy Directory pages.
2. **Fetch.** Requests one page per second with a user agent that links to this repository, and
   skips URLs that `robots.txt` disallows. Transient failures (timeouts, HTTP 429 and 5xx) are
   retried with backoff. Each page and its fetch metadata are cached under `data/raw/`. Conditional
   requests are sent when the site provides an `ETag` or `Last-Modified` header.
3. **Extract.** Reads the page's `<main>` element and keeps the title, the heading hierarchy,
   paragraphs, lists, and table rows. Pages with fewer words than the source's `min_words`, such as
   section index pages, are skipped as too short; on the Policy Directory this skips 66 pages.
4. **Chunk.** Splits each document at its headings into chunks of about 400 tokens and at most
   800 tokens, counted with the `cl100k_base` encoding. Long sections split at paragraph, then
   sentence boundaries, with about 50 tokens of overlap. A section under 100 tokens merges into the
   following section, or else the previous one, when the result fits within 800 tokens. Each chunk
   starts with its heading path, for example `Policy title > 2. Roles > D. Trustees`.
5. **Store.** Writes each document and its chunks in one transaction. A document whose chunks are
   unchanged keeps its stored chunks; only its fetch metadata is updated. A changed document has
   all its chunks replaced.
6. **Remove.** On a full run, deletes stored documents of the source that are no longer selected,
   including pages that are now too short. Pages that failed to fetch or extract, and pages that
   `robots.txt` disallows, are kept, so a failure never deletes data. Removal is skipped for
   `--limit` and `--dry-run` runs, when a selection is empty, and when the source's `max_pages`
   cap may have truncated the selection.
7. **Embed.** Sends every stored chunk that has no embedding from the configured model, or whose
   stored embedding was made from different text, to the model server in batches of 32, and saves
   each batch before requesting the next. Before it fetches anything, the command makes one tiny
   request to the model server; if that fails, the command stops with exit code `7` and tells you
   the fix. When no chunk needs embedding, the stage makes no embedding requests.

Each page prints one progress line, for example:

```text
[12/313] inserted https://policy.uw.edu/directory/<page>/ (<k> chunks, <t> tokens)
```

The status is `inserted`, `updated`, `unchanged`, `skipped`, `disallowed`, or `failed`. In a dry
run, it is the fetch result: `ok` or `not_modified`. The embedding stage prints one line per batch:

```text
embeddings: 1978 chunks to embed with nomic-embed-text
embedded 32/1978 chunks
embedded 64/1978 chunks
```

## Re-embedding behavior

Embeddings live in `chunk_embeddings`, keyed by chunk and model. Each row records the
`content_hash` of the text that was embedded.

| Situation | What happens |
|-----------|--------------|
| First run | Every chunk is embedded. |
| Rerun, no source changes | Nothing is embedded. The stage makes no embedding requests. |
| A document's text changed | Its chunks are replaced, their embeddings are deleted with them, and the new chunks are embedded. Other documents are untouched. |
| A run with `--no-embed`, or an embedding failure | Stored chunks stay valid. The next run without `--no-embed` embeds everything still missing. |
| A different model in `CAMPUS_RAG_EMBEDDING_MODEL` | Every chunk is embedded for that model. The earlier model's embeddings stay for side-by-side comparison. |
| A document is removed | Its chunks and their embeddings are deleted. |

## Expected duration

Fetching a full run takes about 5.5 minutes, because it makes about 318 requests at one request
per second. `policy.uw.edu` sends no `ETag` or `Last-Modified` headers, so every run downloads every
selected page again. Reruns take about as long as the first run and leave unchanged documents
untouched.

The first embedding of the full corpus (about 1,978 chunks) takes about 10 to 15 minutes on a CPU
with `nomic-embed-text` in Docker: a batch of 32 chunks takes roughly 10 to 14 seconds (0.3 to 0.45
seconds per chunk). Later runs embed only changed chunks, and a run with nothing to embed adds only
the one-request connectivity check.

## Verify

A successful run ends with a summary and exit code `0`:

```text
Summary
  Pages:     <n> selected, <n> fetched, 0 not modified, 0 disallowed, 0 failed, <n> too short, 0 extraction failed
  Documents: <n> inserted, 0 updated, 0 unchanged, 0 removed
  Chunks:    <n> (tokens per chunk: min <n>, median <n>, max <n>)
  Embeddings: <n> embedded, <n> already current (<n>s, <n> chunks/s)
  Requests:  <n> HTTP requests
  Elapsed:   <n>s
```

With `--no-embed` the Embeddings line reads `skipped (--no-embed)`.

Check the exit code with `$LASTEXITCODE`. A rerun with no source changes reports every stored
document as `unchanged` and `0 embedded`. To confirm that every chunk has an embedding:

```powershell
docker compose exec db psql -U campus_rag -d campus_rag -c "SELECT (SELECT count(*) FROM chunks) AS chunks, (SELECT count(*) FROM chunk_embeddings WHERE model = 'nomic-embed-text') AS embedded"
```

## Where the data lives

| Data | Location |
|------|----------|
| Raw HTML and fetch metadata | `data/raw/` (ignored by Git) |
| Documents | `documents` table in the local `campus_rag` database |
| Chunks | `chunks` table, one row per chunk, deleted with their document; its `search_tsv` column feeds keyword search |
| Embeddings | `chunk_embeddings` table, one row per chunk and model, deleted with the chunk |
| Model files | The `campus-rag_campus-rag-ollama` Docker volume, which survives a database reset |

The [UW terms of use](../docs/data-sources.md#uw-terms-of-use-constraints) prohibit reproducing
site content. Keep fetched pages, chunks, and database dumps local, and never commit anything under
`data/`. Add a source only after it is on the
[data-source allowlist](../docs/data-sources.md#allowlist).

## Exit codes

| Code | Meaning |
|------|---------|
| `0` | Success. |
| `2` | Cannot connect to the database. |
| `3` | The database schema is out of date. |
| `4` | Some pages failed to fetch or extract. All other pages were processed, and the failures are listed. |
| `5` | Configuration error: `config/sources.toml` is invalid, `--source` names an unknown source, a sitemap could not be read, or `CAMPUS_RAG_EMBEDDING_DIMENSIONS` is not a positive integer. Nothing was stored or removed. |
| `6` | A database error occurred during the run, or the embedding stage failed after it started. Documents, chunks, and finished embedding batches remain valid; a rerun continues where it stopped. |
| `7` | The model server is unreachable, lacks the embedding model, or returns vectors of the wrong size. Nothing was fetched or stored. Fix the server, or rerun with `--no-embed`. |

## Troubleshooting

| Message starts with | Cause | Fix |
|---------------------|-------|-----|
| `ERROR: cannot connect to PostgreSQL at` | The database is not running, or `.env` points at a different host, port, or credentials. | `docker compose up -d --wait db`, then rerun. |
| `ERROR: database schema is out of date` | Migration `003` is not applied. | `uv run python -m campus_rag.index.migrate`, then rerun. |
| `ERROR: cannot reach the embedding server` | The `ollama` container is stopped, or `CAMPUS_RAG_EMBEDDING_BASE_URL` points at the wrong host or port. Exit code `7`. | `docker compose up -d --wait ollama`, then rerun. To store chunks anyway, rerun with `--no-embed`. |
| `ERROR: the embedding server ... does not have model` | The embedding model is not pulled. Exit code `7`. | `docker compose exec ollama ollama pull nomic-embed-text` (use the model named in the message), then rerun. |
| `ERROR: ... returned vectors of <n> dimensions; expected <m>` | `CAMPUS_RAG_EMBEDDING_DIMENSIONS` does not match the model. Exit code `7`. | Set `CAMPUS_RAG_EMBEDDING_DIMENSIONS` in `.env` to the size in the message. A new size needs its own search index; see [the design](../docs/plans/mvp-design.md#10d-storage-schema). |
| `ERROR: embedding failed:` | The model server failed, stopped, or timed out partway through. Exit code `6`. | Check `docker compose ps ollama`, then rerun; finished batches are kept and the rerun embeds the rest. |
| `ERROR: unknown source` | `--source` does not match a `name` in `config/sources.toml`. | Use a name from the list in the message. |
| `ERROR: cannot read the source sitemap` | A sitemap failed to download or is malformed. The run stops before fetching pages. | Open the sitemap URL from the message in a browser. Rerun later if the site is down. |
| `ERROR: <path>\config\sources.toml:` | The source rules are invalid. | Fix the field named in the message. |
| `ERROR: database error:` | The database stopped or rejected a write during the run. | Check `docker compose ps db`, then rerun; the rerun completes the remaining pages. |
| `failed ... HTTP 503 after 4 attempts` or `timeout` | The site is overloaded or unreachable. | Rerun later. Stored copies of failed pages are kept. |
| `failed ... extraction failed:` | The page layout differs from what the extractor expects. | Open the URL in a browser. If the layout changed, update the extractor and its tests. |
| `WARNING: ... robots.txt ...; all its pages were treated as disallowed.` | `robots.txt` could not be read, so no page from that site was fetched. | Check that `https://<site>/robots.txt` loads, then rerun. |
| Many pages `skipped` as too short | The pages are index pages, or `min_words` is too high. | Expected for section index pages. Otherwise adjust `min_words` in `config/sources.toml`. |
