from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException
from test_api_contract import WORKSPACE_ID, _client, _headers
from test_task8_round4 import (
    _seed_source_row,
    _seed_terminal_file_result,
    _source_id,
)
from test_task8_round5 import _0010_directory, _file_result
from workflow_fixtures import make_workflow_fixture

from suseoro.db import migrations as migration_module
from suseoro.db.connection import connect
from suseoro.db.migrations import apply_migrations
from suseoro.workflow.approvals import ApprovalService


def _migration_directory() -> Path:
    return Path(migration_module.__file__).with_name("migrations")


def _candidate_collection_revision(fixture) -> int:
    return int(
        fixture.connection.execute(
            """
            SELECT candidate_collection_revision
            FROM acquisition_workspaces WHERE id = ?
            """,
            (fixture.workspace_id,),
        ).fetchone()["candidate_collection_revision"]
    )


def test_mixed_legacy_partial_is_exact_only_with_complete_row_evidence(
    tmp_path: Path,
) -> None:
    """A missing/partial evidence set must never certify legacy total=processed."""
    exact = make_workflow_fixture(
        tmp_path / "exact",
        migrations_dir=_0010_directory(tmp_path / "exact-migrations"),
    )
    exact_source = _source_id(exact)
    exact_job, _ = _seed_terminal_file_result(
        exact,
        source_id=exact_source,
        total_rows=100,
        processed_rows=100,
        error={"code": "ROW_ERRORS"},
    )
    for source_row in range(1, 91):
        _seed_source_row(
            exact, source_id=exact_source, source_row=source_row, status="SUCCESS"
        )
    for source_row in range(91, 101):
        _seed_source_row(
            exact,
            source_id=exact_source,
            source_row=source_row,
            status="ROW_ERROR",
        )
    exact.connection.commit()

    incomplete = make_workflow_fixture(
        tmp_path / "incomplete",
        migrations_dir=_0010_directory(tmp_path / "incomplete-migrations"),
    )
    incomplete_source = _source_id(incomplete)
    incomplete_job, _ = _seed_terminal_file_result(
        incomplete,
        source_id=incomplete_source,
        total_rows=100,
        processed_rows=100,
        error={"code": "ROW_ERRORS"},
    )
    _seed_source_row(
        incomplete,
        source_id=incomplete_source,
        source_row=100,
        status="ROW_ERROR",
    )
    incomplete.connection.commit()

    legacy_mixed = make_workflow_fixture(
        tmp_path / "legacy-mixed",
        migrations_dir=_0010_directory(tmp_path / "legacy-mixed-migrations"),
    )
    legacy_source = _source_id(legacy_mixed)
    legacy_job, _ = _seed_terminal_file_result(
        legacy_mixed,
        source_id=legacy_source,
        total_rows=100,
        processed_rows=90,
        error={"code": "ROW_ERRORS"},
    )
    legacy_mixed.connection.commit()

    apply_migrations(exact.connection, _migration_directory())
    apply_migrations(incomplete.connection, _migration_directory())
    apply_migrations(legacy_mixed.connection, _migration_directory())

    exact_result = _file_result(exact, exact_job)
    assert (
        exact_result["processed_rows"],
        exact_result["row_error_count"],
        exact_result["count_confidence"],
    ) == (90, 10, "EXACT")
    assert _file_result(incomplete, incomplete_job)["count_confidence"] == (
        "UNVERIFIED"
    )
    legacy_result = _file_result(legacy_mixed, legacy_job)
    assert (
        legacy_result["processed_rows"],
        legacy_result["row_error_count"],
        legacy_result["count_confidence"],
    ) == (90, 10, "UNVERIFIED")


def test_approval_rejects_unverified_source_totals(tmp_path: Path) -> None:
    """Approval cannot turn an unverified legacy file total into authority."""
    fixture = make_workflow_fixture(
        tmp_path / "unverified-approval",
        migrations_dir=_0010_directory(tmp_path / "unverified-migrations"),
    )
    source_id = _source_id(fixture)
    job_id, _ = _seed_terminal_file_result(
        fixture,
        source_id=source_id,
        total_rows=20,
        processed_rows=20,
        error={"code": "ROW_ERRORS"},
    )
    _seed_source_row(fixture, source_id=source_id, source_row=20, status="ROW_ERROR")
    fixture.connection.execute(
        """
        INSERT INTO workspace_sources (
            workspace_id, source_document_id, school_id, created_at
        ) VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        """,
        (fixture.workspace_id, source_id, fixture.school_id),
    )
    fixture.connection.commit()
    apply_migrations(fixture.connection, _migration_directory())
    assert _file_result(fixture, job_id)["count_confidence"] == "UNVERIFIED"
    fixture.add_candidate(
        title="검증되지 않은 목록의 책",
        author="저자",
        isbn="9788937464010",
        unit_price=12_000,
    )

    with pytest.raises(HTTPException) as blocked:
        ApprovalService(fixture.connection).request_approval(
            school_id=fixture.school_id,
            workspace_id=fixture.workspace_id,
            actor_id=fixture.operator_id,
            actor_roles=("OPERATOR",),
            workspace_version=fixture.workspace_version(),
            candidate_collection_revision=_candidate_collection_revision(fixture),
            budget_won=20_000,
            reason="승인 요청",
            idempotency_key="unverified-count-approval",
            request_id=str(uuid.uuid4()),
        )

    assert blocked.value.status_code == 409
    assert blocked.value.detail["code"] == "SOURCE_COUNTS_UNVERIFIED"
    assert (
        fixture.connection.execute(
            "SELECT COUNT(*) FROM approval_revisions WHERE workspace_id = ?",
            (fixture.workspace_id,),
        ).fetchone()[0]
        == 0
    )


def test_approval_seal_fences_candidate_collection_revision_and_audits_it(
    tmp_path: Path,
) -> None:
    """A candidate move after review must conflict before an immutable seal exists."""
    fixture = make_workflow_fixture(tmp_path / "candidate-fence")
    fixture.add_candidate(
        title="검토자가 본 책",
        author="저자",
        isbn="9788937464010",
        unit_price=10_000,
    )
    accepted_revision = _candidate_collection_revision(fixture)
    fixture.add_candidate(
        title="동시에 추가된 책",
        author="다른 저자",
        isbn="9788937464027",
        unit_price=9_000,
    )
    current_revision = _candidate_collection_revision(fixture)

    with pytest.raises(HTTPException) as stale:
        ApprovalService(fixture.connection).request_approval(
            school_id=fixture.school_id,
            workspace_id=fixture.workspace_id,
            actor_id=fixture.operator_id,
            actor_roles=("OPERATOR",),
            workspace_version=fixture.workspace_version(),
            candidate_collection_revision=accepted_revision,
            budget_won=30_000,
            reason="검토한 목록 승인 요청",
            idempotency_key="candidate-fence-stale",
            request_id=str(uuid.uuid4()),
        )

    assert stale.value.status_code == 409
    assert stale.value.detail == {
        "code": "CANDIDATE_COLLECTION_CHANGED",
        "accepted_candidate_collection_revision": accepted_revision,
        "current_candidate_collection_revision": current_revision,
    }
    assert (
        fixture.connection.execute(
            "SELECT COUNT(*) FROM approval_revisions WHERE workspace_id = ?",
            (fixture.workspace_id,),
        ).fetchone()[0]
        == 0
    )

    request_id = str(uuid.uuid4())
    sealed = ApprovalService(fixture.connection).request_approval(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        workspace_version=fixture.workspace_version(),
        candidate_collection_revision=current_revision,
        budget_won=30_000,
        reason="최신 목록 승인 요청",
        idempotency_key="candidate-fence-current",
        request_id=request_id,
    )
    assert sealed["candidate_collection_revision"] == current_revision
    revision = fixture.connection.execute(
        """
        SELECT candidate_collection_revision FROM approval_revisions
        WHERE id = ?
        """,
        (sealed["revision_id"],),
    ).fetchone()
    assert revision["candidate_collection_revision"] == current_revision
    audit = fixture.connection.execute(
        "SELECT after_json FROM audit_events WHERE request_id = ?",
        (request_id,),
    ).fetchone()
    assert json.loads(audit["after_json"])["candidate_collection_revision"] == (
        current_revision
    )


def test_normal_idempotency_replay_crosses_current_typed_public_boundary(
    tmp_path: Path,
) -> None:
    """Non-upload workflow replays must strip persisted private nested fields."""
    fixture = make_workflow_fixture(tmp_path / "normal-replay-boundary")
    fixture.add_candidate(
        title="재생 경계를 확인할 책",
        author="김사서",
        isbn="9788937464010",
        unit_price=12_000,
    )
    service = ApprovalService(fixture.connection)
    workspace_version = fixture.workspace_version()
    collection_revision = _candidate_collection_revision(fixture)
    request_id = str(uuid.uuid4())
    first = service.request_approval(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        workspace_version=workspace_version,
        candidate_collection_revision=collection_revision,
        budget_won=20_000,
        reason="정상 재생 경계 검증",
        idempotency_key="normal-private-replay",
        request_id=request_id,
    )
    persisted = {
        **first,
        "private": {
            "path": "C:/secret/library.sqlite3",
            "sql": "SELECT * FROM private_borrowers",
            "exception": {"message": "OperationalError: no such table"},
        },
    }
    fixture.connection.execute(
        """
        UPDATE idempotency_keys SET response_body = ?
        WHERE school_id = ? AND actor_id = ?
          AND route = 'approvals.request' AND key = ?
        """,
        (
            json.dumps(persisted, ensure_ascii=False),
            fixture.school_id,
            fixture.operator_id,
            "normal-private-replay",
        ),
    )
    fixture.connection.commit()

    replay = service.request_approval(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        workspace_version=workspace_version,
        candidate_collection_revision=collection_revision,
        budget_won=20_000,
        reason="정상 재생 경계 검증",
        idempotency_key="normal-private-replay",
        request_id=request_id,
    )

    assert replay == first
    encoded = json.dumps(replay, ensure_ascii=False)
    for private in ("library.sqlite3", "private_borrowers", "OperationalError"):
        assert private not in encoded

    fixture.connection.execute(
        """
        UPDATE idempotency_keys SET response_body = ?
        WHERE school_id = ? AND actor_id = ?
          AND route = 'approvals.request' AND key = ?
        """,
        (
            json.dumps(
                {
                    "state": "APPROVAL_PENDING",
                    "row_version": 2,
                    "private": {"path": "C:/secret/invalid.sqlite3"},
                }
            ),
            fixture.school_id,
            fixture.operator_id,
            "normal-private-replay",
        ),
    )
    fixture.connection.commit()

    with pytest.raises(HTTPException) as invalid:
        service.request_approval(
            school_id=fixture.school_id,
            workspace_id=fixture.workspace_id,
            actor_id=fixture.operator_id,
            actor_roles=("OPERATOR",),
            workspace_version=workspace_version,
            candidate_collection_revision=collection_revision,
            budget_won=20_000,
            reason="정상 재생 경계 검증",
            idempotency_key="normal-private-replay",
            request_id=request_id,
        )
    assert invalid.value.status_code == 409
    assert invalid.value.detail == {"code": "HISTORICAL_REPLAY_INVALID"}
    assert "invalid.sqlite3" not in str(invalid.value.detail)


def test_historical_upload_replay_sanitizes_nested_error_and_mapping_payload(
    tmp_path: Path,
) -> None:
    """A valid-shaped replay must not echo persisted SQL/path/exception details."""
    client, settings, csrf = _client(tmp_path / "upload-replay")
    headers = _headers(csrf, "task9-private-replay")
    first = client.post(
        f"/api/v2/workspaces/{WORKSPACE_ID}/sources",
        headers=headers,
        data={"role": "PURCHASE_REQUEST"},
        files=[("files", ("private.csv", "제목,저자\n책,저자\n", "text/csv"))],
    )
    assert first.status_code == 202
    first_body = first.json()
    malicious = {
        "job_id": first_body["job_id"],
        "items": [
            {
                "filename": "private.csv",
                "status": "FAILED",
                "source_id": None,
                "error": {
                    "code": "UNSUPPORTED_FILE_TYPE",
                    "message": (
                        "OperationalError: no such table private_borrowers at "
                        "C:/secret/library.sqlite3"
                    ),
                    "mapping_required": {
                        "headers": ["SELECT * FROM private_borrowers"],
                        "preview_rows": [["C:/secret/library.sqlite3"]],
                    },
                },
                "repair_obligation_id": None,
                "repair_generation": None,
            }
        ],
        "mapping_required": {"path": "C:/secret/library.sqlite3"},
    }
    serialized = json.dumps(malicious, ensure_ascii=False)
    with connect(settings.database_path) as connection:
        connection.execute(
            """
            UPDATE upload_idempotency_claims SET response_body = ?
            WHERE key = ?
            """,
            (serialized, "task9-private-replay"),
        )
        connection.execute(
            "UPDATE idempotency_keys SET response_body = ? WHERE key = ?",
            (serialized, "task9-private-replay"),
        )
        connection.commit()

    replay = client.post(
        f"/api/v2/workspaces/{WORKSPACE_ID}/sources",
        headers=headers,
        data={"role": "PURCHASE_REQUEST"},
        files=[("files", ("private.csv", "제목,저자\n책,저자\n", "text/csv"))],
    )

    assert replay.status_code == 202
    assert replay.json()["items"][0]["error"] == {
        "code": "UNSUPPORTED_FILE_TYPE",
        "message": "지원하지 않는 파일 형식입니다.",
    }
    replay_text = replay.text
    for private in (
        "OperationalError",
        "private_borrowers",
        "library.sqlite3",
        "SELECT",
        "mapping_required",
    ):
        assert private not in replay_text


def test_legacy_upload_replay_is_sanitized_before_new_claim_is_persisted(
    tmp_path: Path,
) -> None:
    """Pre-claim ledger bodies cross the current upload boundary before copying."""
    client, settings, csrf = _client(tmp_path / "legacy-upload-replay")
    original_key = "task9-legacy-origin"
    first = client.post(
        f"/api/v2/workspaces/{WORKSPACE_ID}/sources",
        headers=_headers(csrf, original_key),
        data={"role": "PURCHASE_REQUEST"},
        files=[("files", ("legacy.csv", "제목\n책\n", "text/csv"))],
    )
    assert first.status_code == 202
    malicious = {
        "job_id": first.json()["job_id"],
        "items": [
            {
                "filename": "legacy.csv",
                "status": "FAILED",
                "source_id": None,
                "error": {
                    "code": "sqlite3.OperationalError: private_table",
                    "message": "C:/secret/private.sqlite3",
                    "mapping_required": {"sql": "SELECT * FROM private_table"},
                },
                "private_path": "C:/secret/private.sqlite3",
            }
        ],
        "exception": "OperationalError",
    }
    legacy_key = "task9-legacy-only"
    with connect(settings.database_path) as connection:
        connection.execute(
            """
            INSERT INTO idempotency_keys (
                id, school_id, actor_id, route, key, request_hash,
                response_status, response_body, created_at
            )
            SELECT ?, school_id, actor_id, route, ?, request_hash,
                   response_status, ?, created_at
            FROM idempotency_keys WHERE key = ?
            """,
            (
                str(uuid.uuid4()),
                legacy_key,
                json.dumps(malicious, ensure_ascii=False),
                original_key,
            ),
        )
        connection.commit()

    replay = client.post(
        f"/api/v2/workspaces/{WORKSPACE_ID}/sources",
        headers=_headers(csrf, legacy_key),
        data={"role": "PURCHASE_REQUEST"},
        files=[("files", ("legacy.csv", "제목\n책\n", "text/csv"))],
    )
    assert replay.status_code == 202
    assert replay.json()["items"][0] == {
        "filename": "legacy.csv",
        "status": "FAILED",
        "source_id": None,
        "error": {
            "code": "UPLOAD_ITEM_FAILED",
            "message": "파일을 안전하게 확인할 수 없습니다. 다시 올려 주세요.",
        },
        "repair_obligation_id": None,
        "repair_generation": None,
        "procurement_import_id": None,
    }
    with connect(settings.database_path) as connection:
        copied = connection.execute(
            "SELECT response_body FROM upload_idempotency_claims WHERE key = ?",
            (legacy_key,),
        ).fetchone()["response_body"]
    for private in ("OperationalError", "private_table", "private.sqlite3", "SELECT"):
        assert private not in copied


def test_malformed_historical_upload_replay_fails_closed(tmp_path: Path) -> None:
    client, settings, csrf = _client(tmp_path / "malformed-upload-replay")
    original_key = "task9-malformed-origin"
    first = client.post(
        f"/api/v2/workspaces/{WORKSPACE_ID}/sources",
        headers=_headers(csrf, original_key),
        data={"role": "PURCHASE_REQUEST"},
        files=[("files", ("malformed.csv", "제목\n책\n", "text/csv"))],
    )
    assert first.status_code == 202
    malformed_key = "task9-malformed-only"
    with connect(settings.database_path) as connection:
        connection.execute(
            """
            INSERT INTO idempotency_keys (
                id, school_id, actor_id, route, key, request_hash,
                response_status, response_body, created_at
            )
            SELECT ?, school_id, actor_id, route, ?, request_hash,
                   response_status, '{not-json', created_at
            FROM idempotency_keys WHERE key = ?
            """,
            (str(uuid.uuid4()), malformed_key, original_key),
        )
        connection.commit()

    replay = client.post(
        f"/api/v2/workspaces/{WORKSPACE_ID}/sources",
        headers=_headers(csrf, malformed_key),
        data={"role": "PURCHASE_REQUEST"},
        files=[("files", ("malformed.csv", "제목\n책\n", "text/csv"))],
    )
    assert replay.status_code == 409
    assert replay.json()["detail"]["code"] == "HISTORICAL_REPLAY_INVALID"
    assert "not-json" not in replay.text
