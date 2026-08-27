from __future__ import annotations

import sqlite3
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from suseoro.api.app import create_app
from suseoro.config import Settings
from suseoro.db.connection import connect
from suseoro.db.migrations import MigrationChecksumMismatch, apply_migrations


FOUNDATION_TABLES = {
    "schools",
    "users",
    "user_roles",
    "sessions",
    "acquisition_workspaces",
    "idempotency_keys",
    "audit_events",
    "durable_jobs",
}

FOUNDATION_INDEXES = {
    "idx_users_school_id",
    "idx_user_roles_user_id",
    "idx_sessions_user_id",
    "idx_sessions_expires_at",
    "idx_acquisition_workspaces_school_status",
    "idx_idempotency_keys_created_at",
    "idx_audit_events_school_occurred_at",
    "idx_audit_events_entity",
    "idx_durable_jobs_school_status",
    "idx_durable_jobs_workspace_id",
}

VALID_UUID = "550e8400-e29b-41d4-a716-446655440000"
VALID_TIMESTAMP = "2026-08-28T12:34:56Z"


def test_settings_create_all_application_data_paths(data_dir: Path) -> None:
    """Removing directory creation would leave the app unable to persist files."""
    settings = Settings(data_dir=data_dir)

    assert settings.data_dir == data_dir
    assert settings.database_path == data_dir / "db" / "suseoro.sqlite3"
    assert settings.sources_dir == data_dir / "sources"
    assert settings.exports_dir == data_dir / "exports"
    assert settings.backups_dir == data_dir / "backups"
    assert all(
        path.is_dir()
        for path in (
            settings.data_dir,
            settings.database_path.parent,
            settings.sources_dir,
            settings.exports_dir,
            settings.backups_dir,
        )
    )


def test_sqlite_connection_applies_required_pragmas(data_dir: Path) -> None:
    """Removing any per-connection pragma would weaken durability or integrity."""
    settings = Settings(data_dir=data_dir)

    with connect(settings.database_path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 1


def test_migrations_are_idempotent_and_reject_changed_history(
    data_dir: Path, tmp_path: Path
) -> None:
    """Changing an applied migration must be detected instead of silently drifting schema."""
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    migration = migrations_dir / "0001_example.sql"
    migration.write_text("CREATE TABLE examples (id TEXT PRIMARY KEY);", encoding="utf-8")

    settings = Settings(data_dir=data_dir)
    with connect(settings.database_path) as connection:
        apply_migrations(connection, migrations_dir)
        first = connection.execute(
            "SELECT migration_id, checksum, applied_at FROM schema_migrations"
        ).fetchall()
        apply_migrations(connection, migrations_dir)
        second = connection.execute(
            "SELECT migration_id, checksum, applied_at FROM schema_migrations"
        ).fetchall()

        assert len(first) == 1
        assert second == first

        migration.write_text(
            "CREATE TABLE examples (id TEXT PRIMARY KEY, value TEXT);", encoding="utf-8"
        )
        with pytest.raises(MigrationChecksumMismatch, match="0001_example"):
            apply_migrations(connection, migrations_dir)


def test_foundation_migration_creates_required_tables_and_indexes(data_dir: Path) -> None:
    """Removing a ledger table or lookup index would break later workflow queries."""
    settings = Settings(data_dir=data_dir)

    with connect(settings.database_path) as connection:
        apply_migrations(connection)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }

    assert FOUNDATION_TABLES <= tables
    assert FOUNDATION_INDEXES <= indexes


@pytest.mark.parametrize(
    ("column", "invalid_value"),
    [
        ("id", "not-a-uuid"),
        ("id", None),
        ("created_at", "2026-08-28 12:34:56+00:00"),
        ("created_at", "2026-02-30T12:34:56Z"),
        ("updated_at", "not-a-timestamp"),
    ],
)
def test_foundation_migration_rejects_invalid_uuid_and_utc_timestamp_values(
    data_dir: Path, column: str, invalid_value: str | None
) -> None:
    """Removing ledger format checks would permit invalid IDs or non-UTC timestamps."""
    values = {
        "id": VALID_UUID,
        "name": "Format validation school",
        "created_at": VALID_TIMESTAMP,
        "updated_at": VALID_TIMESTAMP,
    }
    values[column] = invalid_value
    settings = Settings(data_dir=data_dir)

    with connect(settings.database_path) as connection:
        apply_migrations(connection)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO schools (id, name, created_at, updated_at)
                VALUES (:id, :name, :created_at, :updated_at)
                """,
                values,
            )


def test_foundation_constraints_upgrade_preserves_valid_linked_data(
    data_dir: Path, tmp_path: Path
) -> None:
    """The forward validation migration must preserve valid 0001 ledger records."""
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    source_migrations = Path(__file__).parents[1] / "src" / "suseoro" / "db" / "migrations"
    shutil.copy2(source_migrations / "0001_foundation.sql", migrations_dir)

    settings = Settings(data_dir=data_dir)
    with connect(settings.database_path) as connection:
        apply_migrations(connection, migrations_dir)
        connection.execute(
            """
            INSERT INTO schools (id, name, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (VALID_UUID, "Upgrade school", VALID_TIMESTAMP, VALID_TIMESTAMP),
        )
        connection.execute(
            """
            INSERT INTO users (
                id, school_id, username, password_hash, display_name,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "550e8400-e29b-41d4-a716-446655440001",
                VALID_UUID,
                "operator",
                "hash",
                "Operator",
                VALID_TIMESTAMP,
                VALID_TIMESTAMP,
            ),
        )

        shutil.copy2(
            source_migrations / "0001a_foundation_constraints.sql", migrations_dir
        )
        apply_migrations(connection, migrations_dir)

        school = connection.execute(
            "SELECT name FROM schools WHERE id = ?", (VALID_UUID,)
        ).fetchone()
        user = connection.execute(
            "SELECT username FROM users WHERE id = ?",
            ("550e8400-e29b-41d4-a716-446655440001",),
        ).fetchone()

        assert school[0] == "Upgrade school"
        assert user[0] == "operator"
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO schools (id, name, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                ("not-a-uuid", "Invalid", VALID_TIMESTAMP, VALID_TIMESTAMP),
            )


def test_forward_constraints_reject_invalid_legacy_data_without_data_loss(
    data_dir: Path, tmp_path: Path
) -> None:
    """Invalid historical values must block the validation migration, not be discarded."""
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    source_migrations = Path(__file__).parents[1] / "src" / "suseoro" / "db" / "migrations"
    shutil.copy2(source_migrations / "0001_foundation.sql", migrations_dir)

    settings = Settings(data_dir=data_dir)
    with connect(settings.database_path) as connection:
        apply_migrations(connection, migrations_dir)
        connection.execute(
            """
            INSERT INTO schools (id, name, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            ("not-a-uuid", "Legacy school", VALID_TIMESTAMP, VALID_TIMESTAMP),
        )
        shutil.copy2(
            source_migrations / "0001a_foundation_constraints.sql", migrations_dir
        )

        with pytest.raises(sqlite3.IntegrityError):
            apply_migrations(connection, migrations_dir)

        assert connection.execute("SELECT name FROM schools").fetchone()[0] == "Legacy school"
        assert [
            row[0]
            for row in connection.execute(
                "SELECT migration_id FROM schema_migrations"
            ).fetchall()
        ] == ["0001_foundation"]


def test_health_reports_safe_readiness_without_data_path(data_dir: Path) -> None:
    """Health must expose readiness but never leak deployment-specific storage details."""
    app = create_app(Settings(data_dir=data_dir))

    with TestClient(app) as client:
        response = client.get("/api/v2/health")

    assert response.status_code == 200
    assert response.json() == {
        "version": "0.1.0",
        "database": "ready",
        "worker": "not_started",
    }
    assert str(data_dir) not in response.text
