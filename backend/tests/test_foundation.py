from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from suseoro.api.app import create_app
from suseoro.config import Settings
from suseoro.db.connection import connect
from suseoro.db.migrations import MigrationChecksumMismatch, apply_migrations


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
