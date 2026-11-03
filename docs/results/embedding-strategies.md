# Embedding strategy results

Generated on 2026-11-03 at commit `05ad702`. The corpus has 247 documents and 1,978 chunks. The golden set has 47 questions; the 36 standalone answerable ones are scored here, and each variant retrieves the top 10 chunks by exact cosine search. Hybrid search fuses the variant's top 50 vector results with the production BM25 keyword results by reciprocal rank fusion. See [decision 15](../plans/mvp-design.md#15-embedding-strategy-experiment) for the design and the [retrieval runbook](../../runbooks/evaluate-retrieval.md#compare-embedding-strategies) to repeat the run.

## How much to trust the differences

With 36 questions, one question moves recall by about 0.028. Differences under 0.06, about two questions, are not meaningful.

## Variants

| Variant | Model | Text embedded | Task prefixes | Dimensions | Notes |
|---|---|---|---|---|---|
| `baseline` | `nomic-embed-text` | Heading path + body | Yes | 768 | Production rows, not re-embedded |
| `no-heading` | `nomic-embed-text` | Body only | Yes | 768 | Heading path stripped from the embedded text |
| `no-task-prefix` | `nomic-embed-text` | Heading path + body | None | 768 | Same text as the baseline without the nomic task prefixes |
| `bge-m3` | `bge-m3` | Heading path + body | None | 1,024 | A second model; it uses no task prefixes |
| `nomic-256` | `nomic-embed-text` | Heading path + body | Yes | First 256 of 768, renormalized | Matryoshka truncation of the baseline vectors; no model calls |

## Vector search

Overall, over the 36 standalone answerable questions.

| Variant | Questions | recall@1 | recall@5 | recall@10 | MRR@10 |
|---|---|---|---|---|---|
| `baseline` | 36 | 0.861 | 0.972 | 1.000 | 0.910 |
| `no-heading` | 36 | 0.833 | 1.000 | 1.000 | 0.901 |
| `no-task-prefix` | 36 | 0.722 | 0.972 | 1.000 | 0.843 |
| `bge-m3` | 36 | 0.806 | 0.972 | 1.000 | 0.878 |
| `nomic-256` | 36 | 0.889 | 1.000 | 1.000 | 0.928 |

By question type.

| Question type | Variant | Questions | recall@1 | recall@5 | recall@10 | MRR@10 |
|---|---|---|---|---|---|---|
| paraphrase | `baseline` | 19 | 0.842 | 0.947 | 1.000 | 0.892 |
| paraphrase | `no-heading` | 19 | 0.895 | 1.000 | 1.000 | 0.934 |
| paraphrase | `no-task-prefix` | 19 | 0.737 | 0.947 | 1.000 | 0.850 |
| paraphrase | `bge-m3` | 19 | 0.842 | 0.947 | 1.000 | 0.893 |
| paraphrase | `nomic-256` | 19 | 0.842 | 1.000 | 1.000 | 0.889 |
| exact_term | `baseline` | 17 | 0.882 | 1.000 | 1.000 | 0.931 |
| exact_term | `no-heading` | 17 | 0.765 | 1.000 | 1.000 | 0.865 |
| exact_term | `no-task-prefix` | 17 | 0.706 | 1.000 | 1.000 | 0.835 |
| exact_term | `bge-m3` | 17 | 0.765 | 1.000 | 1.000 | 0.863 |
| exact_term | `nomic-256` | 17 | 0.941 | 1.000 | 1.000 | 0.971 |

## Hybrid search

Overall, over the 36 standalone answerable questions.

| Variant | Questions | recall@1 | recall@5 | recall@10 | MRR@10 |
|---|---|---|---|---|---|
| `baseline` | 36 | 0.694 | 1.000 | 1.000 | 0.818 |
| `no-heading` | 36 | 0.694 | 0.972 | 1.000 | 0.820 |
| `no-task-prefix` | 36 | 0.694 | 1.000 | 1.000 | 0.822 |
| `bge-m3` | 36 | 0.694 | 0.972 | 1.000 | 0.823 |
| `nomic-256` | 36 | 0.694 | 1.000 | 1.000 | 0.831 |

By question type.

| Question type | Variant | Questions | recall@1 | recall@5 | recall@10 | MRR@10 |
|---|---|---|---|---|---|---|
| paraphrase | `baseline` | 19 | 0.579 | 1.000 | 1.000 | 0.743 |
| paraphrase | `no-heading` | 19 | 0.632 | 0.947 | 1.000 | 0.773 |
| paraphrase | `no-task-prefix` | 19 | 0.579 | 1.000 | 1.000 | 0.750 |
| paraphrase | `bge-m3` | 19 | 0.579 | 0.947 | 1.000 | 0.744 |
| paraphrase | `nomic-256` | 19 | 0.579 | 1.000 | 1.000 | 0.758 |
| exact_term | `baseline` | 17 | 0.824 | 1.000 | 1.000 | 0.902 |
| exact_term | `no-heading` | 17 | 0.765 | 1.000 | 1.000 | 0.873 |
| exact_term | `no-task-prefix` | 17 | 0.824 | 1.000 | 1.000 | 0.902 |
| exact_term | `bge-m3` | 17 | 0.824 | 1.000 | 1.000 | 0.912 |
| exact_term | `nomic-256` | 17 | 0.824 | 1.000 | 1.000 | 0.912 |

## Embedding time

Corpus embedding time is the time in embedding requests on this CPU, in batches of 32; query embedding time is the median over the scored questions.

| Variant | Corpus embedding time | Median query embedding ms |
|---|---|---|
| `baseline` | production vectors reused | 38 |
| `no-heading` | 512 s (8.5 min) | 38 |
| `no-task-prefix` | 530 s (8.8 min) | 32 |
| `bge-m3` | 1220 s (20.3 min) | 73 |
| `nomic-256` | 1.6 s, derived with no model calls | 37 |

## What this shows

In vector search the baseline's recall@1 is 0.861 over 36 questions. The change from the baseline is `no-heading` -0.028, `no-task-prefix` -0.139, `bge-m3` -0.056, `nomic-256` +0.028. Only `no-task-prefix` differs by 0.06 or more. In vector search the baseline's recall@5 is 0.972 over 36 questions. The change from the baseline is `no-heading` +0.028, `no-task-prefix` +0.000, `bge-m3` +0.000, `nomic-256` +0.028. None differs by 0.06 or more, so none is distinguishable from noise.

In hybrid search the baseline's recall@1 is 0.694 over 36 questions. The change from the baseline is `no-heading` +0.000, `no-task-prefix` +0.000, `bge-m3` +0.000, `nomic-256` +0.000. None differs by 0.06 or more, so none is distinguishable from noise. In hybrid search the baseline's recall@5 is 1.000 over 36 questions. The change from the baseline is `no-heading` -0.028, `no-task-prefix` +0.000, `bge-m3` -0.028, `nomic-256` +0.000. None differs by 0.06 or more, so none is distinguishable from noise.
