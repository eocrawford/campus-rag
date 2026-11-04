"""Tests for scripts/canonicalize_lock.py (no network: PyPI lookups are faked)."""

# The sample lock reproduces uv.lock's one-line file entries, which exceed the line limit.
# ruff: noqa: E501

import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "canonicalize_lock", Path(__file__).resolve().parent.parent / "scripts" / "canonicalize_lock.py"
)
assert _SPEC and _SPEC.loader
canonicalize_lock = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(canonicalize_lock)

SHA_A = "a" * 64
SHA_B = "b" * 64
MIRROR = "https://mirror.example.com/pypi"

LOCK = f"""version = 1
requires-python = ">=3.12"

[[package]]
name = "demo-pkg"
version = "1.2.0"
source = {{ registry = "{MIRROR}/simple/" }}
sdist = {{ url = "{MIRROR}/download/demo-pkg/1.2.0/demo_pkg-1.2.0.tar.gz", hash = "sha256:{SHA_A}", size = 100, upload-time = "2020-01-01T00:00:00Z" }}
wheels = [
    {{ url = "{MIRROR}/download/demo-pkg/1.2.0/demo_pkg-1.2.0-py3-none-any.whl", hash = "sha256:{SHA_B}", size = 50, upload-time = "2020-01-01T00:00:00Z" }},
]

[[package]]
name = "campus-rag"
version = "0.1.0"
source = {{ editable = "." }}
"""


def fake_lookup(name: str, version: str) -> dict[str, dict]:
    assert (name, version) == ("demo-pkg", "1.2.0")
    base = "https://files.pythonhosted.org/packages/xx/yy"
    return {
        "demo_pkg-1.2.0.tar.gz": {
            "url": f"{base}/demo_pkg-1.2.0.tar.gz",
            "digests": {"sha256": SHA_A},
            "size": 100,
            "upload_time_iso_8601": "2024-05-01T12:00:00.000000Z",
        },
        "demo_pkg-1.2.0-py3-none-any.whl": {
            "url": f"{base}/demo_pkg-1.2.0-py3-none-any.whl",
            "digests": {"sha256": SHA_B},
            "size": 50,
            "upload_time_iso_8601": "2024-05-01T12:00:01.000000Z",
        },
    }


def test_rewrites_registry_and_file_urls_to_pypi() -> None:
    result, rewritten = canonicalize_lock.canonicalize(LOCK, fake_lookup)

    assert rewritten == 2
    assert "mirror.example.com" not in result
    assert 'registry = "https://pypi.org/simple"' in result
    assert "https://files.pythonhosted.org/packages/xx/yy/demo_pkg-1.2.0.tar.gz" in result
    assert 'upload-time = "2024-05-01T12:00:00.000000Z"' in result
    assert 'source = { editable = "." }' in result
    assert canonicalize_lock.non_canonical_urls(result) == []


def test_already_canonical_lock_is_unchanged() -> None:
    canonical, _ = canonicalize_lock.canonicalize(LOCK, fake_lookup)
    again, rewritten = canonicalize_lock.canonicalize(canonical, fake_lookup)
    assert again == canonical
    assert rewritten == 0


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [("sha256", "c" * 64, "sha256"), ("size", 999, "size")],
)
def test_mismatch_with_pypi_is_refused(field: str, value: object, message: str) -> None:
    def lookup(name: str, version: str) -> dict[str, dict]:
        files = fake_lookup(name, version)
        record = files["demo_pkg-1.2.0.tar.gz"]
        if field == "sha256":
            record["digests"]["sha256"] = value
        else:
            record["size"] = value
        return files

    with pytest.raises(canonicalize_lock.CanonicalizeError, match=message):
        canonicalize_lock.canonicalize(LOCK, lookup)


def test_file_missing_from_pypi_is_refused() -> None:
    with pytest.raises(canonicalize_lock.CanonicalizeError, match="not published"):
        canonicalize_lock.canonicalize(LOCK, lambda name, version: {})


def test_main_leaves_file_unchanged_on_error(tmp_path: Path, monkeypatch, capsys) -> None:
    lock = tmp_path / "uv.lock"
    lock.write_text(LOCK, encoding="utf-8")
    monkeypatch.setattr(canonicalize_lock, "pypi_files", lambda name, version: {})
    monkeypatch.setattr(
        canonicalize_lock,
        "canonicalize",
        lambda text: (_ for _ in ()).throw(canonicalize_lock.CanonicalizeError("boom")),
    )

    assert canonicalize_lock.main([str(lock)]) == 1
    assert lock.read_text(encoding="utf-8") == LOCK
    assert "unchanged" in capsys.readouterr().err
