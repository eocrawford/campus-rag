# MVP plan

Goal: a small, well-evaluated RAG service that answers questions about public university
documents with citations, and demonstrates measurable gains from hybrid retrieval.

## Milestones

| # | Milestone | Done when | Status |
|---|-----------|-----------|--------|
| 1 | Repository scaffolding | README, contributor guide, docs, and runbook structure published | Done |
| 2 | Local environment | PostgreSQL with pgvector runs locally; Python project, lint, and tests run | Done: [setup runbook](../../runbooks/local-environment-setup.md) |
| 3 | Corpus | Allowlisted sources selected and documented; about 50 pages ingested and chunked | Not started |
| 4 | Index | Chunks embedded and stored with keyword and vector indexes | Not started |
| 5 | Golden set | 40 questions with expected passages, including unanswerable questions | Not started |
| 6 | Retrieval | Keyword, vector, and hybrid (RRF) search with recall@5 and MRR reported for each | Not started |
| 7 | Grounded answers | Answers cite passages, citations are validated, and unsupported questions are declined | Not started |
| 8 | Answer evaluation | Faithfulness, relevance, and refusal metrics reported with an independent judge | Not started |
| 9 | Interfaces | FastAPI `search` and `answer` endpoints; MCP `search` tool | Not started |
| 10 | Write-up | README results table and a short case study of findings | Not started |
