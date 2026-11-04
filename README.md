# campus-rag

campus-rag answers questions about public university policy documents with cited sources. It
retrieves relevant passages, grounds an LLM answer in those passages, and declines to answer when
the sources do not support one.

> **Status: MVP complete.** All twelve milestones in the [MVP plan](docs/plans/mvp.md) are done:
> 247 UW Policy Directory documents in 1,978 chunks, hybrid BM25 and vector search, cited answers
> from a local LLM, an evaluation harness, an HTTP API, an MCP server, usage analytics, and a mock
> ticketing service. Optional next steps are a reranker, a larger human-graded golden set, and a
> second source; see [what I'd do next](docs/case-study.md#what-id-do-next).

![System overview: offline indexing fetches, chunks, and embeds policy pages into PostgreSQL with pgvector; the CLI, HTTP API, and MCP server answer or search through hybrid search and local Ollama models, with citation validation, a mock ticketing service, usage logs, and an evaluation harness.](docs/images/system-overview.png)

Offline indexing fills the PostgreSQL index that the CLI, HTTP API, and MCP server search, and a
local LLM writes the cited answers; see [architecture](docs/architecture.md#components) for the
legend and a step-by-step diagram of answering a question.

## Results

Measured against a 47-question golden set. The [case study](docs/case-study.md) explains the
decisions behind these numbers and their limits.

| Measure | Result | Report |
|---------|--------|--------|
| Retrieval recall@5, keyword / vector / hybrid (36 questions) | 0.81 / 0.97 / **1.00** | [Retrieval](docs/results/retrieval.md) |
| Retrieval MRR@10, keyword / vector / hybrid (36 questions) | 0.69 / **0.91** / 0.82 | [Retrieval](docs/results/retrieval.md) |
| Refusal accuracy (47 questions; no unanswerable question answered) | 0.98 | [Answers](docs/results/answers.md) |
| Citation validity (cited passages that were supplied) | 1.00 | [Answers](docs/results/answers.md) |
| Faithfulness (LLM judge) | 0.94 | [Answers](docs/results/answers.md) |
| Answer relevance (LLM judge, 1-5) | 4.44 | [Answers](docs/results/answers.md) |

Hybrid search finds a correct passage in the top five for every question; vector search alone
ranks it first more often. An answer takes about a minute on a CPU.

## Why

University staff, faculty, and students spend time searching scattered policy pages. A grounded
assistant answers in plain language and links to the exact source, so readers can verify the
answer instead of trusting the model.

## How it works

Ingestion fetches and chunks public policy pages, embeds each chunk locally, and stores text and
vectors in PostgreSQL with pgvector. A question is rewritten when it follows up on an earlier one,
searched with BM25 keyword and vector search fused by reciprocal rank fusion, and answered by a
local LLM that must cite the passages it uses; code removes invalid citations and returns a fixed
refusal when nothing supports an answer.

| Layer | Responsibility |
|-------|----------------|
| **Ingestion** | Fetch allowed public pages, extract text, chunk with overlap, embed locally, record source metadata. |
| **Index** | Store chunks, embeddings, and metadata in PostgreSQL with pgvector and full-text search. |
| **Retrieval** | Combine BM25 keyword and vector results with reciprocal rank fusion (RRF), with optional source and URL filters. |
| **Generation** | Answer only from retrieved passages, cite each claim, and decline when evidence is missing. |
| **Evaluation** | Measure retrieval recall, answer faithfulness, and embedding strategies against a golden question set. |
| **API and tools** | Expose search and answer through a FastAPI service and an MCP server with `search_policies` and `draft_ticket` tools. |
| **Analytics** | Log requests and answer ratings locally and summarize them in a usage report. |

See [architecture](docs/architecture.md) for the diagrams, design details, and tradeoffs.

## Quick start

Everything runs on your machine: PostgreSQL and the Ollama model server run in Docker on
`127.0.0.1`, and no API key or hosted service is needed. You need Windows 10 or 11, Git,
[uv](https://docs.astral.sh/uv/), and Docker Desktop with at least 12 GB of memory and about 25 GB
of free disk space. No GPU is needed; the published timings come from a 16-core CPU. From the
repository root:

```powershell
.\scripts\setup-local-environment.ps1   # Python, PostgreSQL + pgvector, Ollama models, migrations, tests
uv run python -m campus_rag.ingest       # fetch, chunk, and embed the corpus
uv run python -m campus_rag.ask "How can a student request a change of grade after it is recorded?"
uv run python -m campus_rag.api          # HTTP API with OpenAPI docs at http://127.0.0.1:8000/docs
uv run python -m campus_rag.evaluate retrieval
```

| Step | Time | Runbook |
|------|------|---------|
| Setup | 15-45 minutes the first time, mostly downloads (about 12 GB of models) | [Local environment setup](runbooks/local-environment-setup.md) |
| Ingest | About 15 to 20 minutes the first time | [Ingest the corpus](runbooks/ingest-corpus.md) |
| Ask | About a minute per answer; `--chat` for follow-ups | [Ask questions](runbooks/ask-questions.md) |
| API and MCP | The MCP server starts from your MCP client's configuration | [Run the API and MCP server](runbooks/run-api-and-mcp.md) |
| Evaluate | About 20 seconds for retrieval; 90 minutes to two hours for answers | [Evaluate retrieval](runbooks/evaluate-retrieval.md), [evaluate answers](runbooks/evaluate-answers.md) |

## Repository layout

| Path | Contents |
|------|----------|
| `src/` | Application source code (`campus_rag` package). See [`src/README.md`](src/README.md). |
| `tests/` | Unit tests and database tests. |
| `eval/` | The golden question set. |
| `config/` | Corpus selection rules (URLs and filters only; fetched content is never committed). |
| `scripts/` | Repository entry-point scripts. See [`scripts/README.md`](scripts/README.md). |
| `docs/` | Case study, architecture, evaluation results, data-source policy, plans, and contributor guidance. |
| `runbooks/` | Repeatable operational procedures. |
| `compose.yaml` | Local PostgreSQL and Ollama services. |

## Documentation

- [Walkthrough](docs/walkthrough.md): run the whole system, then read the code in the order a question flows
- [Run the demo](runbooks/run-demo.md): set up a clean machine and demo every part, step by step
- [Case study](docs/case-study.md): decisions, evidence, and limits
- [Documentation index](docs/README.md): architecture, evaluation and results, data sources, glossary, and plans
- [Runbooks](runbooks/AGENTS.md)
- [Contributor and agent guide](AGENTS.md)

## License

[MIT](LICENSE)
