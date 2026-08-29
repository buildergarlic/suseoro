from __future__ import annotations

import shutil
import sqlite3
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from workflow_fixtures import NOW, make_workflow_fixture

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
    "upload_idempotency_claims",
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
    migration.write_text(
        "CREATE TABLE examples (id TEXT PRIMARY KEY);", encoding="utf-8"
    )

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


def test_foundation_migration_creates_required_tables_and_indexes(
    data_dir: Path,
) -> None:
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


def test_upload_claim_forward_migration_has_durable_generation_and_lease_fencing(
    data_dir: Path,
) -> None:
    """Dropping durable fingerprint or generation fields would reopen upload takeover races."""
    settings = Settings(data_dir=data_dir)

    with connect(settings.database_path) as connection:
        apply_migrations(connection)
        columns = {
            row[1]: {"type": row[2], "not_null": bool(row[3])}
            for row in connection.execute(
                "PRAGMA table_info(upload_idempotency_claims)"
            ).fetchall()
        }
        indexes = {
            row[1]: bool(row[2])
            for row in connection.execute(
                "PRAGMA index_list(upload_idempotency_claims)"
            ).fetchall()
        }

    assert {
        "request_fingerprint",
        "generation",
        "lease_owner",
        "lease_expires_at",
        "state",
        "response_status",
        "response_body",
        "request_metadata_json",
    } <= columns.keys()
    assert columns["request_fingerprint"] == {"type": "TEXT", "not_null": True}
    assert columns["generation"] == {"type": "INTEGER", "not_null": True}
    assert any(unique for unique in indexes.values())


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
    source_migrations = (
        Path(__file__).parents[1] / "src" / "suseoro" / "db" / "migrations"
    )
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


def test_0007_adds_replay_metadata_without_rewriting_populated_claims(
    tmp_path: Path,
) -> None:
    """The replay-bound migration must preserve every committed 0006b claim."""
    current_dir = Path(__file__).parents[1] / "src" / "suseoro" / "db" / "migrations"
    old_dir = tmp_path / "0006b-migrations"
    old_dir.mkdir()
    for source in current_dir.glob("*.sql"):
        if source.stem < "0007_upload_replay_metadata":
            shutil.copy2(source, old_dir / source.name)

    database_path = tmp_path / "0006b-upgrade.sqlite3"
    school_id = VALID_UUID
    actor_id = "550e8400-e29b-41d4-a716-446655440001"
    claim_id = "550e8400-e29b-41d4-a716-446655440002"
    with connect(database_path) as connection:
        apply_migrations(connection, old_dir)
        connection.execute(
            "INSERT INTO schools (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (school_id, "업그레이드 학교", VALID_TIMESTAMP, VALID_TIMESTAMP),
        )
        connection.execute(
            """
            INSERT INTO users (
                id, school_id, username, password_hash, display_name,
                created_at, updated_at
            ) VALUES (?, ?, 'operator', 'hash', '담당자', ?, ?)
            """,
            (actor_id, school_id, VALID_TIMESTAMP, VALID_TIMESTAMP),
        )
        connection.execute(
            """
            INSERT INTO upload_idempotency_claims (
                id, school_id, actor_id, route, key, request_fingerprint,
                generation, state, response_status, response_body,
                created_at, updated_at
            ) VALUES (?, ?, ?, 'POST /sources', 'stable-key', ?, 3, 'COMPLETED',
                      202, '{"items":[]}', ?, ?)
            """,
            (
                claim_id,
                school_id,
                actor_id,
                "a" * 64,
                VALID_TIMESTAMP,
                VALID_TIMESTAMP,
            ),
        )
        connection.commit()

        shutil.copy2(
            current_dir / "0007_upload_replay_metadata.sql",
            old_dir / "0007_upload_replay_metadata.sql",
        )
        apply_migrations(connection, old_dir)
        preserved = connection.execute(
            """
            SELECT id, request_fingerprint, generation, state, response_status,
                   response_body, request_metadata_json
            FROM upload_idempotency_claims WHERE id = ?
            """,
            (claim_id,),
        ).fetchone()
        latest = connection.execute(
            "SELECT migration_id FROM schema_migrations ORDER BY migration_id DESC LIMIT 1"
        ).fetchone()[0]

    assert dict(preserved) == {
        "id": claim_id,
        "request_fingerprint": "a" * 64,
        "generation": 3,
        "state": "COMPLETED",
        "response_status": 202,
        "response_body": '{"items":[]}',
        "request_metadata_json": None,
    }
    assert latest == "0007_upload_replay_metadata"


def test_0008_preserves_populated_0007_claims_and_backfills_failed_items(
    tmp_path: Path,
) -> None:
    """The repair migration must preserve claims and surface old unresolved failures."""
    current_dir = Path(__file__).parents[1] / "src" / "suseoro" / "db" / "migrations"
    old_dir = tmp_path / "0007-migrations"
    old_dir.mkdir()
    for source in current_dir.glob("*.sql"):
        if source.stem < "0008_upload_repair_and_job_discovery":
            shutil.copy2(source, old_dir / source.name)

    database_path = tmp_path / "0007-upgrade.sqlite3"
    school_id = VALID_UUID
    actor_id = "550e8400-e29b-41d4-a716-446655440001"
    workspace_id = "550e8400-e29b-41d4-a716-446655440002"
    claim_id = "550e8400-e29b-41d4-a716-446655440003"
    legacy_key_id = "550e8400-e29b-41d4-a716-446655440004"
    route = f"POST /api/v2/workspaces/{workspace_id}/sources"
    response_body = (
        '{"items":[{"error":{"code":"UNSUPPORTED_FILE_TYPE",'
        '"message":"unsupported"},"filename":"legacy.exe",'
        '"source_id":null,"status":"FAILED"}],"job_id":null}'
    )
    metadata = '{"files":[{"filename":"legacy.exe","size_bytes":9}]}'
    with connect(database_path) as connection:
        apply_migrations(connection, old_dir)
        connection.execute(
            "INSERT INTO schools (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (school_id, "업그레이드 학교", VALID_TIMESTAMP, VALID_TIMESTAMP),
        )
        connection.execute(
            """
            INSERT INTO users (
                id, school_id, username, password_hash, display_name,
                created_at, updated_at
            ) VALUES (?, ?, 'operator', 'hash', '담당자', ?, ?)
            """,
            (actor_id, school_id, VALID_TIMESTAMP, VALID_TIMESTAMP),
        )
        connection.execute(
            """
            INSERT INTO acquisition_workspaces (
                id, school_id, name, status, created_by_user_id, created_at, updated_at
            ) VALUES (?, ?, '이전 부분 접수', 'DRAFT', ?, ?, ?)
            """,
            (
                workspace_id,
                school_id,
                actor_id,
                VALID_TIMESTAMP,
                VALID_TIMESTAMP,
            ),
        )
        connection.execute(
            """
            INSERT INTO upload_idempotency_claims (
                id, school_id, actor_id, route, key, request_fingerprint,
                generation, state, response_status, response_body,
                created_at, updated_at, request_metadata_json
            ) VALUES (?, ?, ?, ?, 'legacy-partial', ?, 1, 'COMPLETED', 207,
                      ?, ?, ?, ?)
            """,
            (
                claim_id,
                school_id,
                actor_id,
                route,
                "a" * 64,
                response_body,
                VALID_TIMESTAMP,
                VALID_TIMESTAMP,
                metadata,
            ),
        )
        connection.execute(
            """
            INSERT INTO idempotency_keys (
                id, school_id, actor_id, route, key, request_hash,
                response_status, response_body, created_at
            ) VALUES (?, ?, ?, ?, 'pre-claim-partial', ?, 207, ?, ?)
            """,
            (
                legacy_key_id,
                school_id,
                actor_id,
                route,
                "b" * 64,
                response_body.replace("legacy.exe", "pre-claim.exe"),
                VALID_TIMESTAMP,
            ),
        )
        connection.commit()

        apply_migrations(connection, current_dir)
        preserved = connection.execute(
            "SELECT response_status, response_body, request_metadata_json FROM upload_idempotency_claims WHERE id = ?",
            (claim_id,),
        ).fetchone()
        obligation = connection.execute(
            """
            SELECT workspace_id, upload_claim_id, filename, content_sha256,
                   size_bytes, role, status, error_code
            FROM upload_repair_obligations WHERE upload_claim_id = ?
            """,
            (claim_id,),
        ).fetchone()
        legacy_obligation = connection.execute(
            """
            SELECT workspace_id, upload_claim_id, filename, role, status, error_code
            FROM upload_repair_obligations WHERE filename = 'pre-claim.exe'
            """
        ).fetchone()
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        latest = connection.execute(
            "SELECT migration_id FROM schema_migrations ORDER BY migration_id DESC LIMIT 1"
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "DELETE FROM upload_idempotency_claims WHERE id = ?", (claim_id,)
            )
        connection.rollback()

    assert dict(preserved) == {
        "response_status": 207,
        "response_body": response_body,
        "request_metadata_json": metadata,
    }
    assert dict(obligation) == {
        "workspace_id": workspace_id,
        "upload_claim_id": claim_id,
        "filename": "legacy.exe",
        "content_sha256": None,
        "size_bytes": 9,
        "role": "UNKNOWN",
        "status": "UNRESOLVED",
        "error_code": "UNSUPPORTED_FILE_TYPE",
    }
    assert dict(legacy_obligation) == {
        "workspace_id": workspace_id,
        "upload_claim_id": None,
        "filename": "pre-claim.exe",
        "role": "UNKNOWN",
        "status": "UNRESOLVED",
        "error_code": "UNSUPPORTED_FILE_TYPE",
    }
    assert foreign_keys == []
    assert latest == "0012_task8_round4_integrity"


def test_0005a_upgrades_c714_state_and_disposition_values_without_data_loss(
    tmp_path: Path,
) -> None:
    current_dir = Path(__file__).parents[1] / "src" / "suseoro" / "db" / "migrations"
    old_dir = tmp_path / "c714348-migrations"
    old_dir.mkdir()
    for source in current_dir.glob("*.sql"):
        if source.stem not in {
            "0005a_workflow_contract",
            "0005b_analysis_completion_guard",
            "0006_api_operations",
            "0008_upload_repair_and_job_discovery",
            "0009_repair_recovery_transition",
            "0010_source_correction_recovery",
            "0010a_task8_round4_upgrade_prelude",
            "0011_task8_round3_integrity",
            "0012_task8_round4_integrity",
        }:
            shutil.copy2(source, old_dir / source.name)
    fixture = make_workflow_fixture(
        tmp_path / "upgrade",
        state="AWAITING_DELIVERY",
        migrations_dir=old_dir,
    )
    candidate_id = fixture.add_candidate(
        title="보존 책",
        author="저자",
        isbn="9788937464010",
        quantity=1,
        unit_price=10_000,
    )
    recommendation_id = fixture.connection.execute(
        "SELECT recommendation_id FROM candidate_decisions WHERE id = ?",
        (candidate_id,),
    ).fetchone()["recommendation_id"]
    aliases = {
        "DATA_PREPARATION": "DRAFT",
        "COMPARING": "ANALYZING",
        "REVISION_REQUESTED": "CHANGES_REQUESTED",
        "QUOTE_ADJUSTMENT": "QUOTE_REVIEW",
        "AWAITING_DELIVERY": "ORDER_SENT",
        "COMPLETE": "COMPLETED",
    }
    workspace_ids = {"AWAITING_DELIVERY": fixture.workspace_id}
    for old_state in aliases:
        if old_state == "AWAITING_DELIVERY":
            continue
        workspace_id = str(uuid.uuid4())
        workspace_ids[old_state] = workspace_id
        fixture.connection.execute(
            """
            INSERT INTO acquisition_workspaces (
                id, school_id, name, status, created_by_user_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                workspace_id,
                fixture.school_id,
                f"upgrade-{old_state}",
                old_state,
                fixture.operator_id,
                NOW,
                NOW,
            ),
        )
    approval_id = str(uuid.uuid4())
    approval_row_id = str(uuid.uuid4())
    quote_id = str(uuid.uuid4())
    quote_row_id = str(uuid.uuid4())
    zero_quote_row_id = str(uuid.uuid4())
    artifact_id = str(uuid.uuid4())
    order_id = str(uuid.uuid4())
    order_row_id = str(uuid.uuid4())
    difference_id = str(uuid.uuid4())
    fixture.connection.execute(
        """
        INSERT INTO approval_revisions (
            id, school_id, workspace_id, revision_number, canonical_json, sha256,
            budget_won, expected_total_won, created_by_user_id, reason,
            created_at
        ) VALUES (?, ?, ?, 1, '{}', ?, 20000, 10000, ?, 'legacy', ?)
        """,
        (
            approval_id,
            fixture.school_id,
            fixture.workspace_id,
            "a" * 64,
            fixture.operator_id,
            NOW,
        ),
    )
    fixture.connection.execute(
        """
        INSERT INTO approval_rows (
            id, approval_revision_id, school_id, workspace_id, candidate_id,
            recommendation_id, isbn13, title, author, quantity, unit_price,
            line_total_won, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, '9788937464010', '보존 책', '저자', 1, 10000, 10000, ?)
        """,
        (
            approval_row_id,
            approval_id,
            fixture.school_id,
            fixture.workspace_id,
            candidate_id,
            recommendation_id,
            NOW,
        ),
    )
    fixture.connection.execute(
        "UPDATE approval_revisions SET sealed_at = ? WHERE id = ?",
        (NOW, approval_id),
    )
    fixture.connection.execute(
        """
        INSERT INTO approval_decisions (
            id, approval_revision_id, school_id, workspace_id, actor_id,
            decision, reason, created_at
        ) VALUES (?, ?, ?, ?, ?, 'APPROVED', 'legacy', ?)
        """,
        (
            str(uuid.uuid4()),
            approval_id,
            fixture.school_id,
            fixture.workspace_id,
            fixture.reviewer_id,
            NOW,
        ),
    )
    fixture.connection.execute(
        """
        INSERT INTO vendor_quotes (
            id, school_id, workspace_id, approval_revision_id, vendor_name,
            total_won, list_total_won, discount_won, budget_overrun_won,
            out_of_stock_count, missing_price_count, list_mismatch_count,
            needs_review_count, unmatched_count, requires_reapproval, reason,
            created_by_user_id, created_at
        ) VALUES (?, ?, ?, ?, 'legacy vendor', 9000, 10000, 1000, 0,
                  0, 0, 0, 0, 0, 0, 'legacy', ?, ?)
        """,
        (
            quote_id,
            fixture.school_id,
            fixture.workspace_id,
            approval_id,
            fixture.operator_id,
            NOW,
        ),
    )
    fixture.connection.execute(
        """
        INSERT INTO vendor_quote_rows (
            id, quote_id, approval_row_id, match_status, isbn13, title, author,
            quantity, unit_price, line_total_won, created_at
        ) VALUES (?, ?, ?, 'MATCHED_ISBN', '9788937464010', '보존 책', '저자', 1, 9000, 9000, ?)
        """,
        (quote_row_id, quote_id, approval_row_id, NOW),
    )
    fixture.connection.execute(
        """
        INSERT INTO vendor_quote_rows (
            id, quote_id, approval_row_id, match_status, isbn13, title, author,
            quantity, unit_price, line_total_won, created_at
        ) VALUES (?, ?, ?, 'MATCHED_ISBN', '9788937464010', '구버전 0수량',
                  '저자', 0, 9000, 0, ?)
        """,
        (zero_quote_row_id, quote_id, approval_row_id, NOW),
    )
    fixture.connection.execute(
        "UPDATE vendor_quotes SET sealed_at = ? WHERE id = ?",
        (NOW, quote_id),
    )
    fixture.connection.execute(
        """
        INSERT INTO generated_artifacts (
            id, school_id, workspace_id, artifact_type, storage_path, sha256,
            size_bytes, content_bytes, created_by_user_id, created_at
        ) VALUES (?, ?, ?, 'ORDER_XLSX', 'legacy.xlsx', ?, 1, X'00', ?, ?)
        """,
        (
            artifact_id,
            fixture.school_id,
            fixture.workspace_id,
            "b" * 64,
            fixture.operator_id,
            NOW,
        ),
    )
    fixture.connection.execute(
        """
        INSERT INTO order_revisions (
            id, school_id, workspace_id, approval_revision_id, quote_id,
            artifact_id, revision_number, reason, created_by_user_id,
            created_at
        ) VALUES (?, ?, ?, ?, ?, ?, 1, 'legacy', ?, ?)
        """,
        (
            order_id,
            fixture.school_id,
            fixture.workspace_id,
            approval_id,
            quote_id,
            artifact_id,
            fixture.operator_id,
            NOW,
        ),
    )
    fixture.connection.execute(
        """
        INSERT INTO order_rows (
            id, order_revision_id, approval_row_id, isbn13, title, author,
            quantity, unit_price, line_total_won, created_at
        ) VALUES (?, ?, ?, '9788937464010', '보존 책', '저자', 1, 9000, 9000, ?)
        """,
        (order_row_id, order_id, approval_row_id, NOW),
    )
    fixture.connection.execute(
        "UPDATE order_revisions SET sealed_at = ? WHERE id = ?",
        (NOW, order_id),
    )
    fixture.connection.execute(
        """
        INSERT INTO receiving_differences (
            id, school_id, workspace_id, order_revision_id, order_row_id,
            kind, reference_key, details_json, disposition, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, 'MISSING', 'legacy', '{}',
                  'ADDITIONAL_DELIVERY_PLANNED', ?, ?)
        """,
        (
            difference_id,
            fixture.school_id,
            fixture.workspace_id,
            order_id,
            order_row_id,
            NOW,
            NOW,
        ),
    )
    fixture.connection.commit()

    apply_migrations(fixture.connection, current_dir)

    migrated_states = {
        row["id"]: row["status"]
        for row in fixture.connection.execute(
            "SELECT id, status FROM acquisition_workspaces WHERE id IN ({})".format(
                ",".join("?" for _ in workspace_ids)
            ),
            tuple(workspace_ids.values()),
        ).fetchall()
    }
    assert {
        old_state: migrated_states[workspace_ids[old_state]] for old_state in aliases
    } == aliases
    assert (
        fixture.connection.execute(
            "SELECT disposition FROM receiving_differences WHERE id = ?",
            (difference_id,),
        ).fetchone()["disposition"]
        == "ADDITIONAL_DELIVERY"
    )
    assert fixture.connection.execute("PRAGMA foreign_key_check").fetchall() == []
    preserved_quote_rows = [
        dict(row)
        for row in fixture.connection.execute(
            "SELECT id, quantity FROM vendor_quote_rows WHERE quote_id = ? ORDER BY quantity",
            (quote_id,),
        ).fetchall()
    ]
    assert preserved_quote_rows == [
        {"id": zero_quote_row_id, "quantity": 0},
        {"id": quote_row_id, "quantity": 1},
    ]
    assert (
        fixture.connection.execute(
            "SELECT migration_id FROM schema_migrations ORDER BY migration_id DESC LIMIT 1"
        ).fetchone()[0]
        == "0012_task8_round4_integrity"
    )


def test_forward_constraints_reject_invalid_legacy_data_without_data_loss(
    data_dir: Path, tmp_path: Path
) -> None:
    """Invalid historical values must block the validation migration, not be discarded."""
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    source_migrations = (
        Path(__file__).parents[1] / "src" / "suseoro" / "db" / "migrations"
    )
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

        assert (
            connection.execute("SELECT name FROM schools").fetchone()[0]
            == "Legacy school"
        )
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
