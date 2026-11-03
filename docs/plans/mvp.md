# MVP plan

Goal: a small, well-evaluated RAG service that answers questions about public university
documents with citations, and demonstrates measurable gains from hybrid retrieval.

## Milestones

| # | Milestone | Done when | Status |
|---|-----------|-----------|--------|
| 1 | Repository scaffolding | README, contributor guide, docs, and runbook structure published | Done |
| 2 | Local environment | PostgreSQL with pgvector runs locally; Python project, lint, and tests run | Done: [setup runbook](../../runbooks/local-environment-setup.md) |
| 3 | Corpus | UW Policy Directory pages selected by committed rules (about 300 pages) fetched, extracted, chunked, and stored | Done: 247 documents, 1,978 chunks ([ingestion runbook](../../runbooks/ingest-corpus.md)) |
| 4 | Index | Chunks embedded and stored with keyword and vector indexes | Done: 1,978 `nomic-embed-text` embeddings with HNSW and full-text indexes ([decision 10](mvp-design.md#10-m4-index-embeddings-and-keyword-search)) |
| 5 | Golden set | 40 questions with expected passages, including unanswerable questions | Done: 47 questions ([evaluation](../evaluation.md#golden-set)) |
| 6 | Retrieval | Keyword, vector, and hybrid (RRF) search with recall@5 and MRR reported for each | Done: hybrid recall@5 1.00 ([results](../results/retrieval.md)) |
| 7 | Grounded answers | Answers cite passages, citations are validated, and unsupported questions are declined | Done ([ask runbook](../../runbooks/ask-questions.md)) |
| 8 | Answer evaluation | Faithfulness, relevance, and refusal metrics reported with an independent judge | Done: faithfulness 0.94, refusal accuracy 0.98, citation validity 1.00 ([results](../results/answers.md)) |
| 9 | Interfaces | FastAPI `search` and `answer` endpoints; MCP `search` tool | Done: API, `search_policies` MCP tool ([runbook](../../runbooks/run-api-and-mcp.md), [decision 13](mvp-design.md#13-m9-interfaces)) |
| 10 | Write-up | README results table and a short case study of findings | Not started |
| 11 | Usage analytics and feedback | Each request logs latency, retrieval scores, and outcome; users can rate answers; a report summarizes usage and ratings | Done: `request_log`, `POST /feedback`, usage report ([runbook](../../runbooks/run-api-and-mcp.md#read-the-usage-report), [decision 14](mvp-design.md#14-m11-m12-analytics-and-mock-ticketing)) |
| 12 | Mock ticketing tool (stretch) | When the corpus cannot answer, an MCP tool drafts a help-desk ticket in a local mock ticketing API | Done: `draft_ticket` MCP tool and mock ticketing API ([runbook](../../runbooks/run-api-and-mcp.md#file-a-help-desk-ticket), [decision 14](mvp-design.md#14-m11-m12-analytics-and-mock-ticketing)) |
