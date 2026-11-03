# Evaluation

Evaluation separates two questions: did retrieval find the right passages, and did the answer
stay faithful to them? Measuring them separately shows where a failure comes from.
The golden set arrives in M5, retrieval metrics in M6, and answer metrics in M8 of the
[MVP plan](plans/mvp.md).

## Golden set

The golden set is a versioned file of questions, each with:

- the question text;
- the source URL and section that contain the answer;
- a short reference answer written in original words (never copied source text; see
  [data sources](data-sources.md#uw-terms-of-use-constraints)); and
- a flag for questions the corpus cannot answer, which test refusal behavior.

Target size for the MVP is 40 questions, including at least 5 unanswerable ones. Questions should
mix exact-term lookups (policy numbers, system names) with paraphrased questions that share few
words with the source.

## Retrieval metrics

| Metric | Meaning |
|--------|---------|
| **recall@k** | Share of questions whose expected passage appears in the top *k* results. |
| **MRR** | Mean reciprocal rank of the first correct passage; rewards ranking it higher. |

These metrics are deterministic and cost nothing beyond embedding the questions.

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
