from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from suseoro.api.app import create_app
from suseoro.backup.service import (
    BackupService,
    LocalAdminConfirmationRequired,
    RestoreVerificationError,
    select_retained_backups,
)
from suseoro.config import Settings
from suseoro.db.connection import connect
from suseoro.db.migrations import apply_migrations
from suseoro.repositories.auth import UserRecord, issue_session

NOW = "2026-08-28T00:00:00.000000Z"


def _database(data_dir: Path) -> Settings:
    settings = Settings(data_dir=data_dir)
    with connect(settings.database_path) as connection:
        apply_migrations(connection)
        connection.execute(
            "INSERT INTO schools (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
            ("550e8400-e29b-41d4-a716-446655440100", "백업 학교", NOW, NOW),
        )
        connection.commit()
    connection.close()
    return settings


def _school_name(settings: Settings) -> str:
    with connect(settings.database_path) as connection:
        name = connection.execute("SELECT name FROM schools").fetchone()["name"]
    connection.close()
    return name


def test_online_backup_writes_verified_manifest_and_matching_checksum(
    data_dir: Path,
) -> None:
    settings = _database(data_dir)
    service = BackupService(settings.database_path, settings.backups_dir)

    manifest = service.create(kind="daily", now=datetime(2026, 8, 28, tzinfo=UTC))

    manifest_json = json.loads(manifest.manifest_path.read_text(encoding="utf-8"))
    assert manifest.verified is True
    assert manifest.kind == "daily"
    assert manifest.database_path.is_file()
    assert (
        hashlib.sha256(manifest.database_path.read_bytes()).hexdigest()
        == manifest.sha256
    )
    assert manifest_json["sha256"] == manifest.sha256
    assert manifest_json["verified"] is True
    assert manifest_json["schema_migrations"]
    assert all(item["checksum"] for item in manifest_json["schema_migrations"])
    with connect(manifest.database_path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    connection.close()


def test_retention_keeps_daily_7_weekly_4_monthly_12_buckets() -> None:
    newest = datetime(2026, 8, 28, 12, tzinfo=UTC)
    backups = [
        {
            "id": f"backup-{index:03d}",
            "created_at": newest - timedelta(days=index),
        }
        for index in range(430)
    ]

    retained = select_retained_backups(backups)
    retained_ids = {item["id"] for item in retained}

    assert {f"backup-{index:03d}" for index in range(7)}.issubset(retained_ids)
    assert len({item["created_at"].isocalendar()[:2] for item in retained}) >= 4
    assert (
        len({(item["created_at"].year, item["created_at"].month) for item in retained})
        >= 12
    )
    assert len(retained) <= 23


def test_restore_requires_local_admin_confirmation_and_creates_pre_restore_backup(
    data_dir: Path,
) -> None:
    settings = _database(data_dir)
    service = BackupService(settings.database_path, settings.backups_dir)
    manifest = service.create(kind="daily", now=datetime(2026, 8, 28, tzinfo=UTC))
    with connect(settings.database_path) as connection:
        connection.execute("UPDATE schools SET name = '변경된 학교'")
        connection.commit()
    connection.close()

    with pytest.raises(LocalAdminConfirmationRequired):
        service.restore(
            manifest.manifest_path,
            confirmation_token="secret",
            expected_confirmation_token="secret",
            local_request=False,
        )
    with pytest.raises(LocalAdminConfirmationRequired):
        service.restore(
            manifest.manifest_path,
            confirmation_token="wrong",
            expected_confirmation_token="secret",
            local_request=True,
        )

    restored = service.restore(
        manifest.manifest_path,
        confirmation_token="secret",
        expected_confirmation_token="secret",
        local_request=True,
        now=datetime(2026, 8, 29, tzinfo=UTC),
    )

    assert restored.pre_restore_backup.kind == "pre_restore"
    assert restored.pre_restore_backup.verified is True
    assert _school_name(settings) == "백업 학교"


def test_checksum_or_schema_failure_leaves_current_database_byte_for_byte_unchanged(
    data_dir: Path,
) -> None:
    settings = _database(data_dir)
    service = BackupService(settings.database_path, settings.backups_dir)
    manifest = service.create(kind="daily", now=datetime(2026, 8, 28, tzinfo=UTC))
    with connect(settings.database_path) as connection:
        connection.execute("UPDATE schools SET name = '현재 학교'")
        connection.commit()
    connection.close()
    before = settings.database_path.read_bytes()
    manifest.database_path.write_bytes(
        manifest.database_path.read_bytes() + b"tampered"
    )

    with pytest.raises(RestoreVerificationError, match="checksum"):
        service.restore(
            manifest.manifest_path,
            confirmation_token="secret",
            expected_confirmation_token="secret",
            local_request=True,
        )

    assert settings.database_path.read_bytes() == before
    assert _school_name(settings) == "현재 학교"


def test_manifest_checksum_itself_is_verified_before_restore(data_dir: Path) -> None:
    settings = _database(data_dir)
    service = BackupService(settings.database_path, settings.backups_dir)
    manifest = service.create(kind="daily", now=datetime(2026, 8, 28, tzinfo=UTC))
    data = json.loads(manifest.manifest_path.read_text(encoding="utf-8"))
    data["sha256"] = "0" * 64
    manifest.manifest_path.write_text(json.dumps(data), encoding="utf-8")
    before = settings.database_path.read_bytes()

    with pytest.raises(RestoreVerificationError):
        service.restore(
            manifest.manifest_path,
            confirmation_token="secret",
            expected_confirmation_token="secret",
            local_request=True,
        )

    assert settings.database_path.read_bytes() == before


def test_restore_api_replays_without_creating_a_second_pre_restore_backup(
    data_dir: Path,
) -> None:
    settings = _database(data_dir)
    service = BackupService(settings.database_path, settings.backups_dir)
    # The actor/session exists only in the live database, not in the restore target.
    # Restore completion and replay must therefore not depend on restored FK rows.
    manifest = service.create(kind="daily")
    user_id = "550e8400-e29b-41d4-a716-446655440101"
    with connect(settings.database_path) as connection:
        connection.execute(
            """
            INSERT INTO users (
                id, school_id, username, password_hash, display_name,
                created_at, updated_at
            ) VALUES (?, ?, 'operator', 'hash', '담당자', ?, ?)
            """,
            (user_id, "550e8400-e29b-41d4-a716-446655440100", NOW, NOW),
        )
        connection.execute(
            """
            INSERT INTO user_roles (school_id, user_id, role, created_at)
            VALUES (?, ?, 'OPERATOR', ?)
            """,
            ("550e8400-e29b-41d4-a716-446655440100", user_id, NOW),
        )
        issued = issue_session(
            connection,
            UserRecord(
                id=user_id,
                school_id="550e8400-e29b-41d4-a716-446655440100",
                username="operator",
                display_name="담당자",
                roles=("OPERATOR",),
            ),
            3_600,
        )
        connection.commit()
    connection.close()
    app = create_app(settings)
    headers = {
        "X-CSRF-Token": issued.csrf_token,
        "X-Request-ID": "550e8400-e29b-41d4-a716-446655440102",
        "Idempotency-Key": "restore-once",
        "X-Local-Admin-Confirmation": app.state.local_admin_confirmation_token,
    }
    client = TestClient(app, base_url="https://testserver")
    client.cookies.set("suseoro_session", issued.session_token)
    client.cookies.set("suseoro_csrf", issued.csrf_token)

    with client:
        first = client.post(
            "/api/v2/admin/restores",
            headers=headers,
            json={"manifest_file": manifest.manifest_path.name},
        )
        second = client.post(
            "/api/v2/admin/restores",
            headers=headers,
            json={"manifest_file": manifest.manifest_path.name},
        )

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert len([item for item in service.list() if item.kind == "pre_restore"]) == 1


def _rewrite_manifest_for_database(manifest) -> None:
    with sqlite3.connect(manifest.database_path) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        schema = [
            {"version": row[0], "checksum": row[1]}
            for row in connection.execute(
                "SELECT migration_id, checksum FROM schema_migrations ORDER BY migration_id"
            )
        ]
    data = json.loads(manifest.manifest_path.read_text(encoding="utf-8"))
    data["sha256"] = hashlib.sha256(manifest.database_path.read_bytes()).hexdigest()
    data["size_bytes"] = manifest.database_path.stat().st_size
    data["schema_migrations"] = schema
    encoded = json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2).encode(
        "utf-8"
    )
    manifest.manifest_path.write_bytes(encoded)
    manifest.manifest_path.with_suffix(
        manifest.manifest_path.suffix + ".sha256"
    ).write_text(hashlib.sha256(encoded).hexdigest(), encoding="ascii")


@pytest.mark.parametrize("mutation", ["future", "altered"])
def test_restore_rejects_future_or_altered_bundled_migration_history(
    data_dir: Path, mutation: str
) -> None:
    settings = _database(data_dir)
    service = BackupService(settings.database_path, settings.backups_dir)
    manifest = service.create(kind="daily")
    with sqlite3.connect(manifest.database_path) as connection:
        if mutation == "future":
            connection.execute(
                "INSERT INTO schema_migrations VALUES ('9999_future', ?, ?)",
                ("f" * 64, NOW),
            )
        else:
            migration_id = connection.execute(
                "SELECT migration_id FROM schema_migrations ORDER BY migration_id LIMIT 1"
            ).fetchone()[0]
            connection.execute(
                "UPDATE schema_migrations SET checksum = ? WHERE migration_id = ?",
                ("a" * 64, migration_id),
            )
        connection.commit()
    _rewrite_manifest_for_database(manifest)

    with pytest.raises(RestoreVerificationError, match="migration"):
        service.restore(
            manifest.manifest_path,
            confirmation_token="secret",
            expected_confirmation_token="secret",
            local_request=True,
        )


def test_restore_safely_upgrades_a_compatible_older_backup_before_swap(
    data_dir: Path, tmp_path: Path
) -> None:
    settings = _database(data_dir)
    old_database = tmp_path / "old.sqlite3"
    old_migrations = tmp_path / "old-migrations"
    old_migrations.mkdir()
    bundled = Path(__file__).parents[1] / "src" / "suseoro" / "db" / "migrations"
    migrations = sorted(bundled.glob("*.sql"))
    for migration in migrations[:-1]:
        shutil.copy2(migration, old_migrations / migration.name)
    with connect(old_database) as connection:
        apply_migrations(connection, old_migrations)
        connection.commit()
    old_service = BackupService(old_database, settings.backups_dir)
    manifest = old_service.create(kind="upgrade")

    BackupService(settings.database_path, settings.backups_dir).restore(
        manifest.manifest_path,
        confirmation_token="secret",
        expected_confirmation_token="secret",
        local_request=True,
    )

    with connect(settings.database_path) as connection:
        applied = [
            row["migration_id"]
            for row in connection.execute(
                "SELECT migration_id FROM schema_migrations ORDER BY migration_id"
            )
        ]
    assert applied == [migration.stem for migration in migrations]


def test_concurrent_restore_operation_replays_once_and_makes_one_prebackup(
    data_dir: Path,
) -> None:
    settings = _database(data_dir)
    service = BackupService(settings.database_path, settings.backups_dir)
    manifest = service.create(kind="daily")

    def restore_once():
        return service.restore(
            manifest.manifest_path,
            confirmation_token="secret",
            expected_confirmation_token="secret",
            local_request=True,
            operation_key="same-restore-request",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = list(executor.map(lambda _: restore_once(), range(2)))

    assert first == second
    assert len([item for item in service.list() if item.kind == "pre_restore"]) == 1


def test_backup_kind_is_constrained_and_retention_is_enforced_on_create(
    data_dir: Path,
) -> None:
    settings = _database(data_dir)
    service = BackupService(settings.database_path, settings.backups_dir)
    with pytest.raises(ValueError, match="kind"):
        service.create(kind="../../escape")

    start = datetime(2026, 1, 1, tzinfo=UTC)
    for day in range(40):
        service.create(kind="daily", now=start + timedelta(days=day))

    manifests = service.list()
    retained = select_retained_backups([manifest.to_dict() for manifest in manifests])
    assert {item.id for item in manifests} == {item["id"] for item in retained}
