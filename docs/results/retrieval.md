# Retrieval results

recall@5 over 36 standalone questions: keyword 0.81, vector 0.97, hybrid 1.00 (hybrid leads).

Generated on 2026-11-03 at commit `e5b1de2+uncommitted`. The corpus has 247 documents and 1,978 chunks, embedded with `nomic-embed-text`. The golden set has 47 questions; the unanswerable ones have no target and are not scored here. Each mode retrieves the top 10 chunks, and a question counts as found when a chunk matching its expected target is among them. See [evaluation](../evaluation.md) for the metric definitions and the [retrieval runbook](../../runbooks/evaluate-retrieval.md) to repeat the run.

## Recall and MRR by mode

Standalone answerable questions, searched as written.

| Mode | Questions | recall@1 | recall@3 | recall@5 | recall@10 | MRR@10 |
|---|---|---|---|---|---|---|
| keyword | 36 | 0.611 | 0.722 | 0.806 | 0.861 | 0.688 |
| vector | 36 | 0.861 | 0.972 | 0.972 | 1.000 | 0.910 |
| hybrid | 36 | 0.694 | 0.944 | 1.000 | 1.000 | 0.818 |

## By question type

Follow-up questions are searched as written, without a rewrite into a standalone query, so they are scored apart from the standalone groups. They are the baseline for the gain from query rewriting.

| Group | Mode | Questions | recall@1 | recall@3 | recall@5 | recall@10 | MRR@10 |
|---|---|---|---|---|---|---|---|
| paraphrase | keyword | 19 | 0.421 | 0.526 | 0.632 | 0.737 | 0.510 |
| paraphrase | vector | 19 | 0.842 | 0.947 | 0.947 | 1.000 | 0.892 |
| paraphrase | hybrid | 19 | 0.579 | 0.895 | 1.000 | 1.000 | 0.743 |
| exact_term | keyword | 17 | 0.824 | 0.941 | 1.000 | 1.000 | 0.887 |
| exact_term | vector | 17 | 0.882 | 1.000 | 1.000 | 1.000 | 0.931 |
| exact_term | hybrid | 17 | 0.824 | 1.000 | 1.000 | 1.000 | 0.902 |
| follow-up (not rewritten) | keyword | 4 | 0.000 | 0.250 | 0.750 | 0.750 | 0.196 |
| follow-up (not rewritten) | vector | 4 | 0.250 | 0.250 | 0.500 | 0.750 | 0.328 |
| follow-up (not rewritten) | hybrid | 4 | 0.000 | 0.250 | 0.750 | 0.750 | 0.183 |

## Keyword ranking

Keyword search ranks chunks with BM25, computed in SQL. PostgreSQL's built-in `ts_rank_cd` has no inverse document frequency, so words found in most chunks ("university", "policy", "student") outweigh rare, distinctive ones. The table compares both rankings in keyword and in hybrid search; vector search is unchanged. BM25 was adopted after the first measurement showed this gap, with the textbook constants `k1 = 1.2` and `b = 0.75` and no parameter search, so the golden set both revealed the problem and measures the fix.

| Search | Questions | recall@1 | recall@3 | recall@5 | recall@10 | MRR@10 |
|---|---|---|---|---|---|---|
| keyword, BM25 | 36 | 0.611 | 0.722 | 0.806 | 0.861 | 0.688 |
| keyword, ts_rank_cd | 36 | 0.222 | 0.306 | 0.389 | 0.583 | 0.299 |
| hybrid, BM25 keyword | 36 | 0.694 | 0.944 | 1.000 | 1.000 | 0.818 |
| hybrid, ts_rank_cd keyword | 36 | 0.417 | 0.722 | 0.833 | 0.972 | 0.601 |
| vector | 36 | 0.861 | 0.972 | 0.972 | 1.000 | 0.910 |

## Latency

Wall time per search on the local machine, from one pass after a warm-up query. Vector and hybrid searches include the call to the embedding server.

| Mode | Searches | Median ms | p95 ms | Median embedding ms |
|---|---|---|---|---|
| keyword | 40 | 45 | 86 | 0 |
| vector | 40 | 46 | 53 | 34 |
| hybrid | 40 | 96 | 146 | 34 |

## HNSW recall

The HNSW index is approximate. This table measures the share of an exact scan's top 10 chunks that the index also returns, for the 47 golden-set questions. `ef_search` is the index's candidate list size: larger values are slower and more accurate.

| Search | Mean recall@10 | Lowest recall@10 | Median ms |
|---|---|---|---|
| HNSW, ef_search = 40 | 0.989 | 0.900 | 9 |
| HNSW, ef_search = 100 | 0.998 | 0.900 | 10 |
| Exact scan | 1.000 | 1.000 | 17 |

## Example questions

One question for each pattern that occurs, with the rank of its first relevant chunk in each mode.

| Question | Type | Pattern | keyword rank | vector rank | hybrid rank |
|---|---|---|---|---|---|
| q003 | exact_term | every mode ranks it first | 1 | 1 | 1 |
| q004 | paraphrase | vector finds it, keyword does not | not in top 10 | 3 | 4 |
| q001 | paraphrase | hybrid ranks it above the single modes | 2 | 2 | 1 |
