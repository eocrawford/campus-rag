# Case study: campus-rag

campus-rag answers questions about 247 public University of Washington policy documents with
citations that code has checked, and declines when the policies do not support an answer. Every
model runs on one local machine, and a 47-question golden set measures retrieval and answers.

## Problem

University staff, faculty, and students look up rules spread across hundreds of policy pages:
leave, grading, records, and the use of university resources. Site search works only when the
reader knows the policy's words, and a general chatbot can answer confidently and wrongly. A
useful assistant answers from the official text, shows which passage supports each statement, and
says "I don't know" when the policies are silent. Citations let a reader verify an answer instead
of trusting the model.

## What I built

An offline pipeline splits 247 UW Policy Directory documents into 1,978 heading-aware chunks and
embeds them into PostgreSQL with pgvector. At question time, a follow-up is rewritten into a
standalone query, BM25 keyword search and vector search are fused with reciprocal rank fusion, and
a local 7B model answers from up to six numbered passages. Code validates every citation and
replaces an uncited answer with a fixed refusal. A command line, a FastAPI service, and an MCP
server for AI agents share this pipeline, with request logging, answer ratings, and a mock
help-desk ticketing API. See the [architecture diagrams](architecture.md#components).

## Key decisions and evidence

Each decision is recorded with its alternatives in the [design decisions](plans/mvp-design.md).
Retrieval figures cover the 36 standalone answerable golden questions
([retrieval results](results/retrieval.md)); answer figures cover all 47
([answer results](results/answers.md)).

### Hybrid retrieval, ranked with BM25

**Decision:** fuse keyword and vector search with reciprocal rank fusion, and rank keyword results
with BM25 computed in SQL ([decision 11b](plans/mvp-design.md#11b-search),
[`keyword.py`](../src/campus_rag/retrieve/keyword.py)). **Alternatives:** vector search alone, or
PostgreSQL's built-in `ts_rank_cd` ranking.

**Evidence:** `ts_rank_cd` has no inverse document frequency, so common words such as "university"
count as much as rare, distinctive ones. Its keyword recall@5 was 0.39, and hybrid search built on
it scored 0.83, below vector search alone at 0.97. BM25, with textbook constants and no tuning,
raised keyword recall@5 to 0.81 and hybrid recall@5 to 1.00
([keyword ranking](results/retrieval.md#keyword-ranking)).

**Tradeoff:** hybrid search puts a correct passage in the top five for every question, but vector
search alone ranks it first more often: MRR@10 is 0.91 for vector and 0.82 for hybrid, and
recall@1 is 0.86 against 0.69. Hybrid's lead at recall@5 is one question, within the noise. Hybrid
stays the default because the prompt holds up to six passages, so a passage in the top five
reaches the model whatever its rank.

### Local models on a CPU

**Decision:** run embeddings (`nomic-embed-text`), answers (`qwen2.5:7b-instruct`), follow-up
rewrites (`qwen2.5:3b-instruct`), and the evaluation judge (`llama3.1:8b`) in Ollama on the same
machine ([decision 10a](plans/mvp-design.md#10a-embedding-provider-and-model),
[decision 12a](plans/mvp-design.md#12a-models)). **Alternative:** a hosted API.

**Evidence:** there is no per-request cost, and no policy text or question leaves the machine. The
price is latency: on a 16-core CPU with no GPU, an answer takes about a minute (median generation
time 59 seconds in the answer evaluation), while hybrid retrieval takes about 0.1 seconds. The two
3B models tried answered in 12 to 27 seconds instead of 46 to 53, but neither cited passages in
the required format.

### Deterministic guardrails

**Decision:** code, not the prompt, enforces grounding
([`citations.py`](../src/campus_rag/generate/citations.py),
[decision 12b](plans/mvp-design.md#12b-grounded-answers)). Citations to passages that were not
supplied are removed, and an answer with no valid citation left becomes a fixed refusal.
**Alternatives:** trust the prompt alone, or request JSON output, which cited 5 of 7 sentences
against 5 of 5 for plain text in a four-question trial.

**Evidence:** citation validity is 1.00 (61 of 61 citation numbers named a supplied passage,
counted before the check removes any). Refusal accuracy is 0.98 (46 of 47) with no false answers:
all 7 unanswerable questions were refused. The one error is a false refusal of a question whose
target passage was in the prompt. The judge rated faithfulness 0.94 and relevance 4.44 of 5.

### Embedding strategy experiment

**Decision:** test each embedding choice on this corpus with exact search
([decision 15](plans/mvp-design.md#15-embedding-strategy-experiment)): no heading path in the
embedded text, no task prefixes, a second model (`bge-m3`), and vectors truncated to 256
dimensions. **Alternative:** rely on published benchmarks.

**Evidence** ([results](results/embedding-strategies.md)): task prefixes are the only choice whose
effect exceeds the noise. Without them, vector recall@1 falls from 0.86 to 0.72 and MRR@10 from
0.91 to 0.84. `bge-m3` is no better and takes 2.4 times as long to embed the corpus. The
256-dimension vectors match the full 768 within one question, so one third of the storage is a
viable option. Hybrid search hides every difference. Production keeps the 768-dimension
`nomic-embed-text` vectors with prefixes.

### Follow-up rewriting, corrected by evaluation

**Decision:** a small model rewrites a follow-up such as "What about graduate students?" into a
standalone query ([`rewrite.py`](../src/campus_rag/generate/rewrite.py)). **Alternative:** search
the follow-up as typed.

**Evidence:** measurement exposed two faults. With the assistant's answers in its prompt, the 3B
model returned follow-ups unchanged, so it now sees only the earlier questions. It also treated
elliptical follow-ups as new topics, which golden question q005 revealed, so the prompt now names
that pattern and gives four worked examples unrelated to the golden set. All four golden
follow-ups are now rewritten with their earlier topic. Retrieval did not improve: the rewritten
query reached a target passage for 3 of 4 follow-ups, the same as the follow-up as typed. The
[report](results/answers.md#follow-up-rewriting) discloses that the golden set both revealed the
fault and measures the fix.

### Agentic interface

**Decision:** expose search to AI agents as an MCP tool, `search_policies`, so the agent decides
when and what to search, and add a `draft_ticket` tool that files a help-desk ticket in a local
mock IT service management (ITSM) API over HTTP ([decision 14](plans/mvp-design.md#14-m11-m12-analytics-and-mock-ticketing),
[`mcp_server.py`](../src/campus_rag/mcp_server.py)). **Alternative:** a fixed retrieve-then-answer
endpoint only.

**Evidence:** when the policies do not answer, the HTTP API returns a ticket draft that code builds
from the question; nothing is filed automatically. The `draft_ticket` description tells the agent
to file only after search found nothing and the user agreed, and to add no personal data. That
rule depends on the agent following its instructions; a production system would confirm consent in
the client. Every API and MCP call is logged locally, without user identity, for a usage report.

## Limits

- **Uncited sentences.** An answer is kept when at least one sentence cites a passage; only 0.61 of
  answer sentences (48 of 79) carry their own citation.
- **Follow-ups are the weakest group:** faithfulness 0.79, relevance 3.50, one retrieval miss in
  four.
- **One small judge.** A single 8B judge was sometimes lenient, did not always split claims
  atomically, and returned one unusable reply. No answer was graded by a person.
- **A small golden set.** With 36 questions scored for retrieval, one question moves a rate by
  about 0.03, so differences under 0.06 are noise.
- **CPU latency.** About a minute per answer suits a demonstration, not a help desk.
- **One source.** UW-IT Connect is approved but deferred, because its pages render in the browser
  and the fetched HTML holds no article text.
- **No reranker.** No local cross-encoder runs without adding PyTorch, and LLM reranking would
  cost about 40 seconds per query on this CPU.

## Responsible use

- Ingestion reads only allowlisted public pages, honors `robots.txt`, and makes at most one request
  per second with a user agent that links to this repository ([data sources](data-sources.md)).
- Pages, chunks, and embeddings stay on the local machine and out of Git.
- No personal, student, patient, or personnel data is ingested, so nothing covered by FERPA or
  HIPAA is processed.
- Golden questions and reference answers are written in original words, and answers are short
  summaries that link to the source.
- The API and ticketing servers refuse non-loopback addresses unless overridden, because hosting a
  public service over UW content needs UW's permission.

## How it was built

I built campus-rag with AI coding agents, under my direction and review. Each design decision was
recorded in [`docs/plans`](plans/mvp-design.md), with its options, tradeoffs, and status, before it
was implemented. Changes are validated with lint and about 700 tests that use fake model providers
and a test database; retrieval and prompt changes also carry evaluation runs, and each committed
report records the commit it measured.

## What I'd do next

- Rerank hybrid results with a hosted or GPU model, to recover vector search's rank-one lead.
- Grow the golden set and grade answers by hand, to calibrate the judge and lower the noise floor.
- Add UW-IT Connect through headless-browser rendering, with UW's permission.
- Require a citation on every answer sentence.
- Deploy behind UW sign-in, if UW approves hosting.
