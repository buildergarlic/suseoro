from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from pathlib import Path

import pytest
from test_api_contract import WORKSPACE_ID, _client, _headers
from test_task8_round4 import (
    INTEGRITY_ID,
    PRELUDE_ID,
    _copy_migrations,
    _result_counts,
    _seed_terminal_file_result,
    _source_id,
)
from workflow_fixtures import make_workflow_fixture

from suseoro.api.dependencies import AuthenticatedUser
from suseoro.api.routes.candidates import list_candidates
from suseoro.backup.service import BackupService, RestoreVerificationError
from suseoro.db import migrations as migration_module
from suseoro.db.connection import connect
from suseoro.db.migrations import apply_migrations
from suseoro.jobs.repository import JobRepository

CAPTURE_ID = "0010b_task8_round5_count_capture"
RESTORATION_ID = "0013_task8_round5_count_restoration"
CANDIDATE_REVISION_ID = "0014_task8_round5_candidate_revision"
MAPPING_PROVENANCE_ID = "0015_task8_round5_mapping_provenance"
COMMITTED_0011_SHA256 = (
    "eb22db853d9488c3fdc0a95ad0eafd2e4c79884d41d6d313b272cb49822242b0"
)
COMMITTED_0012_SHA256 = (
    "d236b01e9fc31013c5c183ff1536c5815badd47acce6ce5c01dc700b77dcb462"
)


def _migration_directory() -> Path:
    return Path(migration_module.__file__).with_name("migrations")


def _round4_directory(tmp_path: Path) -> Path:
    return _copy_migrations(
        tmp_path,
        exclude={
            CAPTURE_ID,
            RESTORATION_ID,
            CANDIDATE_REVISION_ID,
            MAPPING_PROVENANCE_ID,
        },
    )


def _0010_directory(tmp_path: Path) -> Path:
    return _copy_migrations(
        tmp_path,
        exclude={
            PRELUDE_ID,
            "0011_task8_round3_integrity",
            INTEGRITY_ID,
            CAPTURE_ID,
            RESTORATION_ID,
            CANDIDATE_REVISION_ID,
            MAPPING_PROVENANCE_ID,
        },
    )


def _0011_directory(tmp_path: Path) -> Path:
    return _copy_migrations(
        tmp_path,
        exclude={
            PRELUDE_ID,
            CAPTURE_ID,
            INTEGRITY_ID,
            RESTORATION_ID,
            CANDIDATE_REVISION_ID,
            MAPPING_PROVENANCE_ID,
        },
    )


def _file_result(fixture, job_id: str) -> dict[str, object]:
    return next(
        item
        for item in JobRepository(fixture.connection).file_results(job_id)
        if item["source_document_id"] == _source_id(fixture)
    )


def test_populated_0010_upgrade_preserves_exact_partial_counts_per_job(
    tmp_path: Path,
) -> None:
    """Replacing immutable per-job counts with current source rows is data corruption."""
    fixture = make_workflow_fixture(
        tmp_path / "populated-0010",
        migrations_dir=_0010_directory(tmp_path / "0010-migrations"),
    )
    source_id = _source_id(fixture)
    job_id, result_id = _seed_terminal_file_result(
        fixture,
        source_id=source_id,
        total_rows=100,
        processed_rows=90,
        error={"code": "ROW_ERRORS"},
    )

    apply_migrations(fixture.connection, _migration_directory())

    assert _result_counts(fixture, (result_id,))[result_id] == (
        100,
        90,
        10,
        "PARTIAL",
    )
    assert _file_result(fixture, job_id)["count_confidence"] == "EXACT"


def test_populated_0010_failed_result_preserves_nonzero_processed_count(
    tmp_path: Path,
) -> None:
    """A failed historical item may still have processed rows worth preserving."""
    fixture = make_workflow_fixture(
        tmp_path / "populated-0010-failed",
        migrations_dir=_0010_directory(tmp_path / "0010-failed-migrations"),
    )
    source_id = _source_id(fixture)
    job_id, result_id = _seed_terminal_file_result(
        fixture,
        source_id=source_id,
        total_rows=100,
        processed_rows=40,
        status="FAILED",
        error={"code": "PARSER_FAILURE"},
    )

    apply_migrations(fixture.connection, _migration_directory())

    assert _result_counts(fixture, (result_id,))[result_id] == (
        100,
        40,
        60,
        "FAILED",
    )
    assert _file_result(fixture, job_id)["count_confidence"] == "EXACT"


def test_already_0012_without_capture_is_explicitly_unverified_and_not_rewritten(
    tmp_path: Path,
) -> None:
    """An already-reconstructed row has no evidence that permits claiming exact counts."""
    fixture = make_workflow_fixture(
        tmp_path / "already-0012",
        migrations_dir=_0010_directory(tmp_path / "already-0012-0010"),
    )
    source_id = _source_id(fixture)
    job_id, result_id = _seed_terminal_file_result(
        fixture,
        source_id=source_id,
        total_rows=100,
        processed_rows=90,
        error={"code": "ROW_ERRORS"},
    )
    apply_migrations(
        fixture.connection,
        _round4_directory(tmp_path / "already-0012-round4"),
    )
    reconstructed = _result_counts(fixture, (result_id,))[result_id]

    apply_migrations(fixture.connection, _migration_directory())

    assert _result_counts(fixture, (result_id,))[result_id] == reconstructed
    assert _file_result(fixture, job_id)["count_confidence"] == "UNVERIFIED"


def test_already_0011_is_captured_before_0012_without_claiming_exact_confidence(
    tmp_path: Path,
) -> None:
    """An 0011-only database keeps its pre-0012 values but is conservatively marked."""
    fixture = make_workflow_fixture(
        tmp_path / "already-0011",
        migrations_dir=_0011_directory(tmp_path / "already-0011-migrations"),
    )
    source_id = _source_id(fixture)
    job_id, result_id = _seed_terminal_file_result(
        fixture,
        source_id=source_id,
        total_rows=100,
        processed_rows=90,
        row_error_count=10,
        error={"code": "ROW_ERRORS"},
    )
    before_0012 = _result_counts(fixture, (result_id,))[result_id]

    apply_migrations(fixture.connection, _migration_directory())

    assert _result_counts(fixture, (result_id,))[result_id] == before_0012
    assert _file_result(fixture, job_id)["count_confidence"] == "UNVERIFIED"


def test_already_0012_with_pre_rewrite_capture_restores_exact_counts(
    tmp_path: Path,
) -> None:
    """A captured row survives a partial rollout that applied 0012 before restoration."""
    fixture = make_workflow_fixture(
        tmp_path / "captured-before-0012",
        migrations_dir=_0010_directory(tmp_path / "captured-0010"),
    )
    source_id = _source_id(fixture)
    job_id, result_id = _seed_terminal_file_result(
        fixture,
        source_id=source_id,
        total_rows=100,
        processed_rows=90,
        error={"code": "ROW_ERRORS"},
    )
    capture_path = _migration_directory() / f"{CAPTURE_ID}.sql"
    assert capture_path.is_file()
    capture_sql = capture_path.read_text(encoding="utf-8")
    capture_checksum = hashlib.sha256(capture_sql.encode("utf-8")).hexdigest()
    fixture.connection.executescript("BEGIN IMMEDIATE;\n" + capture_sql)
    fixture.connection.execute(
        """
        INSERT INTO schema_migrations (migration_id, checksum, applied_at)
        VALUES (?, ?, '2026-08-29T00:00:00.000000Z')
        """,
        (CAPTURE_ID, capture_checksum),
    )
    fixture.connection.commit()
    apply_migrations(
        fixture.connection,
        _round4_directory(tmp_path / "captured-round4"),
    )
    assert _result_counts(fixture, (result_id,))[result_id] != (
        100,
        90,
        10,
        "PARTIAL",
    )

    apply_migrations(fixture.connection, _migration_directory())

    assert _result_counts(fixture, (result_id,))[result_id] == (
        100,
        90,
        10,
        "PARTIAL",
    )
    assert _file_result(fixture, job_id)["count_confidence"] == "EXACT"


def test_missing_prelude_repair_does_not_rerun_0012_count_reconstruction(
    tmp_path: Path,
) -> None:
    """Repairing a missing lower-sorted ledger member must not touch history again."""
    fixture = make_workflow_fixture(tmp_path / "postlude-rerun")
    source_id = _source_id(fixture)
    _, result_id = _seed_terminal_file_result(
        fixture,
        source_id=source_id,
        total_rows=2,
        processed_rows=1,
        row_error_count=1,
        error={"code": "ROW_ERRORS"},
    )
    fixture.connection.executescript(
        """
        CREATE TABLE round5_count_update_spy (result_id TEXT NOT NULL);
        CREATE TRIGGER round5_spy_historical_count_update
        AFTER UPDATE OF total_rows, processed_rows, row_error_count
        ON job_file_results
        BEGIN
            INSERT INTO round5_count_update_spy(result_id) VALUES (NEW.id);
        END;
        """
    )
    fixture.connection.execute(
        "DELETE FROM schema_migrations WHERE migration_id = ?", (PRELUDE_ID,)
    )
    fixture.connection.commit()

    apply_migrations(fixture.connection, _migration_directory())

    assert (
        fixture.connection.execute(
            "SELECT COUNT(*) FROM round5_count_update_spy WHERE result_id = ?",
            (result_id,),
        ).fetchone()[0]
        == 0
    )


def test_round5_preserves_committed_0011_and_0012_checksums(tmp_path: Path) -> None:
    connection = connect(tmp_path / "fresh-round5.sqlite3")
    apply_migrations(connection, _migration_directory())
    applied = {
        str(row["migration_id"]): str(row["checksum"])
        for row in connection.execute(
            "SELECT migration_id, checksum FROM schema_migrations"
        ).fetchall()
    }

    assert applied["0011_task8_round3_integrity"] == COMMITTED_0011_SHA256
    assert applied[INTEGRITY_ID] == COMMITTED_0012_SHA256
    assert {
        CAPTURE_ID,
        RESTORATION_ID,
        CANDIDATE_REVISION_ID,
        MAPPING_PROVENANCE_ID,
    } <= set(applied)


def test_backup_validator_accepts_exact_0011_and_0012_release_histories() -> None:
    bundled = BackupService._bundled_schema()
    historical_0011 = tuple(
        item
        for item in bundled
        if item["version"] <= "0011_task8_round3_integrity"
        and item["version"] not in {PRELUDE_ID, CAPTURE_ID}
    )
    historical_0012 = tuple(
        item
        for item in bundled
        if item["version"] <= INTEGRITY_ID and item["version"] != CAPTURE_ID
    )

    BackupService._validate_migration_history(historical_0011)
    BackupService._validate_migration_history(historical_0012)

    missing_committed_member = tuple(
        item
        for item in historical_0012
        if item["version"] != "0011_task8_round3_integrity"
    )
    with pytest.raises(RestoreVerificationError, match="migration history"):
        BackupService._validate_migration_history(missing_committed_member)


def _seed_private_historical_job(
    connection: sqlite3.Connection,
    *,
    job_type: str,
    workspace_id: str,
    school_id: str,
    source_id: str,
) -> str:
    repository = JobRepository(connection)
    queued = repository.create(
        school_id=school_id,
        workspace_id=workspace_id,
        job_type=job_type,
        payload={"source_document_ids": [source_id]},
        progress_total=1,
    )
    connection.commit()
    claimed = repository.claim_next()
    assert claimed is not None and claimed.id == queued.id
    private = {
        "type": "OperationalError",
        "code": "RAW_SQL_FAILURE",
        "message": (
            "no such table private_patron_export at "
            "C:/secret/library.sqlite3; SELECT * FROM borrowers"
        ),
    }
    repository.record_file_result(
        job_id=claimed.id,
        claim_token=str(claimed.claim_token),
        claim_generation=claimed.claim_generation,
        source_document_id=source_id,
        status="FAILED",
        total_rows=1,
        processed_rows=0,
        row_error_count=1,
        error=private,
    )
    repository.mark_failed(
        claimed.id,
        claim_token=str(claimed.claim_token),
        error=private,
    )
    connection.commit()
    return claimed.id


def test_historical_ingest_and_parse_errors_are_allowlisted_at_every_boundary(
    data_dir: Path,
) -> None:
    """Pre-sanitizer SQL, table, path, type, code, and message strings stay private."""
    client, settings, csrf = _client(data_dir, raise_server_exceptions=False)
    uploaded = client.post(
        f"/api/v2/workspaces/{WORKSPACE_ID}/sources",
        headers=_headers(csrf, "round5-private-history"),
        data={"role": "PURCHASE_REQUEST"},
        files=[("files", ("history.csv", "제목,저자\nA,B\n", "text/csv"))],
    )
    assert uploaded.status_code == 202
    source_id = uploaded.json()["items"][0]["source_id"]
    with connect(settings.database_path) as connection:
        connection.execute(
            "DELETE FROM durable_jobs WHERE id = ?", (uploaded.json()["job_id"],)
        )
        connection.commit()
        ingest_id = _seed_private_historical_job(
            connection,
            job_type="INGEST",
            workspace_id=WORKSPACE_ID,
            school_id="550e8400-e29b-41d4-a716-446655440100",
            source_id=source_id,
        )
        parse_id = _seed_private_historical_job(
            connection,
            job_type="PARSE",
            workspace_id=WORKSPACE_ID,
            school_id="550e8400-e29b-41d4-a716-446655440100",
            source_id=source_id,
        )
        mapping_id = _seed_private_historical_job(
            connection,
            job_type="INGEST",
            workspace_id=WORKSPACE_ID,
            school_id="550e8400-e29b-41d4-a716-446655440100",
            source_id=source_id,
        )
        private_mapping = {
            "type": "OperationalError",
            "code": "MAPPING_REQUIRED",
            "message": "private_index exception",
            "public_mapping_payload_version": 1,
            "mapping_required": {
                "headers": ["C:/secret/private.sqlite"],
                "preview_rows": [["SELECT borrowers FROM private_table"]],
                "suggested_mapping": {},
                "required_fields": [],
                "confidence": 0,
                "questions": [],
            },
        }
        connection.execute(
            """
            UPDATE durable_jobs SET status = 'RUNNING', stage = 'PARSING'
            WHERE id = ?
            """,
            (mapping_id,),
        )
        connection.execute(
            "UPDATE job_file_results SET error_json = ? WHERE job_id = ?",
            (json.dumps(private_mapping), mapping_id),
        )
        connection.execute(
            """
            UPDATE durable_jobs SET status = 'FAILED', stage = 'FAILED'
            WHERE id = ?
            """,
            (mapping_id,),
        )
        malformed = "{OperationalError: SELECT private_index FROM C:/secret}"
        connection.execute(
            """
            UPDATE durable_jobs
            SET status = 'RUNNING', stage = 'PARSING', error_json = ?
            WHERE id = ?
            """,
            (malformed, parse_id),
        )
        connection.execute(
            "UPDATE job_file_results SET error_json = ? WHERE job_id = ?",
            (malformed, parse_id),
        )
        connection.execute(
            """
            UPDATE durable_jobs
            SET status = 'FAILED', stage = 'FAILED', error_json = ?
            WHERE id = ?
            """,
            (malformed, parse_id),
        )
        connection.commit()

    responses = [
        client.get(f"/api/v2/jobs/{ingest_id}"),
        client.get(f"/api/v2/jobs/{parse_id}"),
        client.get(f"/api/v2/jobs/{mapping_id}"),
        client.get(f"/api/v2/workspaces/{WORKSPACE_ID}/jobs"),
        client.get(f"/api/v2/sources/{source_id}"),
        client.get(f"/api/v2/workspaces/{WORKSPACE_ID}/sources"),
    ]
    assert all(response.status_code == 200 for response in responses)
    serialized = json.dumps(
        [response.json() for response in responses], ensure_ascii=False
    )
    for private in (
        "OperationalError",
        "RAW_SQL_FAILURE",
        "private_patron_export",
        "library.sqlite3",
        "SELECT",
        "borrowers",
    ):
        assert private not in serialized
    for job_id in (ingest_id, parse_id):
        payload = client.get(f"/api/v2/jobs/{job_id}").json()
        assert payload["error"] == {
            "type": "JobFailure",
            "code": "JOB_FAILED",
            "message": "작업을 처리하지 못했습니다. 다시 시도해 주세요.",
        }
        assert payload["items"][0]["error"] == {
            "type": "ParserFailure",
            "code": "PARSER_FAILURE",
            "message": "파일 내용을 읽지 못했습니다. 다시 읽어 주세요.",
        }
    mapping_payload = client.get(f"/api/v2/jobs/{mapping_id}").json()
    assert mapping_payload["items"][0]["error"] == {
        "type": "MappingRequired",
        "code": "MAPPING_REQUIRED",
        "message": "열 이름과 자료 내용을 확인해 연결해 주세요.",
    }
    assert mapping_payload["items"][0]["mapping_required"] is None


def test_candidate_page_uses_one_read_snapshot_for_revision_count_summary_and_rows(
    tmp_path: Path,
) -> None:
    """A concurrent v2-to-v3 move cannot split one page across SQLite snapshots."""
    database_path = tmp_path / "candidate-snapshot" / "workflow.sqlite3"
    fixture = make_workflow_fixture(tmp_path / "candidate-snapshot")
    candidate_id = fixture.add_candidate(
        title="동시에 옮긴 책",
        author="저자",
        isbn=None,
        outcome="NEEDS_REVIEW",
    )
    reader = fixture.connection
    writer = connect(database_path)
    revision_before = int(
        reader.execute(
            """
            SELECT candidate_collection_revision FROM acquisition_workspaces
            WHERE id = ?
            """,
            (fixture.workspace_id,),
        ).fetchone()[0]
    )
    moved = False

    def move_before_summary(sql: str) -> None:
        nonlocal moved
        if moved or "SUM(CASE WHEN outcome" not in sql:
            return
        moved = True
        writer.execute(
            """
            UPDATE candidate_decisions
            SET outcome = 'EXCLUDED', row_version = row_version + 1
            WHERE id = ?
            """,
            (candidate_id,),
        )
        writer.commit()

    reader.set_trace_callback(move_before_summary)
    user = AuthenticatedUser(
        id=fixture.operator_id,
        school_id=fixture.school_id,
        username="operator",
        display_name="담당자",
        roles=("OPERATOR",),
        session_id="session",
        csrf_token_digest="digest",
    )
    try:
        response = list_candidates(
            fixture.workspace_id,
            user,
            reader,
            outcome="NEEDS_REVIEW",
            search=None,
            cursor=None,
            limit=100,
        )
    finally:
        reader.set_trace_callback(None)
        writer.close()

    assert moved
    assert response["workspace_revision"] == revision_before
    assert response["total_count"] == 1
    assert response["summary"]["needs_review_count"] == 1
    assert response["summary"]["excluded_count"] == 0
    assert [item["id"] for item in response["items"]] == [candidate_id]


def test_candidate_mutation_and_workspace_revision_are_atomic(tmp_path: Path) -> None:
    fixture = make_workflow_fixture(tmp_path / "candidate-revision-atomic")
    candidate_id = fixture.add_candidate(
        title="원자 revision 책",
        author="저자",
        isbn=None,
        outcome="NEEDS_REVIEW",
    )
    connection = fixture.connection
    before = int(
        connection.execute(
            "SELECT candidate_collection_revision FROM acquisition_workspaces WHERE id = ?",
            (fixture.workspace_id,),
        ).fetchone()[0]
    )

    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        "UPDATE candidate_decisions SET outcome = 'EXCLUDED' WHERE id = ?",
        (candidate_id,),
    )
    assert (
        connection.execute(
            "SELECT candidate_collection_revision FROM acquisition_workspaces WHERE id = ?",
            (fixture.workspace_id,),
        ).fetchone()[0]
        == before + 1
    )
    connection.rollback()

    assert (
        connection.execute(
            "SELECT outcome FROM candidate_decisions WHERE id = ?", (candidate_id,)
        ).fetchone()[0]
        == "NEEDS_REVIEW"
    )
    assert (
        connection.execute(
            "SELECT candidate_collection_revision FROM acquisition_workspaces WHERE id = ?",
            (fixture.workspace_id,),
        ).fetchone()[0]
        == before
    )


def test_candidate_workspace_move_advances_both_collection_revisions(
    tmp_path: Path,
) -> None:
    fixture = make_workflow_fixture(tmp_path / "candidate-revision-move")
    candidate_id = fixture.add_candidate(
        title="workspace 이동 책",
        author="저자",
        isbn=None,
        outcome="NEEDS_REVIEW",
    )
    connection = fixture.connection
    second_workspace_id = str(uuid.uuid4())
    second_recommendation_id = str(uuid.uuid4())

    workspace = connection.execute(
        "SELECT * FROM acquisition_workspaces WHERE id = ?", (fixture.workspace_id,)
    ).fetchone()
    assert workspace is not None
    workspace_columns = list(workspace.keys())
    workspace_values = dict(workspace)
    workspace_values.update(
        id=second_workspace_id,
        name="revision 이동 대상",
        candidate_collection_revision=0,
    )
    connection.execute(
        f"INSERT INTO acquisition_workspaces ({', '.join(workspace_columns)}) "
        f"VALUES ({', '.join('?' for _ in workspace_columns)})",
        tuple(workspace_values[column] for column in workspace_columns),
    )

    recommendation = connection.execute(
        """
        SELECT recommendation.*
        FROM recommendations AS recommendation
        JOIN candidate_decisions AS candidate
          ON candidate.recommendation_id = recommendation.id
        WHERE candidate.id = ?
        """,
        (candidate_id,),
    ).fetchone()
    assert recommendation is not None
    recommendation_columns = list(recommendation.keys())
    recommendation_values = dict(recommendation)
    recommendation_values.update(
        id=second_recommendation_id,
        workspace_id=second_workspace_id,
    )
    connection.execute(
        f"INSERT INTO recommendations ({', '.join(recommendation_columns)}) "
        f"VALUES ({', '.join('?' for _ in recommendation_columns)})",
        tuple(recommendation_values[column] for column in recommendation_columns),
    )
    connection.commit()

    before = {
        str(row["id"]): int(row["candidate_collection_revision"])
        for row in connection.execute(
            """
            SELECT id, candidate_collection_revision
            FROM acquisition_workspaces WHERE id IN (?, ?)
            """,
            (fixture.workspace_id, second_workspace_id),
        ).fetchall()
    }
    connection.execute(
        """
        UPDATE candidate_decisions
        SET workspace_id = ?, recommendation_id = ?
        WHERE id = ?
        """,
        (second_workspace_id, second_recommendation_id, candidate_id),
    )
    connection.commit()

    after = {
        str(row["id"]): int(row["candidate_collection_revision"])
        for row in connection.execute(
            """
            SELECT id, candidate_collection_revision
            FROM acquisition_workspaces WHERE id IN (?, ?)
            """,
            (fixture.workspace_id, second_workspace_id),
        ).fetchall()
    }
    assert after == {
        fixture.workspace_id: before[fixture.workspace_id] + 1,
        second_workspace_id: before[second_workspace_id] + 1,
    }
