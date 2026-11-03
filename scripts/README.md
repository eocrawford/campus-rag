# scripts

Repository entry-point scripts. Each script is documented in a runbook under
[`runbooks/`](../runbooks/AGENTS.md).

| Script | Purpose | Runbook |
|--------|---------|---------|
| `setup-local-environment.ps1` | Set up and verify the local environment: Python, PostgreSQL with pgvector, the Ollama embedding server and model, migrations, and tests. | [Local environment setup](../runbooks/local-environment-setup.md) |

Python commands run as modules rather than scripts; see [`src/README.md`](../src/README.md).