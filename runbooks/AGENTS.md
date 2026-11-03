# Runbooks Table Of Contents. Update when you add a new runbook.
  - name: local-environment-setup
    description: Set up and verify the local development environment (Python, PostgreSQL with pgvector, Ollama embedding and chat models, tests) with one command.
    path: runbooks/local-environment-setup.md
  - name: ingest-corpus
    description: Fetch, extract, chunk, store, and embed the allowlisted corpus in the local database; trial, full, no-embed, and rerun commands.
    path: runbooks/ingest-corpus.md
  - name: ask-questions
    description: Ask questions from the command line and read cited answers; follow-up chat, timing on CPU, exit codes, and troubleshooting.
    path: runbooks/ask-questions.md
  - name: evaluate-retrieval
    description: Search the corpus from the command line and measure keyword, vector, and hybrid retrieval (recall@k, MRR, latency, HNSW recall) against the golden set; commands, how to read the tables, and exit codes.
    path: runbooks/evaluate-retrieval.md
  - name: evaluate-answers
    description: Answer every golden question and measure refusals, citations, retrieval hits, follow-up rewriting, and judge-graded faithfulness and relevance; commands, duration on CPU, resuming, and how to read the report.
    path: runbooks/evaluate-answers.md
  - name: run-api-and-mcp
    description: Run the HTTP API, the MCP server, and the mock ticketing API; search, ask, rate answers, and file tickets from curl, PowerShell, or an agent; connect an MCP client, read the usage report, and troubleshoot.
    path: runbooks/run-api-and-mcp.md
