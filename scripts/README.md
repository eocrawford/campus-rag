# scripts

Repository entry-point scripts. Each script is documented in a runbook under
[`runbooks/`](../runbooks/AGENTS.md).

| Script | Purpose | Runbook |
|--------|---------|---------|
| `setup-local-environment.ps1` | Set up and verify the local environment: Python, PostgreSQL with pgvector, the Ollama embedding server and model, migrations, and tests. | [Local environment setup](../runbooks/local-environment-setup.md) |
| `demo.ps1`, `demo_mcp_client.py` | Demonstrate the whole system step by step, from setup and ingestion to search, answers, the API, the MCP server, and evaluation. `demo_mcp_client.py` connects to the MCP server over stdio the way an AI agent does. | [Run the demo](../runbooks/run-demo.md) |
| `update-lock-via-mirror.ps1`, `canonicalize_lock.py` | Update `uv.lock` through a PyPI mirror on a network that blocks PyPI's file host, rewriting the result to canonical PyPI URLs after verifying each file's sha256 and size against pypi.org. | [Update dependencies](../runbooks/update-dependencies.md) |
| `verify_glossary.py` | Check every Markdown link: relative links, anchors, and pinned GitHub permalinks to code (file exists at the commit, line range in bounds). `--verbose` shows the first line of each range; `--pin <sha>` requires one commit. | [Contributor guide](../AGENTS.md#validation) |

Python commands run as modules rather than scripts; see [`src/README.md`](../src/README.md).