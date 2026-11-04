# Runbook: update dependencies

**Goal.** Add, remove, or upgrade a Python dependency and commit a `uv.lock` that works for
everyone, including from a network that only reaches PyPI through a package mirror.

## The scripts

| Script | What it is for |
|--------|----------------|
| [`update-lock-via-mirror.ps1`](../scripts/update-lock-via-mirror.ps1) | Updates `uv.lock` after a `pyproject.toml` change on a network that blocks `files.pythonhosted.org`. It locks a temporary copy of the project through the mirror, rewrites the result to canonical PyPI URLs, copies it back, and verifies it. Run this one. |
| [`canonicalize_lock.py`](../scripts/canonicalize_lock.py) | The rewriting step the PowerShell script calls. It replaces every mirror registry and download URL in a `uv.lock` with the canonical `pypi.org` or `files.pythonhosted.org` URL, but only after confirming each file's sha256 and size match the release on pypi.org. It works with any PyPI mirror and leaves the lock unchanged if any check fails. |

Neither script is needed on a network that can reach PyPI directly. Neither stores a mirror
address: the mirror comes from `-PackageIndexUrl` or your local pip configuration.

## On a network that can reach PyPI

1. Edit `pyproject.toml`, or run `uv add <package>` / `uv remove <package>`.
2. Run `uv lock` (`uv add` and `uv remove` do this for you), then `uv sync`.
3. Run lint and tests:
   ```powershell
   uv run ruff check src tests scripts
   $env:CAMPUS_RAG_REQUIRE_DB='1'; uv run pytest
   ```
4. Commit `pyproject.toml` and `uv.lock` together.

## On a network that blocks PyPI's file host

Some networks block `files.pythonhosted.org` and require a PyPI mirror. Locking against the mirror
directly would write the mirror's URLs into `uv.lock`, which breaks installs for everyone else, so
lock in a temporary copy instead and rewrite the URLs:

1. Edit `pyproject.toml`.
2. Re-lock through the mirror:
   ```powershell
   .\scripts\update-lock-via-mirror.ps1 -PackageIndexUrl <mirror simple URL>
   ```
   Without `-PackageIndexUrl`, the script uses `global.index-url` from `pip config list`. It locks a
   temporary copy of the project against the mirror, then
   [`canonicalize_lock.py`](../scripts/canonicalize_lock.py) rewrites every registry and download
   URL to its canonical `pypi.org` or `files.pythonhosted.org` URL. A file is rewritten only when
   its sha256, and its size when recorded, match the release on pypi.org, so a mirror cannot
   substitute different bytes. The script prints the packages added and removed, copies the lock
   back, and checks it with `uv lock --check --offline`.
3. Install the new lock from the mirror, with hashes enforced:
   ```powershell
   .\scripts\setup-local-environment.ps1 -PackageIndexUrl <mirror simple URL>
   ```
4. Run lint and tests, then commit `pyproject.toml` and `uv.lock` together.

When nothing changed, the script reports that `uv.lock` is already up to date and leaves it alone.

## Rules

- Never run `uv lock` in the repository with `UV_DEFAULT_INDEX` or a mirror index configured.
- Never commit a lock that names a mirror. `canonicalize_lock.py` refuses to finish while any
  registry or file URL points elsewhere than PyPI.
- The build backend is `uv_build`, which uv provides itself, so rebuilding the project after a
  `pyproject.toml` change needs no download. Keep its version range current when moving to a new uv
  minor version.

## Troubleshooting

| Message | Cause | Fix |
|---------|-------|-----|
| `FAILED: No package mirror is configured in pip.` | No `-PackageIndexUrl` and no `global.index-url` in pip's configuration. | Pass `-PackageIndexUrl <mirror simple URL>`. |
| `FAILED: uv lock failed against the mirror.` | The mirror URL is wrong or unreachable, or `pyproject.toml` has an unsatisfiable requirement. | Check the URL and the requirement, then rerun. |
| `ERROR: <package> <version>: <file> is not published on pypi.org` | The mirror serves a file that PyPI does not. | Pin a version that PyPI publishes, or ask the mirror's owners. The repository is unchanged. |
| `ERROR: ... sha256 ... differs from pypi.org` | The mirror's file differs from PyPI's. | Do not use that mirror for this package; report it. The repository is unchanged. |
| `FAILED: uv lock --check failed after the update.` | The new lock does not match `pyproject.toml`. | `git checkout -- uv.lock`, then report the problem. |
