"""Checksum-protected SQLite schema migration runner."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


class MigrationChecksumMismatch(RuntimeError):
    """Raised when an already-applied migration has been changed."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


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
            applied_at TEXT NOT NULL
        )
        """
    )


def apply_migrations(
    connection: sqlite3.Connection, migrations_dir: Path | None = None
) -> None:
    """Apply pending SQL files and refuse checksum changes to migration history."""
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
        try:
            connection.executescript(
                "BEGIN IMMEDIATE;\n" + contents + "\n" + ledger_insert + "\nCOMMIT;"
            )
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
