from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from suseoro.config import Settings
from suseoro.db.connection import connect
from suseoro.db.migrations import apply_migrations
from suseoro.services.concurrency import (
    VersionConflict,
    acquire_edit_lock,
    update_with_version,
)
from suseoro.services.idempotency import (
    IdempotencyConflict,
    complete_idempotent_request,
    reserve_idempotency_key,
)

SCHOOL_ID = "550e8400-e29b-41d4-a716-446655440200"
ACTOR_ID = "550e8400-e29b-41d4-a716-446655440201"
OTHER_ACTOR_ID = "550e8400-e29b-41d4-a716-446655440202"
WORKSPACE_ID = "550e8400-e29b-41d4-a716-446655440203"
NOW = "2026-08-28T12:34:56Z"


def _seed_database(settings: Settings) -> None:
    with connect(settings.database_path) as connection:
        apply_migrations(connection)
        connection.execute(
            "INSERT INTO schools (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (SCHOOL_ID, "Concurrency school", NOW, NOW),
        )
        for user_id, username in ((ACTOR_ID, "actor"), (OTHER_ACTOR_ID, "other")):
            connection.execute(
                """
                INSERT INTO users (
                    id, school_id, username, password_hash, display_name,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'unused', ?, ?, ?)
                """,
                (user_id, SCHOOL_ID, username, username.title(), NOW, NOW),
            )
        connection.execute(
            """
            INSERT INTO acquisition_workspaces (
                id, school_id, name, status, row_version, created_by_user_id,
                created_at, updated_at
            ) VALUES (?, ?, 'Original', 'DRAFT', 3, ?, ?, ?)
            """,
            (WORKSPACE_ID, SCHOOL_ID, ACTOR_ID, NOW, NOW),
        )
        connection.commit()


def test_same_idempotency_key_and_request_returns_completed_response(data_dir: Path) -> None:
    """Executing a completed duplicate instead of replaying it must fail this test."""
    settings = Settings(data_dir=data_dir)
    _seed_database(settings)
    with connect(settings.database_path) as connection:
        first = reserve_idempotency_key(
            connection,
            school_id=SCHOOL_ID,
            actor_id=ACTOR_ID,
            route="POST /api/v2/workspaces",
            key="client-key-1",
            request_body={"name": "Admissions"},
        )
        assert first is None
        complete_idempotent_request(
            connection,
            school_id=SCHOOL_ID,
            actor_id=ACTOR_ID,
            route="POST /api/v2/workspaces",
            key="client-key-1",
            status=201,
            body={"id": WORKSPACE_ID, "name": "Admissions"},
        )
        replay = reserve_idempotency_key(
            connection,
            school_id=SCHOOL_ID,
            actor_id=ACTOR_ID,
            route="POST /api/v2/workspaces",
            key="client-key-1",
            request_body={"name": "Admissions"},
        )

    assert replay is not None
    assert replay.status == 201
    assert replay.body == {"id": WORKSPACE_ID, "name": "Admissions"}


def test_same_idempotency_key_with_different_request_is_409(data_dir: Path) -> None:
    """Reusing a key for changed payload without a 409 must fail this test."""
    settings = Settings(data_dir=data_dir)
    _seed_database(settings)
    with connect(settings.database_path) as connection:
        reserve_idempotency_key(
            connection,
            school_id=SCHOOL_ID,
            actor_id=ACTOR_ID,
            route="POST /api/v2/workspaces",
            key="client-key-1",
            request_body={"name": "Admissions"},
        )
        with pytest.raises(IdempotencyConflict) as caught:
            reserve_idempotency_key(
                connection,
                school_id=SCHOOL_ID,
                actor_id=ACTOR_ID,
                route="POST /api/v2/workspaces",
                key="client-key-1",
                request_body={"name": "Changed"},
            )

    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_stale_if_match_returns_structured_412_without_updating(data_dir: Path) -> None:
    """An update that ignores stale row_version must fail this test."""
    settings = Settings(data_dir=data_dir)
    _seed_database(settings)
    with connect(settings.database_path) as connection:
        with pytest.raises(VersionConflict) as caught:
            update_with_version(
                connection,
                table="acquisition_workspaces",
                entity_id=WORKSPACE_ID,
                submitted_version=2,
                changes={"name": "Stale edit"},
            )
        row = connection.execute(
            "SELECT name, row_version FROM acquisition_workspaces WHERE id = ?",
            (WORKSPACE_ID,),
        ).fetchone()

    assert caught.value.status_code == 412
    assert caught.value.detail == {
        "code": "ROW_VERSION_CONFLICT",
        "current": 3,
        "submitted": 2,
    }
    assert dict(row) == {"name": "Original", "row_version": 3}


def test_successful_versioned_update_increments_row_version(data_dir: Path) -> None:
    """Updating data without atomically incrementing row_version must fail this test."""
    settings = Settings(data_dir=data_dir)
    _seed_database(settings)
    with connect(settings.database_path) as connection:
        updated = update_with_version(
            connection,
            table="acquisition_workspaces",
            entity_id=WORKSPACE_ID,
            submitted_version=3,
            changes={"name": "Current edit"},
        )

    assert updated["name"] == "Current edit"
    assert updated["row_version"] == 4


def test_edit_lock_expires_after_exactly_two_minutes(data_dir: Path) -> None:
    """A stale lease blocking another editor beyond two minutes must fail this test."""
    settings = Settings(data_dir=data_dir)
    _seed_database(settings)
    acquired_at = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
    with connect(settings.database_path) as connection:
        first = acquire_edit_lock(
            connection,
            school_id=SCHOOL_ID,
            entity_type="acquisition_workspace",
            entity_id=WORKSPACE_ID,
            actor_id=ACTOR_ID,
            now=acquired_at,
        )
        replacement = acquire_edit_lock(
            connection,
            school_id=SCHOOL_ID,
            entity_type="acquisition_workspace",
            entity_id=WORKSPACE_ID,
            actor_id=OTHER_ACTOR_ID,
            now=acquired_at + timedelta(minutes=2),
        )

    assert first.expires_at == acquired_at + timedelta(minutes=2)
    assert replacement.actor_id == OTHER_ACTOR_ID
    assert replacement.expires_at == acquired_at + timedelta(minutes=4)
