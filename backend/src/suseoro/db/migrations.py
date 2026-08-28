"""Checksum-protected SQLite schema migration runner."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from suseoro.db.connection import install_fts_protection, trusted_fts_maintenance


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
    install_fts_protection(connection)


def _apply_migrations_trusted(
    connection: sqlite3.Connection, migrations_dir: Path | None = None
) -> None:
    """Apply pending SQL files and refuse checksum changes to migration history."""
    connection.set_authorizer(None)
    _ensure_migration_ledger(connection)
    directory = migrations_dir or _migration_directory()

    for migration_path in sorted(directory.glob("*.sql")):
        migration_id = migration_path.stem
        # Letter-suffixed hardening migrations depend on their committed base.
        # Upgrade-fixture directories intentionally omit newer bases; defer this
        # migration until the base is supplied by the real bundled directory.
        if (
            migration_id == "0006a_api_hardening"
            and not (directory / "0006_api_operations.sql").is_file()
        ):
            continue
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


def apply_migrations(
    connection: sqlite3.Connection, migrations_dir: Path | None = None
) -> None:
    try:
        with trusted_fts_maintenance(connection):
            _apply_migrations_trusted(connection, migrations_dir)
    finally:
        install_fts_protection(connection)
