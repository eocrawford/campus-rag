# Evaluation

Evaluation separates two questions: did retrieval find the right passages, and did the answer
stay faithful to them? Measuring them separately shows where a failure comes from. The committed
results are [retrieval](results/retrieval.md), [answers](results/answers.md), and
[embedding strategies](results/embedding-strategies.md).

## Golden set

The golden set, [`eval/golden_set.jsonl`](../eval/golden_set.jsonl), holds 47 questions, one JSON
object per line. [`campus_rag.evaluate.golden`](../src/campus_rag/evaluate/golden.py) loads and
validates it.

| Field | Content |
|-------|---------|
| `id` | Unique identifier, such as `q001`. |
| `question` | The question as a person would ask it. |
| `type` | `paraphrase`, `exact_term`, or `unanswerable`. |
| `expected` | Passages that answer the question: `{url, heading_prefix}` objects. Empty only for unanswerable questions. |
| `reference_answer` | One to three sentences in original words; for unanswerable questions, why the corpus lacks the answer. |
| `follow_up_of` | Optional. The earlier question a multi-turn follow-up depends on, such as "What about graduate students?" |
| `notes` | Optional. Authoring notes. |

| Type | Count | Purpose |
|------|-------|---------|
| `paraphrase` | 23 | Natural questions that share few words with the source. |
| `exact_term` | 17 | Policy numbers, titles, defined terms, and roles. |
| `unanswerable` | 7 | Plausible campus questions the corpus does not cover; they test refusal. |

Four questions are follow-ups. Questions span the Administrative Policy Statements, Presidential
Orders, Board of Regents Governance, Employment and Administrative Policies, Student Governance and
Policies, and the Faculty Code.

A retrieved chunk matches a target when its document URL equals the target URL (ignoring a trailing
slash) and either heading path is a prefix of the other, compared by whole ` > ` segments. Targets
name URLs and headings rather than chunk IDs, so re-chunking does not invalidate them.

Authoring rules:

- Write questions and reference answers in original words; never copy policy text (see
  [data sources](data-sources.md#uw-terms-of-use-constraints)). A question or answer must not share
  a run of eight or more words with any stored chunk.
- Choose a target passage by reading the stored chunks, then write a question that a reader of that
  passage would agree it answers. List every passage that answers it.
- Confirm unanswerable questions by searching the stored chunks for their key terms.
- Run `validate_against_db` after re-ingesting or editing the file; it reports targets whose URL or
  heading no longer exists in the database.

## Retrieval metrics

| Metric | Meaning |
|--------|---------|
| **recall@k** | Share of questions whose expected passage appears in the top *k* results. |
| **MRR** | Mean reciprocal rank of the first correct passage; rewards ranking it higher. |

These metrics are deterministic and cost nothing beyond embedding the questions.
`uv run python -m campus_rag.evaluate retrieval` computes them for keyword, vector, and hybrid
search, overall and by question type, with latency and the HNSW index's recall against an exact
scan; the committed summary is [results/retrieval.md](results/retrieval.md) and the procedure is
in the [retrieval runbook](../runbooks/evaluate-retrieval.md). Follow-up questions are scored
separately, searched as written, as the baseline for the gain from query rewriting.

`uv run python -m campus_rag.evaluate embeddings` uses the same metrics to compare embedding
choices (heading path, task prefixes, a second model, and reduced dimensions) with exact search;
see [decision 15](plans/mvp-design.md#15-embedding-strategy-experiment) and the
[results](results/embedding-strategies.md).

## Answer metrics

`uv run python -m campus_rag.evaluate answers` answers every golden question the way the `ask`
command does (hybrid search, `qwen2.5:7b-instruct`), then scores each answer. Follow-up questions
are asked with the earlier question and this run's answer to it as history, so the query rewrite
runs as it does in chat. The committed summary is [results/answers.md](results/answers.md) and the
procedure is in the [answer evaluation runbook](../runbooks/evaluate-answers.md).

| Metric | Meaning | How it is measured |
|--------|---------|--------------------|
| **Refusal accuracy** | Answerable questions are answered and unanswerable questions are refused. | Deterministic. Reported with the false-refusal rate (answerable questions refused) and the false-answer rate (unanswerable questions answered). |
| **Citation validity** | Every citation refers to a supplied passage. | Deterministic: valid cited numbers / all cited numbers, before the citation check removes the invalid ones. |
| **Sentence citation coverage** | Every sentence of an answer cites a passage. | Deterministic: answer sentences with at least one `[n]` / all answer sentences. The citation check keeps an answer when at least one sentence cites a passage, so this measures how often a sentence lacks its own. |
| **Retrieval hit** | The expected passage reached the answer model. | Deterministic: a passage in the prompt matches a golden target. For follow-ups, the same check on a search with the follow-up as asked gives the gain from rewriting. |
| **Faithfulness** | Every claim in the answer is supported by a passage it cites. | Judge: splits the answer into atomic claims and labels each supported or unsupported, seeing only the passages the answer cites. Score = supported claims / claims. |
| **Answer relevance** | The answer addresses the question asked. | Judge: a 1-5 rating (5 = directly and completely answers; 1 = off-topic), with the golden reference answer as a hint of what a good answer covers. |

The judge is `llama3.1:8b`, from a different model family than the answer model, to avoid
self-grading bias. It runs at temperature 0 with a JSON-schema response format. A reply that is
not valid JSON, or does not match the schema, gets one retry with a repair instruction; a second
failure is recorded as a judge error for that question and the run continues. Code also counts
supported claims for which the judge names no passage it was shown, a sign of a misread to check
by hand. Refusals are not judged. The run also reports the median and p95 time of each stage:
rewrite, retrieval, generation, and judging.

## Comparing changes

Every retrieval or prompt change reports a before/after table in its pull request. The
[retrieval results](results/retrieval.md) compare keyword, vector, and hybrid search, and BM25
keyword ranking with `ts_rank_cd`. With 36 scored questions, one question moves recall by about
0.028, so treat differences under 0.06 as noise.
