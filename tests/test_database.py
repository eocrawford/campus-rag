import psycopg
import pytest

from campus_rag.config import Settings
from campus_rag.index import migrate
from campus_rag.index.migrate import MigrationError, apply_migrations, discover_migrations

pytestmark = pytest.mark.db


def test_migrations_apply_on_fresh_database(db_url: str) -> None:
    migrations = discover_migrations()
    with psycopg.connect(db_url, autocommit=True) as conn:
        applied = apply_migrations(conn, migrations)
        assert [m.version for m in applied] == [m.version for m in migrations]
        rows = conn.execute("SELECT version, name, checksum FROM schema_migrations").fetchall()
        assert rows == [(m.version, m.name, m.checksum) for m in migrations]


def test_second_run_applies_nothing(db_url: str) -> None:
    migrations = discover_migrations()
    with psycopg.connect(db_url, autocommit=True) as conn:
        apply_migrations(conn, migrations)
        assert apply_migrations(conn, migrations) == []


def test_pgvector_works(db_url: str) -> None:
    with psycopg.connect(db_url, autocommit=True) as conn:
        apply_migrations(conn, discover_migrations())
        row = conn.execute("SELECT '[1,2,3]'::vector <=> '[1,2,4]'::vector").fetchone()
        assert row is not None
        assert isinstance(row[0], float)


def test_modified_applied_migration_raises(db_url: str) -> None:
    migrations = discover_migrations()
    with psycopg.connect(db_url, autocommit=True) as conn:
        apply_migrations(conn, migrations)
        conn.execute("UPDATE schema_migrations SET checksum = 'tampered' WHERE version = 1")
        with pytest.raises(MigrationError, match="001_enable_pgvector.sql was modified"):
            apply_migrations(conn, migrations)


def test_unknown_applied_version_raises(db_url: str) -> None:
    migrations = discover_migrations()
    with psycopg.connect(db_url, autocommit=True) as conn:
        apply_migrations(conn, migrations)
        conn.execute(
            "INSERT INTO schema_migrations (version, name, checksum) VALUES (99, 'x', 'y')"
        )
        with pytest.raises(MigrationError, match="099_x.sql"):
            apply_migrations(conn, migrations)


def test_failed_migration_raises_and_rolls_back(db_url: str, tmp_path) -> None:
    (tmp_path / "001_broken.sql").write_text("CREATE TABLE t (id int); SELECT bogus;")
    with psycopg.connect(db_url, autocommit=True) as conn:
        with pytest.raises(MigrationError, match=r"migration 001_broken\.sql failed: .*bogus"):
            apply_migrations(conn, discover_migrations(tmp_path))
        assert conn.execute("SELECT count(*) FROM schema_migrations").fetchone() == (0,)
        assert conn.execute("SELECT to_regclass('t')").fetchone() == (None,)


def test_main_reports_failed_migration(db_url: str, tmp_path, monkeypatch, capsys) -> None:
    (tmp_path / "001_broken.sql").write_text("SELECT bogus;")
    monkeypatch.setattr(migrate, "load_settings", lambda: Settings(database_url=db_url))
    monkeypatch.setattr(migrate, "discover_migrations", lambda: discover_migrations(tmp_path))
    assert migrate.main() == migrate.EXIT_MIGRATION_FAILED
    err = capsys.readouterr().err
    assert err.startswith("ERROR: migration 001_broken.sql failed:")
    assert "Traceback" not in err
