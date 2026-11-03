# Runbook: search the corpus and evaluate retrieval

**Goal.** Inspect what each search mode returns for a question, then measure keyword, vector, and
hybrid search against the golden set: recall@k, MRR, latency, and how closely the HNSW index
matches an exact scan. The evaluation takes about 20 seconds, needs no chat model, and writes a
summary you can commit.

## Prerequisites

| Requirement | Check |
|-------------|-------|
| [Local environment setup](local-environment-setup.md) completed | `uv run pytest` passes |
| Database running with the corpus ingested and embedded | `docker compose ps db` shows `healthy`; [ingest the corpus](ingest-corpus.md) if `chunks` and `chunk_embeddings` are empty |
| Migrations applied | `uv run python -m campus_rag.index.migrate` prints `Database is up to date` |
| Model server running with the embedding model pulled | `docker compose ps ollama` shows `healthy`; vector and hybrid search and the evaluation need it, keyword search does not |
| Golden set present | `eval/golden_set.jsonl` exists; see [evaluation](../docs/evaluation.md#golden-set) |

## Search from the command line

```powershell
uv run python -m campus_rag.retrieve "Can I get a refund if I withdraw from classes?"
uv run python -m campus_rag.retrieve "APS 2.4" --mode keyword -k 5
uv run python -m campus_rag.retrieve "who approves outside consulting" --mode vector --source uw-policy
```

| Option | Effect |
|--------|--------|
| `--mode keyword\|vector\|hybrid` | Search mode. Default: `hybrid`. |
| `-k N` | Number of results, 1 to 50. Default: 8. |
| `--source NAME` | Only documents from this source (repeatable). |
| `--url-prefix PREFIX` | Only documents whose URL starts with `PREFIX`, matched literally (repeatable). |

Each result shows its rank, score, keyword and vector ranks (`-` when the other search did not
return it), title, heading path, URL, and the first 160 characters of the passage. Scores differ by
mode: BM25 for keyword (unbounded), `1 - cosine distance` for vector, and the sum of `1 / (60 + rank)`
for hybrid, so compare scores only within one mode.

## Evaluate

```powershell
uv run python -m campus_rag.evaluate retrieval
uv run python -m campus_rag.evaluate retrieval --write-report
```

| Option | Effect |
|--------|--------|
| `--k N` | Results retrieved per question, 1 to 50. Default: 10. Recall is reported at 1, 3, 5, 10, and `N`. |
| `--write-report` | Also writes `docs/results/retrieval.md`. Commit it with the change it measures. |

The command prints the tables below, writes per-question results to
`eval-results/retrieval-<UTC timestamp>.json` (ignored by Git), and exits `0`.

1. It warms up the connection and the embedding model with one query per mode, which is not timed.
2. It searches each answerable golden question in keyword, vector, and hybrid mode and finds the
   rank of the first chunk that matches an expected passage.
3. It repeats keyword and hybrid search with PostgreSQL's `ts_rank_cd` keyword ranking, the
   baseline that BM25 replaced.
4. It compares the HNSW index with an exact scan on every golden question's vector, at
   `ef_search` 40 (pgvector's default) and 100 (the value search uses).

## Read the results

| Table | How to read it |
|-------|----------------|
| Recall and MRR by mode | recall@k is the share of questions whose expected passage is in the top *k*; MRR@10 averages 1 / rank of the first match. Unanswerable questions are not scored. Use recall@5 to judge what the answer prompt can use, since it passes at most six passages. |
| By question type | The same metrics for `paraphrase` and `exact_term` questions. Keyword search is expected to be strongest on exact terms and weakest on paraphrases. |
| Follow-up rows | Follow-up questions are searched as written, without the query rewrite that chat applies, so they score lower. They are the baseline for measuring the rewrite's gain. |
| Keyword ranking | Keyword and hybrid search with BM25 against `ts_rank_cd`. A change to keyword ranking should show its before and after here. |
| Latency | Median and p95 wall time per search, including the embedding call for vector and hybrid, and the median embedding time alone. Other work on the machine, such as a chat model, inflates it; rerun on an idle machine before comparing. |
| HNSW recall | The share of an exact scan's top 10 that the index returns. Values near 1.000 mean the index loses almost nothing; a lower `ef_search` is faster and less accurate. |

The golden set is small (36 standalone answerable questions), so one question moves recall@5 by
0.028. Treat differences of a few points as noise and judge changes on the by-type and per-question
results in the raw JSON, which lists each question's rank in every mode.

## Output files

| File | Content | In Git |
|------|---------|--------|
| `eval-results/retrieval-<timestamp>.json` | Run details, per-question ranks, timings, and the retrieved chunk IDs, URLs, and headings for each mode | No |
| `docs/results/retrieval.md` | Summary tables, written by `--write-report`; the intro records the date and the commit (`+uncommitted` when the working tree has changes, so rerun after committing) | Yes |

## Exit codes

| Code | Meaning | Fix |
|------|---------|-----|
| `0` | Success | |
| `1` | Invalid argument or configuration (an empty query, `-k` or `--k` out of range) | Correct the argument |
| `2` | The database is unreachable | `docker compose up -d --wait db` |
| `3` | The schema is out of date | `uv run python -m campus_rag.index.migrate` |
| `5` | The golden set is missing or invalid (evaluation only) | Check `eval/golden_set.jsonl` |
| `6` | Another database error | Read the message; rerun |
| `7` | The embedding server is unreachable or lacks the model | `docker compose up -d --wait ollama`, then pull the model; keyword search needs no server |

## Troubleshooting

| Symptom | Cause and fix |
|---------|---------------|
| `No results.` for a keyword query | The question has no searchable words (only stop words or punctuation), or no chunk shares one. Rephrase, or use `--mode vector`. |
| Vector search returns nothing | No embeddings exist for the configured model. Run `uv run python -m campus_rag.ingest` with the model server running; it embeds the missing chunks. |
| `unrecognized configuration parameter "hnsw.iterative_scan"` | The database image has pgvector older than 0.8. Update the `db` image and recreate the container. |
| `--write-report` shows `+uncommitted` | The working tree had changes when the report was written. Commit, then rerun to record the commit that produced the numbers. |
| Latency is much higher than the committed report | The embedding model was loading or another model was using the CPU. Rerun on an idle machine. |
