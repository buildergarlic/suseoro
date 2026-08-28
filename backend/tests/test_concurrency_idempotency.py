from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from suseoro.api.dependencies import require_if_match
from suseoro.config import Settings
from suseoro.db.connection import connect
from suseoro.db.migrations import apply_migrations
from suseoro.security.passwords import hash_password
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
OTHER_SCHOOL_ID = "550e8400-e29b-41d4-a716-446655440204"
OUTSIDER_ID = "550e8400-e29b-41d4-a716-446655440205"
NOW = "2026-08-28T12:34:56Z"


def _seed_database(settings: Settings) -> None:
    with connect(settings.database_path) as connection:
        apply_migrations(connection)
        connection.execute(
            "INSERT INTO schools (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (SCHOOL_ID, "Concurrency school", NOW, NOW),
        )
        connection.execute(
            "INSERT INTO schools (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (OTHER_SCHOOL_ID, "Other school", NOW, NOW),
        )
        password_hash = hash_password(f"Aa1!{secrets.token_urlsafe(18)}")
        for user_id, username in ((ACTOR_ID, "actor"), (OTHER_ACTOR_ID, "other")):
            connection.execute(
                """
                INSERT INTO users (
                    id, school_id, username, password_hash, display_name,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    SCHOOL_ID,
                    username,
                    password_hash,
                    username.title(),
                    NOW,
                    NOW,
                ),
            )
            connection.execute(
                """
                INSERT INTO user_roles (school_id, user_id, role, created_at)
                VALUES (?, ?, 'OPERATOR', ?)
                """,
                (SCHOOL_ID, user_id, NOW),
            )
        connection.execute(
            """
            INSERT INTO users (
                id, school_id, username, password_hash, display_name,
                created_at, updated_at
            ) VALUES (?, ?, 'outsider', ?, 'Outsider', ?, ?)
            """,
            (OUTSIDER_ID, OTHER_SCHOOL_ID, password_hash, NOW, NOW),
        )
        connection.execute(
            """
            INSERT INTO user_roles (school_id, user_id, role, created_at)
            VALUES (?, ?, 'OPERATOR', ?)
            """,
            (OTHER_SCHOOL_ID, OUTSIDER_ID, NOW),
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


def test_same_idempotency_key_and_request_returns_completed_response(
    data_dir: Path,
) -> None:
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


def test_concurrent_duplicate_reservation_returns_in_progress_then_replays(
    data_dir: Path,
) -> None:
    """A real second connection must not leak SQLite busy errors for a duplicate key."""
    settings = Settings(data_dir=data_dir)
    _seed_database(settings)
    first_connection = connect(settings.database_path)
    second_connection = connect(settings.database_path)
    second_connection.execute("PRAGMA busy_timeout=25")
    try:
        reserve_idempotency_key(
            first_connection,
            school_id=SCHOOL_ID,
            actor_id=ACTOR_ID,
            route="POST /api/v2/workspaces",
            key="concurrent-key",
            request_body={"name": "Concurrent"},
        )
        with pytest.raises(IdempotencyConflict) as caught:
            reserve_idempotency_key(
                second_connection,
                school_id=SCHOOL_ID,
                actor_id=ACTOR_ID,
                route="POST /api/v2/workspaces",
                key="concurrent-key",
                request_body={"name": "Concurrent"},
            )
        assert caught.value.detail["code"] == "IDEMPOTENCY_REQUEST_IN_PROGRESS"

        complete_idempotent_request(
            first_connection,
            school_id=SCHOOL_ID,
            actor_id=ACTOR_ID,
            route="POST /api/v2/workspaces",
            key="concurrent-key",
            status=201,
            body={"id": WORKSPACE_ID},
        )
        first_connection.commit()
        replay = reserve_idempotency_key(
            second_connection,
            school_id=SCHOOL_ID,
            actor_id=ACTOR_ID,
            route="POST /api/v2/workspaces",
            key="concurrent-key",
            request_body={"name": "Concurrent"},
        )
    finally:
        first_connection.rollback()
        second_connection.rollback()
        first_connection.close()
        second_connection.close()

    assert replay is not None
    assert replay.status == 201
    assert replay.body == {"id": WORKSPACE_ID}


def test_if_match_dependency_requires_and_parses_integer_etags() -> None:
    """Missing or malformed If-Match values must fail before a mutation handler runs."""
    app = FastAPI()

    @app.patch("/version-probe")
    def version_probe(
        version: Annotated[int, Depends(require_if_match)],
    ) -> dict[str, int]:
        return {"version": version}

    with TestClient(app) as client:
        missing = client.patch("/version-probe")
        malformed = [
            client.patch("/version-probe", headers={"If-Match": value})
            for value in (
                '"3',
                '3"',
                'W/"3"',
                "+3",
                "-1",
                '" 3"',
                '"3 "',
                " 3 ",
                ' "3" ',
                "3, 4",
                "3junk",
                "03",
                '"03"',
            )
        ]
        accepted = [
            client.patch("/version-probe", headers={"If-Match": value})
            for value in ("0", '"0"', "3", '"3"')
        ]

    assert missing.status_code == 428
    assert missing.json()["detail"]["code"] == "IF_MATCH_REQUIRED"
    assert all(response.status_code == 400 for response in malformed)
    assert all(
        response.json()["detail"]["code"] == "INVALID_IF_MATCH"
        for response in malformed
    )
    assert [response.status_code for response in accepted] == [200, 200, 200, 200]
    assert [response.json() for response in accepted] == [
        {"version": 0},
        {"version": 0},
        {"version": 3},
        {"version": 3},
    ]


def test_if_match_dependency_directly_rejects_whitespace_variants() -> None:
    """Parser validation must not depend on HTTP framework whitespace handling."""
    for value in (
        " 3",
        "3 ",
        "\t3",
        "3\t",
        "3 0",
        ' "3"',
        '"3" ',
        '" 3"',
        '"3 "',
        '"3 0"',
    ):
        with pytest.raises(HTTPException) as caught:
            require_if_match(if_match=value)

        assert caught.value.status_code == 400
        assert caught.value.detail["code"] == "INVALID_IF_MATCH"


def test_stale_if_match_returns_structured_412_without_updating(data_dir: Path) -> None:
    """An update that ignores stale row_version must fail this test."""
    settings = Settings(data_dir=data_dir)
    _seed_database(settings)
    with connect(settings.database_path) as connection:
        with pytest.raises(VersionConflict) as caught:
            update_with_version(
                connection,
                table="acquisition_workspaces",
                school_id=SCHOOL_ID,
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


def test_versioned_update_does_not_reveal_or_change_another_school_row(
    data_dir: Path,
) -> None:
    """Looking up by globally known ID must not cross the caller's school boundary."""
    settings = Settings(data_dir=data_dir)
    _seed_database(settings)
    with connect(settings.database_path) as connection:
        with pytest.raises(HTTPException) as caught:
            update_with_version(
                connection,
                table="acquisition_workspaces",
                school_id=OTHER_SCHOOL_ID,
                entity_id=WORKSPACE_ID,
                submitted_version=3,
                changes={"name": "Cross-school edit"},
            )
        row = connection.execute(
            "SELECT name, row_version FROM acquisition_workspaces WHERE id = ?",
            (WORKSPACE_ID,),
        ).fetchone()

    assert caught.value.status_code == 404
    assert caught.value.detail == {"code": "ENTITY_NOT_FOUND"}
    assert dict(row) == {"name": "Original", "row_version": 3}


def test_versioned_update_rejects_tenant_identity_change_before_sql(
    data_dir: Path,
) -> None:
    """Allowing school_id in changes must not move or partially update a tenant row."""
    settings = Settings(data_dir=data_dir)
    _seed_database(settings)
    with connect(settings.database_path) as connection:
        with pytest.raises(ValueError, match="school_id"):
            update_with_version(
                connection,
                table="acquisition_workspaces",
                school_id=SCHOOL_ID,
                entity_id=WORKSPACE_ID,
                submitted_version=3,
                changes={
                    "school_id": OTHER_SCHOOL_ID,
                    "name": "Moved across schools",
                },
            )
        row = connection.execute(
            """
            SELECT school_id, name, row_version
            FROM acquisition_workspaces WHERE id = ?
            """,
            (WORKSPACE_ID,),
        ).fetchone()
        moved_count = connection.execute(
            """
            SELECT COUNT(*) FROM acquisition_workspaces
            WHERE id = ? AND school_id = ?
            """,
            (WORKSPACE_ID, OTHER_SCHOOL_ID),
        ).fetchone()[0]

    assert dict(row) == {
        "school_id": SCHOOL_ID,
        "name": "Original",
        "row_version": 3,
    }
    assert moved_count == 0


def test_successful_versioned_update_increments_row_version(data_dir: Path) -> None:
    """Updating data without atomically incrementing row_version must fail this test."""
    settings = Settings(data_dir=data_dir)
    _seed_database(settings)
    with connect(settings.database_path) as connection:
        updated = update_with_version(
            connection,
            table="acquisition_workspaces",
            school_id=SCHOOL_ID,
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


def test_edit_lock_normalizes_mixed_width_timestamp_before_expiry_comparison(
    data_dir: Path,
) -> None:
    """A fractional instant after a whole-second expiry must replace a legacy lock."""
    settings = Settings(data_dir=data_dir)
    _seed_database(settings)
    with connect(settings.database_path) as connection:
        connection.execute(
            """
            INSERT INTO edit_locks (
                school_id, entity_type, entity_id, actor_id, acquired_at, expires_at
            ) VALUES (?, 'acquisition_workspace', ?, ?, ?, ?)
            """,
            (
                SCHOOL_ID,
                WORKSPACE_ID,
                ACTOR_ID,
                "2026-08-28T12:00:00Z",
                "2026-08-28T12:02:00Z",
            ),
        )
        replacement = acquire_edit_lock(
            connection,
            school_id=SCHOOL_ID,
            entity_type="acquisition_workspace",
            entity_id=WORKSPACE_ID,
            actor_id=OTHER_ACTOR_ID,
            now=datetime(2026, 8, 28, 12, 2, 0, 500000, tzinfo=UTC),
        )
        stored = connection.execute(
            "SELECT acquired_at, expires_at FROM edit_locks"
        ).fetchone()

    assert replacement.actor_id == OTHER_ACTOR_ID
    assert stored["acquired_at"] == "2026-08-28T12:02:00.500000Z"
    assert stored["expires_at"] == "2026-08-28T12:04:00.500000Z"


def test_edit_lock_rejects_actor_without_role_in_target_school(data_dir: Path) -> None:
    """A valid user ID from another school must not acquire this school's lock."""
    settings = Settings(data_dir=data_dir)
    _seed_database(settings)
    with connect(settings.database_path) as connection:
        with pytest.raises(HTTPException) as caught:
            acquire_edit_lock(
                connection,
                school_id=SCHOOL_ID,
                entity_type="acquisition_workspace",
                entity_id=WORKSPACE_ID,
                actor_id=OUTSIDER_ID,
                now=datetime(2026, 8, 28, 12, 0, tzinfo=UTC),
            )
        count = connection.execute("SELECT COUNT(*) FROM edit_locks").fetchone()[0]

    assert caught.value.status_code == 403
    assert caught.value.detail == {"code": "EDIT_LOCK_ROLE_REQUIRED"}
    assert count == 0
