"""Checksum-protected SQLite schema migration runner."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path


class MigrationChecksumMismatch(RuntimeError):
    """Raised when an already-applied migration has been changed."""


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _migration_directory() -> Path:
    return Path(__file__).with_name("migrations")


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _ensure_migration_ledger(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            migration_id TEXT PRIMARY KEY,
            checksum TEXT NOT NULL,
            applied_at TEXT NOT NULL CHECK (
                (
                    applied_at GLOB '????-??-??T??:??:??Z'
                    OR applied_at GLOB '????-??-??T??:??:??.[0-9]*Z'
                )
                AND applied_at NOT GLOB '*[^0-9T:.Z-]*'
                AND strftime('%Y-%m-%dT%H:%M:%S', applied_at) = substr(applied_at, 1, 19)
            )
        )
        """
    )


def _protect_internal_fts(connection: sqlite3.Connection) -> None:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'holding_search_fts_index'"
    ).fetchone()
    if exists is None:
        return
    allowed_triggers = {
        "normalized_works_fts_insert",
        "normalized_works_fts_update",
        "normalized_works_fts_delete",
    }

    def authorize(action, table, _column, _database, source):
        writes = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}
        if (
            action in writes
            and table
            and table == "holding_search_fts_index"
            and source not in allowed_triggers
        ):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    connection.set_authorizer(authorize)


def apply_migrations(
    connection: sqlite3.Connection, migrations_dir: Path | None = None
) -> None:
    """Apply pending SQL files and refuse checksum changes to migration history."""
    connection.set_authorizer(None)
    _ensure_migration_ledger(connection)
    directory = migrations_dir or _migration_directory()

    for migration_path in sorted(directory.glob("*.sql")):
        migration_id = migration_path.stem
        contents = migration_path.read_text(encoding="utf-8")
        checksum = hashlib.sha256(contents.encode("utf-8")).hexdigest()
        applied = connection.execute(
            "SELECT checksum FROM schema_migrations WHERE migration_id = ?",
            (migration_id,),
        ).fetchone()

        if applied is not None:
            if applied["checksum"] != checksum:
                raise MigrationChecksumMismatch(
                    f"Migration checksum changed for {migration_id}"
                )
            continue

        ledger_insert = (
            "INSERT INTO schema_migrations (migration_id, checksum, applied_at) VALUES ("
            f"{_sql_literal(migration_id)}, {_sql_literal(checksum)}, "
            f"{_sql_literal(_utc_now())});"
        )
        rebuilds_foreign_key_parents = "PRAGMA defer_foreign_keys = ON;" in contents
        try:
            if rebuilds_foreign_key_parents:
                connection.execute("PRAGMA foreign_keys=OFF")
                connection.executescript("BEGIN IMMEDIATE;\n" + contents)
                violations = connection.execute("PRAGMA foreign_key_check").fetchall()
                if violations:
                    raise sqlite3.IntegrityError(
                        f"foreign key violations after {migration_id}: {violations!r}"
                    )
                connection.execute(ledger_insert)
                connection.commit()
            else:
                connection.executescript(
                    "BEGIN IMMEDIATE;\n" + contents + "\n" + ledger_insert + "\nCOMMIT;"
                )
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            if rebuilds_foreign_key_parents:
                connection.execute("PRAGMA foreign_keys=ON")
    _protect_internal_fts(connection)
