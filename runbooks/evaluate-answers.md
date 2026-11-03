# Runbook: evaluate answers

**Goal.** Answer every golden question with the production pipeline, then measure refusals,
citations, retrieval, follow-up rewriting, and judge-graded faithfulness and relevance. The full
run takes about 90 minutes to two hours on a CPU-only machine, saves its progress after every
question, and writes a summary you can commit.

## Prerequisites

| Requirement | Check |
|-------------|-------|
| [Local environment setup](local-environment-setup.md) completed, including the chat models | `docker compose exec ollama ollama list` shows `nomic-embed-text`, `qwen2.5:7b-instruct`, `qwen2.5:3b-instruct`, and `llama3.1:8b` |
| Database running with the corpus ingested and embedded | `docker compose ps db` shows `healthy`; [ingest the corpus](ingest-corpus.md) if `chunks` and `chunk_embeddings` are empty |
| Migrations applied | `uv run python -m campus_rag.index.migrate` prints `Database is up to date` |
| Model server running | `docker compose ps ollama` shows `healthy`; start it with `docker compose up -d --wait ollama` |
| Golden set present | `eval/golden_set.jsonl` exists; see [evaluation](../docs/evaluation.md#golden-set) |
| An otherwise idle machine | Other model requests share the CPU and slow every stage; stop the API and `ask --chat` sessions first |

To pull the judge model by hand:

```powershell
docker compose exec ollama ollama pull llama3.1:8b
```

## Run

From the repository root:

```powershell
uv run python -m campus_rag.evaluate answers                        # every question
uv run python -m campus_rag.evaluate answers --ids q003,q004,q005   # chosen questions
uv run python -m campus_rag.evaluate answers --limit 5              # the first five
uv run python -m campus_rag.evaluate answers --write-report         # also write docs/results/answers.md
```

| Option | Effect |
|--------|--------|
| `--ids ID[,ID...]` | Evaluate only these questions, in golden-set order. |
| `--limit N` | Evaluate only the first `N` selected questions. |
| `--resume PATH` | Continue the run saved in `PATH`: skip its finished questions and add the rest to the same file. |
| `--write-report` | Also write `docs/results/answers.md` from every question in the results file. Commit it with the change it measures. |

The command checks that the embedding model and the answer, rewrite, and judge models are
available, then takes each selected question in golden-set order:

1. It answers the question as `ask` does: hybrid search for the top 8 chunks, up to six passages
   in the prompt, and `qwen2.5:7b-instruct` at temperature 0. A follow-up is asked with the
   earlier question and this run's answer to it as history, so `qwen2.5:3b-instruct` rewrites it
   into a standalone query first. When the earlier question is not in the run (for example
   `--ids q005` alone), the history holds only the earlier question, which is all the rewrite
   uses.
2. It computes the deterministic metrics: correct refusal, citation validity, sentence citation
   coverage, and whether a passage given to the model matches a golden target. For a follow-up,
   it also searches the follow-up as asked and records whether that search would have found a
   target.
3. For an answer (not a refusal), `llama3.1:8b` judges faithfulness and relevance.
4. It saves every finished question to `eval-results/answers-<UTC timestamp>.json` and prints a
   progress line with the question's time, the elapsed time, and an estimate of the time left:

```
[3/6] q016 exact_term: answered, faithfulness 1.00, relevance 5 | 141s, elapsed 7m02s, ETA 7m01s
```

At the end it prints the summary tables, the results file path, and, with `--write-report`, the
report path.

### Duration

On a 16-core CPU without a GPU, an answered question takes about 80-170 seconds: 45-115 seconds
to generate the answer and 25-100 seconds for the two judge requests. A refusal skips the judge
and takes about 40 seconds. The first question also loads each model. The 47-question golden set
takes about 90 minutes to two hours; start it in its own terminal and keep the machine otherwise
idle.

### Resume

The results file is rewritten after every question, so stopping the run (Ctrl+C, a closed
terminal, or a model server failure) loses at most the question in progress. A model server or
database failure stops the run with exit code `7` or `6` and prints the command to continue:

```powershell
uv run python -m campus_rag.evaluate answers --resume eval-results\answers-20261103T204743Z.json
```

Resuming skips every question already in the file, including ones with a judge error. The
answer, rewrite, judge, and embedding models must match the file's; with different models, start a
new run. To render the report from a finished run without answering anything, resume it with
`--write-report`.

## Read the results

| Section | How to read it |
|---------|----------------|
| Summary | Refusal accuracy counts answerable questions answered and unanswerable ones refused; the false-refusal and false-answer rates split its errors. Citation validity is the share of cited passage numbers that were supplied. Sentence citation coverage is the share of answer sentences with their own `[n]`. Retrieval hit is the share of answerable questions whose expected passage reached the model. Faithfulness is the mean share of supported claims; relevance is the mean 1-5 rating. "Supported claims naming no shown passage" counts verdicts where the judge called a claim supported but named a passage number it was not shown; read those verdicts by hand. |
| By question type | The same metrics for paraphrase, exact-term, and unanswerable standalone questions, and for follow-ups. |
| Follow-up rewriting | For each follow-up, the rewritten query and whether a target reached the model searching the follow-up as asked and searching the rewrite. The sentence below it gives the gain. |
| Failure analysis | Each question that failed a check: retrieval miss, false refusal, false answer, invalid citation, unsupported claim, low relevance (2 or less), or judge error. A false refusal with a retrieval miss points at search; a false refusal with a hit points at the prompt or the model. |
| Latency | Median and p95 seconds per stage. Contention with other model requests inflates them. |

Before acting on an unsupported claim or a low relevance score, read the judge's verdicts in the
results file. Each question's `claims` lists every claim with its verdict, passage numbers, and
reason, and `relevance_reason` explains the rating. An 8B judge misreads some claims, and the
golden set is small: one question moves a rate by two or more points.

## Output files

| File | Content | In Git |
|------|---------|--------|
| `eval-results/answers-<timestamp>.json` | Run details (models, commit, corpus size) and, for each question, the answer, rewritten query, cited and invalid passage numbers, passages given to the model (IDs, URLs, headings), metrics, judge verdicts, and timings | No |
| `docs/results/answers.md` | Summary tables, written by `--write-report`; it quotes the questions and, briefly, the model's answers, never passage text. The intro records the date and commit the run started at (`+uncommitted` when the working tree had changes) | Yes |

## Exit codes

| Code | Meaning | Fix |
|------|---------|-----|
| `0` | Success | |
| `1` | Invalid argument or configuration (an unknown `--ids` value, `--limit` below 1, or a `--resume` file that cannot be read or used other models) | Correct the argument |
| `2` | The database is unreachable | `docker compose up -d --wait db` |
| `3` | The schema is out of date | `uv run python -m campus_rag.index.migrate` |
| `5` | The golden set is missing or invalid | Check `eval/golden_set.jsonl` |
| `6` | Another database error | Read the message; continue with `--resume` |
| `7` | The embedding or chat server is unreachable, lacks a model, or timed out | `docker compose up -d --wait ollama`, pull the missing model, then continue with `--resume` |

## Troubleshooting

| Symptom | Cause and fix |
|---------|---------------|
| `chat request ... timed out` | Another request held the model server, or a model was loading. Continue with `--resume`; raise `CAMPUS_RAG_CHAT_TIMEOUT_SECONDS` if it recurs. |
| A question shows `JUDGE ERROR` | The judge's reply was not valid JSON twice. The raw replies are in the question's `judge_replies`. Rerun that question in a new run with `--ids` to retry it. |
| Every question takes much longer than the estimate | The API, an `ask` session, or another evaluation is using the models. Stop them and continue with `--resume`. |
| `--write-report` shows `+uncommitted` | The working tree had changes when the run started. Commit before starting a run whose report you will commit; resuming keeps the starting commit. |
