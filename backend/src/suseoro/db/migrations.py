"""Checksum-protected SQLite schema migration runner."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from suseoro.db.connection import install_fts_protection, trusted_fts_maintenance


class MigrationChecksumMismatch(RuntimeError):
    """Raised when an already-applied migration has been changed."""


_ATOMIC_MIGRATION_BUNDLES = (
    (
        "0010a_task8_round4_upgrade_prelude",
        "0011_task8_round3_integrity",
        "0012_task8_round4_integrity",
    ),
)


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


def _migration_source(path: Path) -> tuple[str, str]:
    contents = path.read_text(encoding="utf-8")
    checksum = hashlib.sha256(contents.encode("utf-8")).hexdigest()
    return contents, checksum


def _applied_checksum(connection: sqlite3.Connection, migration_id: str) -> str | None:
    row = connection.execute(
        "SELECT checksum FROM schema_migrations WHERE migration_id = ?",
        (migration_id,),
    ).fetchone()
    return None if row is None else str(row["checksum"])


def _ledger_insert(migration_id: str, checksum: str) -> str:
    return (
        "INSERT INTO schema_migrations (migration_id, checksum, applied_at) VALUES ("
        f"{_sql_literal(migration_id)}, {_sql_literal(checksum)}, "
        f"{_sql_literal(_utc_now())});"
    )


def _validate_applied_checksum(
    migration_id: str, expected_checksum: str, applied_checksum: str | None
) -> None:
    if applied_checksum is not None and applied_checksum != expected_checksum:
        raise MigrationChecksumMismatch(
            f"Migration checksum changed for {migration_id}"
        )


def _apply_atomic_bundle(
    connection: sqlite3.Connection, migration_paths: tuple[Path, ...]
) -> None:
    migrations: list[tuple[str, str, str, str | None]] = []
    for migration_path in migration_paths:
        migration_id = migration_path.stem
        contents, checksum = _migration_source(migration_path)
        applied_checksum = _applied_checksum(connection, migration_id)
        _validate_applied_checksum(migration_id, checksum, applied_checksum)
        migrations.append((migration_id, contents, checksum, applied_checksum))

    pending = [migration for migration in migrations if migration[3] is None]
    if not pending:
        return
    bundle_ids = tuple(migration[0] for migration in migrations)
    if bundle_ids == _ATOMIC_MIGRATION_BUNDLES[0]:
        pending_ids = {migration[0] for migration in pending}
        execution: list[tuple[str, str, str, str | None]] = []
        # 0011 needs the prelude whenever it is newly applied. Any newly
        # discovered earlier member must finish with the idempotent postlude,
        # even when that postlude is already recorded, so the claim guard can
        # never be left dropped by an out-of-order historical ledger.
        if bundle_ids[0] in pending_ids or bundle_ids[1] in pending_ids:
            execution.append(migrations[0])
        if bundle_ids[1] in pending_ids:
            execution.append(migrations[1])
        execution.append(migrations[2])
    else:
        execution = pending
    script = ["BEGIN IMMEDIATE;"]
    for migration_id, contents, checksum, applied_checksum in execution:
        script.append(contents)
        if applied_checksum is None:
            script.append(_ledger_insert(migration_id, checksum))
    script.append("COMMIT;")
    try:
        connection.executescript("\n".join(script))
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise


def _apply_migrations_trusted(
    connection: sqlite3.Connection, migrations_dir: Path | None = None
) -> None:
    """Apply pending SQL files and refuse checksum changes to migration history."""
    connection.set_authorizer(None)
    _ensure_migration_ledger(connection)
    directory = migrations_dir or _migration_directory()

    migration_paths = tuple(sorted(directory.glob("*.sql")))
    paths_by_id = {path.stem: path for path in migration_paths}
    # Validate every present applied migration before executing anything. This
    # includes bundle members whose application is deferred until all guard
    # files are available.
    for migration_path in migration_paths:
        migration_id = migration_path.stem
        _, checksum = _migration_source(migration_path)
        _validate_applied_checksum(
            migration_id,
            checksum,
            _applied_checksum(connection, migration_id),
        )
    bundles_by_id: dict[str, tuple[Path, ...]] = {}
    deferred_bundle_ids: set[str] = set()
    for bundle in _ATOMIC_MIGRATION_BUNDLES:
        if all(migration_id in paths_by_id for migration_id in bundle):
            bundle_paths = tuple(paths_by_id[migration_id] for migration_id in bundle)
            for migration_id in bundle:
                bundles_by_id[migration_id] = bundle_paths
        elif bundle[0] in paths_by_id or bundle[-1] in paths_by_id:
            # An old release can legitimately contain the committed middle
            # migration alone. Once either new guard file is present, applying
            # only part of the bundle could strand the legacy trigger dropped
            # or run the correction before its required column exists.
            deferred_bundle_ids.update(
                migration_id for migration_id in bundle if migration_id in paths_by_id
            )
    handled_bundle_ids: set[str] = set()

    for migration_path in migration_paths:
        migration_id = migration_path.stem
        if migration_id in deferred_bundle_ids:
            continue
        bundle_paths = bundles_by_id.get(migration_id)
        if bundle_paths is not None:
            bundle_ids = {path.stem for path in bundle_paths}
            if migration_id in handled_bundle_ids:
                continue
            _apply_atomic_bundle(connection, bundle_paths)
            handled_bundle_ids.update(bundle_ids)
            continue
        # Letter-suffixed hardening migrations depend on their committed base.
        # Upgrade-fixture directories intentionally omit newer bases; defer this
        # migration until the base is supplied by the real bundled directory.
        if (
            migration_id == "0006a_api_hardening"
            and not (directory / "0006_api_operations.sql").is_file()
        ):
            continue
        contents, checksum = _migration_source(migration_path)
        applied_checksum = _applied_checksum(connection, migration_id)

        if applied_checksum is not None:
            _validate_applied_checksum(migration_id, checksum, applied_checksum)
            continue

        ledger_insert = _ledger_insert(migration_id, checksum)
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
