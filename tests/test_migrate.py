import hashlib
from pathlib import Path

import pytest

from campus_rag.index.migrate import MigrationError, discover_migrations


def write(directory: Path, name: str, sql: str = "SELECT 1;") -> None:
    (directory / name).write_text(sql, encoding="utf-8")


def test_discovers_in_version_order(tmp_path: Path) -> None:
    write(tmp_path, "010_later.sql")
    write(tmp_path, "002_second.sql")
    write(tmp_path, "001_first.sql")
    migrations = discover_migrations(tmp_path)
    assert [m.version for m in migrations] == [1, 2, 10]
    assert [m.name for m in migrations] == ["first", "second", "later"]


def test_checksum_is_sha256_of_file_bytes(tmp_path: Path) -> None:
    write(tmp_path, "001_first.sql", "SELECT 42;")
    (migration,) = discover_migrations(tmp_path)
    assert migration.checksum == hashlib.sha256(b"SELECT 42;").hexdigest()
    assert migration.sql == "SELECT 42;"


@pytest.mark.parametrize("name", ["1_short.sql", "001-dash.sql", "001_Upper.sql", "readme.sql"])
def test_invalid_filename_raises(tmp_path: Path, name: str) -> None:
    write(tmp_path, name)
    with pytest.raises(MigrationError, match="invalid migration filename"):
        discover_migrations(tmp_path)


def test_duplicate_version_raises(tmp_path: Path) -> None:
    write(tmp_path, "001_a.sql")
    write(tmp_path, "0001_b.sql")
    with pytest.raises(MigrationError, match="duplicate migration version 1"):
        discover_migrations(tmp_path)


def test_packaged_migrations_are_valid() -> None:
    migrations = discover_migrations()
    assert migrations[0].filename == "001_enable_pgvector.sql"
