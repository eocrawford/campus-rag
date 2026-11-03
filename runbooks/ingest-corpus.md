# Runbook: ingest the corpus

**Goal.** Build or refresh the local corpus with one command. The command selects policy pages from
the configured sitemaps, fetches them politely, extracts their text, splits it into chunks, and
stores documents and chunks in the local database. Rerunning it is safe: unchanged documents stay
untouched.

## Prerequisites

| Requirement | Check |
|-------------|-------|
| [Local environment setup](local-environment-setup.md) completed | `uv run pytest` passes |
| Database running | `docker compose ps db` shows `healthy`; start it with `docker compose up -d --wait db` |
| Migrations applied | `uv run python -m campus_rag.index.migrate` prints `Database is up to date` |
| Internet access to `policy.uw.edu` | Open `https://policy.uw.edu/robots.txt` in a browser |

A `--dry-run` needs only internet access; it does not use the database.

## Run

From the repository root, start with a short trial that writes nothing:

```powershell
uv run python -m campus_rag.ingest --limit 10 --dry-run
```

Store those 10 pages:

```powershell
uv run python -m campus_rag.ingest --limit 10
```

Ingest the full corpus:

```powershell
uv run python -m campus_rag.ingest
```

| Option | Effect |
|--------|--------|
| `--source NAME` | Ingests only the named source from `config/sources.toml`. Default: all sources. |
| `--limit N` | Processes at most `N` pages in total. Never removes stored documents. |
| `--dry-run` | Fetches, extracts, and chunks, then reports counts without writing to the database. |

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
   section index pages, are skipped as too short.
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

Each page prints one progress line, for example:

```text
[12/313] inserted https://policy.uw.edu/directory/<page>/ (<k> chunks, <t> tokens)
```

The status is `inserted`, `updated`, `unchanged`, `skipped`, `disallowed`, or `failed`. In a dry
run, it is the fetch result: `ok` or `not_modified`.

## Expected duration

A full run takes about 6 minutes, because it makes about 320 requests at one request per second.
`policy.uw.edu` sends no `ETag` or `Last-Modified` headers, so every run downloads every selected
page again. Reruns take about as long as the first run and leave unchanged documents untouched.

## Verify

A successful run ends with a summary and exit code `0`:

```text
Summary
  Pages:     <n> selected, <n> fetched, 0 not modified, 0 disallowed, 0 failed, <n> too short, 0 extraction failed
  Documents: <n> inserted, 0 updated, 0 unchanged, 0 removed
  Chunks:    <n> (tokens per chunk: min <n>, median <n>, max <n>)
  Requests:  <n> HTTP requests
  Elapsed:   <n>s
```

Check the exit code with `$LASTEXITCODE`. A rerun with no source changes reports every stored
document as `unchanged`.

## Where the data lives

| Data | Location |
|------|----------|
| Raw HTML and fetch metadata | `data/raw/` (ignored by Git) |
| Documents | `documents` table in the local `campus_rag` database |
| Chunks | `chunks` table, one row per chunk, deleted with their document |

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
| `5` | Configuration error: `config/sources.toml` is invalid, `--source` names an unknown source, or a sitemap could not be read. Nothing was stored or removed. |
| `6` | A database error occurred during the run. Documents already stored remain valid. |

## Troubleshooting

| Message starts with | Cause | Fix |
|---------------------|-------|-----|
| `ERROR: cannot connect to PostgreSQL at` | The database is not running, or `.env` points at a different host, port, or credentials. | `docker compose up -d --wait db`, then rerun. |
| `ERROR: database schema is out of date` | Migration `002` is not applied. | `uv run python -m campus_rag.index.migrate`, then rerun. |
| `ERROR: unknown source` | `--source` does not match a `name` in `config/sources.toml`. | Use a name from the list in the message. |
| `ERROR: cannot read the source sitemap` | A sitemap failed to download or is malformed. The run stops before fetching pages. | Open the sitemap URL from the message in a browser. Rerun later if the site is down. |
| `ERROR: <path>\config\sources.toml:` | The source rules are invalid. | Fix the field named in the message. |
| `ERROR: database error:` | The database stopped or rejected a write during the run. | Check `docker compose ps db`, then rerun; the rerun completes the remaining pages. |
| `failed ... HTTP 503 after 4 attempts` or `timeout` | The site is overloaded or unreachable. | Rerun later. Stored copies of failed pages are kept. |
| `failed ... extraction failed:` | The page layout differs from what the extractor expects. | Open the URL in a browser. If the layout changed, update the extractor and its tests. |
| `WARNING: ... robots.txt ...; all its pages were treated as disallowed.` | `robots.txt` could not be read, so no page from that site was fetched. | Check that `https://<site>/robots.txt` loads, then rerun. |
| Many pages `skipped` as too short | The pages are index pages, or `min_words` is too high. | Expected for section index pages. Otherwise adjust `min_words` in `config/sources.toml`. |
