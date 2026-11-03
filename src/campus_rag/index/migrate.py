"""Apply numbered SQL migrations to the local PostgreSQL database.

Run with: python -m campus_rag.index.migrate
"""

import hashlib
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import psycopg

from campus_rag.config import load_settings

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
ADVISORY_LOCK_KEY = 7_262_025_001  # arbitrary fixed key that serializes concurrent migrators
_FILENAME = re.compile(r"^(\d{3,})_([a-z0-9_]+)\.sql$")

EXIT_CONNECTION_FAILED = 2
EXIT_MIGRATION_FAILED = 3


class MigrationError(Exception):
    pass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str
    checksum: str

    @property
    def filename(self) -> str:
        return f"{self.version:03d}_{self.name}.sql"


def discover_migrations(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    """Read NNN_snake_name.sql files from `directory`, sorted by version."""
    by_version: dict[int, Migration] = {}
    for path in sorted(directory.glob("*.sql")):
        match = _FILENAME.match(path.name)
        if not match:
            raise MigrationError(
                f"invalid migration filename {path.name}; expected NNN_snake_name.sql "
                "(3+ digit version, lowercase letters, digits, underscores)"
            )
        version = int(match.group(1))
        if version in by_version:
            raise MigrationError(
                f"duplicate migration version {version}: {by_version[version].filename} "
                f"and {path.name}"
            )
        data = path.read_bytes()
        by_version[version] = Migration(
            version=version,
            name=match.group(2),
            sql=data.decode("utf-8"),
            checksum=hashlib.sha256(data).hexdigest(),
        )
    return [by_version[v] for v in sorted(by_version)]


def apply_migrations(conn: psycopg.Connection, migrations: Sequence[Migration]) -> list[Migration]:
    """Apply pending migrations in order and return the ones newly applied.

    `conn` must be in autocommit mode; each migration runs in its own transaction.
    """
    if not conn.autocommit:
        raise MigrationError("apply_migrations requires an autocommit connection")
    conn.execute("SELECT pg_advisory_lock(%s)", (ADVISORY_LOCK_KEY,))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version integer PRIMARY KEY, "
            "name text NOT NULL, "
            "checksum text NOT NULL, "
            "applied_at timestamptz NOT NULL DEFAULT now())"
        )
        applied = {
            version: (name, checksum)
            for version, name, checksum in conn.execute(
                "SELECT version, name, checksum FROM schema_migrations"
            )
        }
        known = {m.version: m for m in migrations}
        for version, (name, checksum) in sorted(applied.items()):
            migration = known.get(version)
            if migration is None:
                raise MigrationError(
                    f"database has applied migration {version:03d}_{name}.sql "
                    "but the file is missing; restore it or reset the database"
                )
            if migration.checksum != checksum:
                raise MigrationError(
                    f"applied migration {migration.filename} was modified; "
                    "restore it or reset the database"
                )

        newly_applied: list[Migration] = []
        for migration in migrations:
            if migration.version in applied:
                continue
            try:
                with conn.transaction():
                    conn.execute(migration.sql)  # type: ignore[arg-type]
                    conn.execute(
                        "INSERT INTO schema_migrations (version, name, checksum) "
                        "VALUES (%s, %s, %s)",
                        (migration.version, migration.name, migration.checksum),
                    )
            except psycopg.Error as exc:
                raise MigrationError(
                    f"migration {migration.filename} failed: {_first_line(exc)}"
                ) from exc
            newly_applied.append(migration)
        return newly_applied
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_KEY,))


def _first_line(exc: Exception) -> str:
    return (str(exc).strip().splitlines() or ["unknown error"])[0]


def _describe_target(database_url: str) -> str:
    parts = urlsplit(database_url)
    return f"{parts.hostname}:{parts.port}/{parts.path.lstrip('/')}"


def main() -> int:
    settings = load_settings()
    try:
        conn = psycopg.connect(settings.database_url, autocommit=True, connect_timeout=5)
    except psycopg.OperationalError as exc:
        reason = _first_line(exc)
        print(
            f"ERROR: cannot connect to PostgreSQL at {_describe_target(settings.database_url)} "
            f"({reason}). Start it with: docker compose up -d --wait db",
            file=sys.stderr,
        )
        return EXIT_CONNECTION_FAILED

    try:
        with conn:
            migrations = discover_migrations()
            applied = apply_migrations(conn, migrations)
    except MigrationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_MIGRATION_FAILED
    except psycopg.Error as exc:
        print(f"ERROR: database error: {_first_line(exc)}", file=sys.stderr)
        return EXIT_MIGRATION_FAILED

    if applied:
        names = ", ".join(m.filename for m in applied)
        print(f"Applied {len(applied)} migration(s): {names}")
    else:
        print(f"Database is up to date ({len(migrations)} migration(s) applied).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
