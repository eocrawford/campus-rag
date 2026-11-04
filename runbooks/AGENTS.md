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
    description: Search the corpus from the command line and measure keyword, vector, and hybrid retrieval (recall@k, MRR, latency, HNSW recall) against the golden set, and compare embedding strategies (heading, task prefixes, bge-m3, reduced dimensions); commands, how to read the tables, and exit codes.
    path: runbooks/evaluate-retrieval.md
  - name: evaluate-answers
    description: Answer every golden question and measure refusals, citations, retrieval hits, follow-up rewriting, and judge-graded faithfulness and relevance; commands, duration on CPU, resuming, and how to read the report.
    path: runbooks/evaluate-answers.md
  - name: run-api-and-mcp
    description: Run the HTTP API, the MCP server, and the mock ticketing API; search, ask, rate answers, and file tickets from curl, PowerShell, or an agent; connect an MCP client, read the usage report, and troubleshoot.
    path: runbooks/run-api-and-mcp.md
  - name: run-demo
    description: Set up a clean Windows machine step by step and demonstrate the whole system with scripts/demo.ps1 (setup, ingestion, search modes, cited answers and refusals, the HTTP API, the MCP server, evaluation); options, measured times, manual commands for every step, a 10-minute interview narrative, troubleshooting, and demo data cleanup.
    path: runbooks/run-demo.md
  - name: update-dependencies
    description: Add, remove, or upgrade Python dependencies and commit a canonical uv.lock, including from a network that reaches PyPI only through a mirror (update-lock-via-mirror.ps1 and canonicalize_lock.py).
    path: runbooks/update-dependencies.md
