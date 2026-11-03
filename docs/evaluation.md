# Evaluation

Evaluation separates two questions: did retrieval find the right passages, and did the answer
stay faithful to them? Measuring them separately shows where a failure comes from.
The golden set arrives in M5, retrieval metrics in M6, and answer metrics in M8 of the
[MVP plan](plans/mvp.md).

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
separately, searched as written, so a later query-rewrite change can show its gain.

## Answer metrics

| Metric | Meaning |
|--------|---------|
| **Faithfulness** | Every claim in the answer is supported by a cited passage. |
| **Answer relevance** | The answer addresses the question asked. |
| **Citation validity** | Every citation refers to a supplied passage (deterministic check). |
| **Correct refusal** | Unanswerable questions are declined rather than answered. |

Faithfulness and relevance use an LLM judge. The judge model must differ from the answering model
to avoid self-grading bias.

## Comparing changes

Every retrieval or prompt change reports a before/after table in its pull request. The first
planned comparison is keyword-only versus vector-only versus hybrid retrieval at recall@5.
