from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import uuid
from pathlib import Path

import pytest
from workflow_fixtures import NOW, WorkflowFixture, make_workflow_fixture

from suseoro.backup.service import BackupService
from suseoro.db import migrations as migration_module
from suseoro.db.connection import connect
from suseoro.db.migrations import apply_migrations

PRELUDE_ID = "0010a_task8_round4_upgrade_prelude"
INTEGRITY_ID = "0012_task8_round4_integrity"
COMMITTED_0011_SHA256 = (
    "eb22db853d9488c3fdc0a95ad0eafd2e4c79884d41d6d313b272cb49822242b0"
)


def _migration_directory() -> Path:
    return Path(migration_module.__file__).with_name("migrations")


def _copy_migrations(destination: Path, *, exclude: set[str] | None = None) -> Path:
    destination.mkdir()
    excluded = {
        "0010b_task8_round5_count_capture",
        "0013_task8_round5_count_restoration",
        "0014_task8_round5_candidate_revision",
        "0015_task8_round5_mapping_provenance",
        "0016_task9_prerequisite_integrity",
        "0017_task9_procurement_file_imports",
        "0018_task9_round1_receiving_links",
        *(exclude or set()),
    }
    for source in _migration_directory().glob("*.sql"):
        if source.stem not in excluded:
            shutil.copy2(source, destination / source.name)
    return destination


def _source_id(fixture: WorkflowFixture) -> str:
    return str(
        fixture.connection.execute(
            "SELECT id FROM source_documents WHERE school_id = ? ORDER BY id LIMIT 1",
            (fixture.school_id,),
        ).fetchone()["id"]
    )


def _seed_source_row(
    fixture: WorkflowFixture, *, source_id: str, source_row: int, status: str
) -> None:
    fixture.connection.execute(
        """
        INSERT INTO source_rows (
            id, source_document_id, source_row, status, raw_json,
            fields_json, warnings_json, error_code, error_message, created_at
        ) VALUES (?, ?, ?, ?, '{}', '{}', '[]', ?, ?, ?)
        """,
        (
            str(uuid.uuid4()),
            source_id,
            source_row,
            status,
            "ROW_ERROR" if status == "ROW_ERROR" else None,
            "확인 필요" if status == "ROW_ERROR" else None,
            NOW,
        ),
    )


def _seed_terminal_file_result(
    fixture: WorkflowFixture,
    *,
    source_id: str,
    total_rows: int,
    processed_rows: int,
    status: str = "PARTIAL",
    error: dict[str, object] | None = None,
    row_error_count: int | None = None,
) -> tuple[str, str]:
    job_id = str(uuid.uuid4())
    result_id = str(uuid.uuid4())
    claim_token = str(uuid.uuid4())
    fixture.connection.execute(
        """
        INSERT INTO durable_jobs (
            id, school_id, workspace_id, job_type, status, stage,
            payload_json, progress_current, progress_total, retry_count,
            created_at, updated_at, claim_token, claim_generation
        ) VALUES (?, ?, ?, 'INGEST', 'RUNNING', 'PARSING', ?, 0, 1, 0,
                  ?, ?, ?, 1)
        """,
        (
            job_id,
            fixture.school_id,
            fixture.workspace_id,
            json.dumps({"source_document_ids": [source_id]}),
            NOW,
            NOW,
            claim_token,
        ),
    )
    columns = {
        str(row[1])
        for row in fixture.connection.execute("PRAGMA table_info(job_file_results)")
    }
    if "row_error_count" in columns:
        fixture.connection.execute(
            """
            INSERT INTO job_file_results (
                id, job_id, source_document_id, status, total_rows,
                processed_rows, row_error_count, error_json, claim_token,
                claim_generation, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                result_id,
                job_id,
                source_id,
                status,
                total_rows,
                processed_rows,
                row_error_count or 0,
                json.dumps(error, ensure_ascii=False) if error else None,
                claim_token,
                NOW,
                NOW,
            ),
        )
    else:
        fixture.connection.execute(
            """
            INSERT INTO job_file_results (
                id, job_id, source_document_id, status, total_rows,
                processed_rows, error_json, claim_token, claim_generation,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                result_id,
                job_id,
                source_id,
                status,
                total_rows,
                processed_rows,
                json.dumps(error, ensure_ascii=False) if error else None,
                claim_token,
                NOW,
                NOW,
            ),
        )
    fixture.connection.execute(
        "UPDATE durable_jobs SET status = ?, stage = 'TERMINAL' WHERE id = ?",
        (status, job_id),
    )
    fixture.connection.commit()
    return job_id, result_id


def _mapping_required_error() -> dict[str, object]:
    return {
        "code": "MAPPING_REQUIRED",
        "message": "열 이름과 자료 내용을 확인해 연결해 주세요.",
        "mapping_required": {
            "headers": ["임의 열"],
            "preview_rows": [["아직 읽지 않은 책"]],
            "suggested_mapping": {"임의 열": None},
            "required_fields": ["title"],
            "confidence": 0.1,
            "questions": ["제목 열을 골라 주세요."],
        },
    }


def _result_counts(
    fixture: WorkflowFixture, result_ids: tuple[str, ...]
) -> dict[str, tuple[int, int, int, str]]:
    rows = fixture.connection.execute(
        """
        SELECT id, total_rows, processed_rows, row_error_count, status
        FROM job_file_results
        WHERE id IN ({})
        """.format(",".join("?" for _ in result_ids)),
        result_ids,
    ).fetchall()
    return {
        str(row["id"]): (
            int(row["total_rows"]),
            int(row["processed_rows"]),
            int(row["row_error_count"]),
            str(row["status"]),
        )
        for row in rows
    }


def test_round4_populated_0010_upgrade_preserves_mapping_required_as_unread(
    tmp_path: Path,
) -> None:
    """The 0011 backfill must not be blocked or turn unresolved preview rows into reads."""
    old_dir = _copy_migrations(
        tmp_path / "0010-migrations",
        exclude={"0011_task8_round3_integrity", PRELUDE_ID, INTEGRITY_ID},
    )
    fixture = make_workflow_fixture(tmp_path / "upgrade", migrations_dir=old_dir)
    source_id = _source_id(fixture)
    _, result_id = _seed_terminal_file_result(
        fixture,
        source_id=source_id,
        total_rows=1,
        processed_rows=0,
        error=_mapping_required_error(),
    )

    apply_migrations(fixture.connection, _migration_directory())

    assert _result_counts(fixture, (result_id,))[result_id] == (1, 0, 0, "PARTIAL")
    assert fixture.connection.execute("PRAGMA foreign_key_check").fetchall() == []
    migration_ids = [
        str(row[0])
        for row in fixture.connection.execute(
            "SELECT migration_id FROM schema_migrations ORDER BY migration_id"
        ).fetchall()
    ]
    assert migration_ids[-10:] == [
        PRELUDE_ID,
        "0010b_task8_round5_count_capture",
        "0011_task8_round3_integrity",
        INTEGRITY_ID,
        "0013_task8_round5_count_restoration",
        "0014_task8_round5_candidate_revision",
        "0015_task8_round5_mapping_provenance",
        "0016_task9_prerequisite_integrity",
        "0017_task9_procurement_file_imports",
        "0018_task9_round1_receiving_links",
    ]
    with pytest.raises(
        sqlite3.IntegrityError, match="job file result scope or claim mismatch"
    ):
        fixture.connection.execute(
            "UPDATE job_file_results SET processed_rows = 1 WHERE id = ?",
            (result_id,),
        )
    fixture.connection.rollback()


def test_round4_applies_missing_lower_sorted_prelude_after_0011_and_repairs_counts(
    tmp_path: Path,
) -> None:
    """A database that already recorded 0011 must still receive both round-4 migrations."""
    already_0011_dir = _copy_migrations(
        tmp_path / "already-0011-migrations", exclude={PRELUDE_ID, INTEGRITY_ID}
    )
    fixture = make_workflow_fixture(
        tmp_path / "already-0011", migrations_dir=already_0011_dir
    )
    source_id = _source_id(fixture)
    _, successful_result_id = _seed_terminal_file_result(
        fixture,
        source_id=source_id,
        status="SUCCESS",
        total_rows=2,
        processed_rows=2,
        row_error_count=0,
    )
    _, mapping_result_id = _seed_terminal_file_result(
        fixture,
        source_id=source_id,
        total_rows=1,
        processed_rows=1,
        row_error_count=0,
        error=_mapping_required_error(),
    )
    _seed_source_row(fixture, source_id=source_id, source_row=1, status="SUCCESS")
    _seed_source_row(fixture, source_id=source_id, source_row=2, status="ROW_ERROR")
    _, mixed_result_id = _seed_terminal_file_result(
        fixture,
        source_id=source_id,
        total_rows=2,
        processed_rows=2,
        row_error_count=0,
        error={"code": "ROW_ERRORS"},
    )
    before_rowid = int(
        fixture.connection.execute(
            "SELECT rowid FROM schema_migrations WHERE migration_id = '0011_task8_round3_integrity'"
        ).fetchone()[0]
    )

    apply_migrations(fixture.connection, _migration_directory())

    assert _result_counts(
        fixture, (successful_result_id, mapping_result_id, mixed_result_id)
    ) == {
        successful_result_id: (2, 2, 0, "SUCCESS"),
        mapping_result_id: (1, 0, 0, "PARTIAL"),
        mixed_result_id: (2, 2, 0, "PARTIAL"),
    }
    applied = {
        str(row["migration_id"]): (int(row["rowid"]), str(row["checksum"]))
        for row in fixture.connection.execute(
            """
            SELECT rowid, migration_id, checksum FROM schema_migrations
            WHERE migration_id IN (?, '0011_task8_round3_integrity', ?)
            """,
            (PRELUDE_ID, INTEGRITY_ID),
        ).fetchall()
    }
    assert applied[PRELUDE_ID][0] > before_rowid
    assert applied[INTEGRITY_ID][0] > applied[PRELUDE_ID][0]
    for migration_id in (PRELUDE_ID, "0011_task8_round3_integrity", INTEGRITY_ID):
        contents = (_migration_directory() / f"{migration_id}.sql").read_text(
            encoding="utf-8"
        )
        assert (
            applied[migration_id][1]
            == hashlib.sha256(contents.encode("utf-8")).hexdigest()
        )
    assert applied["0011_task8_round3_integrity"][1] == COMMITTED_0011_SHA256


def test_round5_already_0011_partial_is_preserved_but_marked_unverified(
    tmp_path: Path,
) -> None:
    already_0011_dir = _copy_migrations(
        tmp_path / "partial-history-0011", exclude={PRELUDE_ID, INTEGRITY_ID}
    )
    fixture = make_workflow_fixture(
        tmp_path / "partial-history-db", migrations_dir=already_0011_dir
    )
    source_id = _source_id(fixture)
    _, result_id = _seed_terminal_file_result(
        fixture,
        source_id=source_id,
        total_rows=2,
        processed_rows=2,
        row_error_count=0,
        error={"code": "ROW_ERRORS"},
    )
    _seed_source_row(fixture, source_id=source_id, source_row=1, status="SUCCESS")
    _seed_source_row(fixture, source_id=source_id, source_row=2, status="SUCCESS")
    fixture.connection.commit()

    apply_migrations(fixture.connection, _migration_directory())

    assert _result_counts(fixture, (result_id,)) == {result_id: (2, 2, 0, "PARTIAL")}
    assert (
        fixture.connection.execute(
            """
        SELECT confidence FROM job_file_result_count_history
        WHERE job_file_result_id = ?
        """,
            (result_id,),
        ).fetchone()[0]
        == "UNVERIFIED"
    )


def test_round4_fresh_database_has_complete_checksum_ledger_and_final_guards(
    tmp_path: Path,
) -> None:
    connection = connect(tmp_path / "fresh.sqlite3")
    apply_migrations(connection, _migration_directory())

    migration_ids = [
        str(row[0])
        for row in connection.execute(
            "SELECT migration_id FROM schema_migrations ORDER BY migration_id"
        ).fetchall()
    ]
    assert migration_ids[-10:] == [
        PRELUDE_ID,
        "0010b_task8_round5_count_capture",
        "0011_task8_round3_integrity",
        INTEGRITY_ID,
        "0013_task8_round5_count_restoration",
        "0014_task8_round5_candidate_revision",
        "0015_task8_round5_mapping_provenance",
        "0016_task9_prerequisite_integrity",
        "0017_task9_procurement_file_imports",
        "0018_task9_round1_receiving_links",
    ]
    assert {
        str(row[0])
        for row in connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'trigger' AND name IN (
                'job_file_results_scope_update',
                'job_file_result_counts_insert',
                'job_file_result_counts_update'
            )
            """
        ).fetchall()
    } == {
        "job_file_results_scope_update",
        "job_file_result_counts_insert",
        "job_file_result_counts_update",
    }
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_round4_incomplete_bundle_is_deferred_without_stranding_the_guard(
    tmp_path: Path,
) -> None:
    old_dir = _copy_migrations(
        tmp_path / "incomplete-old",
        exclude={"0011_task8_round3_integrity", PRELUDE_ID, INTEGRITY_ID},
    )
    fixture = make_workflow_fixture(tmp_path / "incomplete-db", migrations_dir=old_dir)
    source_id = _source_id(fixture)
    _seed_terminal_file_result(
        fixture,
        source_id=source_id,
        total_rows=1,
        processed_rows=0,
        error=_mapping_required_error(),
    )
    incomplete_dir = _copy_migrations(
        tmp_path / "incomplete-current", exclude={INTEGRITY_ID}
    )

    apply_migrations(fixture.connection, incomplete_dir)

    applied = {
        str(row[0])
        for row in fixture.connection.execute(
            "SELECT migration_id FROM schema_migrations"
        ).fetchall()
    }
    assert applied.isdisjoint({PRELUDE_ID, "0011_task8_round3_integrity", INTEGRITY_ID})
    assert "row_error_count" not in {
        str(row[1])
        for row in fixture.connection.execute("PRAGMA table_info(job_file_results)")
    }
    assert (
        fixture.connection.execute(
            """
            SELECT COUNT(*) FROM sqlite_master
            WHERE type = 'trigger' AND name = 'job_file_results_scope_update'
            """
        ).fetchone()[0]
        == 1
    )


def test_round4_incomplete_bundle_still_rejects_applied_checksum_drift(
    tmp_path: Path,
) -> None:
    already_0011_dir = _copy_migrations(
        tmp_path / "incomplete-checksum-old", exclude={PRELUDE_ID, INTEGRITY_ID}
    )
    fixture = make_workflow_fixture(
        tmp_path / "incomplete-checksum-db", migrations_dir=already_0011_dir
    )
    fixture.connection.execute(
        """
        UPDATE schema_migrations SET checksum = ?
        WHERE migration_id = '0011_task8_round3_integrity'
        """,
        ("0" * 64,),
    )
    fixture.connection.commit()
    incomplete_dir = _copy_migrations(
        tmp_path / "incomplete-checksum-current", exclude={INTEGRITY_ID}
    )

    with pytest.raises(
        migration_module.MigrationChecksumMismatch,
        match="0011_task8_round3_integrity",
    ):
        apply_migrations(fixture.connection, incomplete_dir)

    assert (
        fixture.connection.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE migration_id = ?",
            (PRELUDE_ID,),
        ).fetchone()[0]
        == 0
    )


def test_round4_missing_prelude_after_postlude_reruns_restoration(
    tmp_path: Path,
) -> None:
    connection = connect(tmp_path / "missing-prelude-after-postlude.sqlite3")
    apply_migrations(connection, _migration_directory())
    connection.execute(
        "DELETE FROM schema_migrations WHERE migration_id = ?", (PRELUDE_ID,)
    )
    connection.commit()

    apply_migrations(connection, _migration_directory())

    assert (
        connection.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE migration_id = ?",
            (PRELUDE_ID,),
        ).fetchone()[0]
        == 1
    )
    assert (
        connection.execute(
            """
        SELECT COUNT(*) FROM sqlite_master
        WHERE type = 'trigger' AND name = 'job_file_results_scope_update'
        """
        ).fetchone()[0]
        == 1
    )


def test_round4_upgrade_bundle_rolls_back_prelude_and_0011_when_postlude_fails(
    tmp_path: Path,
) -> None:
    """A failed correction must not strand a recorded prelude with the claim guard absent."""
    old_dir = _copy_migrations(
        tmp_path / "failure-old",
        exclude={"0011_task8_round3_integrity", PRELUDE_ID, INTEGRITY_ID},
    )
    fixture = make_workflow_fixture(tmp_path / "failure-db", migrations_dir=old_dir)
    source_id = _source_id(fixture)
    _seed_terminal_file_result(
        fixture,
        source_id=source_id,
        total_rows=1,
        processed_rows=0,
        error=_mapping_required_error(),
    )
    failing_dir = _copy_migrations(
        tmp_path / "failure-current",
        exclude={"0011_task8_round3_integrity", PRELUDE_ID, INTEGRITY_ID},
    )
    (failing_dir / f"{PRELUDE_ID}.sql").write_text(
        "DROP TRIGGER IF EXISTS job_file_results_scope_update;\n", encoding="utf-8"
    )
    shutil.copy2(
        _migration_directory() / "0011_task8_round3_integrity.sql",
        failing_dir / "0011_task8_round3_integrity.sql",
    )
    (failing_dir / f"{INTEGRITY_ID}.sql").write_text(
        "SELECT * FROM deliberately_missing_round4_table;\n", encoding="utf-8"
    )

    with pytest.raises(sqlite3.OperationalError, match="deliberately_missing"):
        apply_migrations(fixture.connection, failing_dir)

    assert {
        str(row[0])
        for row in fixture.connection.execute(
            "SELECT migration_id FROM schema_migrations"
        ).fetchall()
    }.isdisjoint({PRELUDE_ID, "0011_task8_round3_integrity", INTEGRITY_ID})
    assert "row_error_count" not in {
        str(row[1])
        for row in fixture.connection.execute("PRAGMA table_info(job_file_results)")
    }
    assert (
        fixture.connection.execute(
            """
            SELECT COUNT(*) FROM sqlite_master
            WHERE type = 'trigger' AND name = 'job_file_results_scope_update'
            """
        ).fetchone()[0]
        == 1
    )


def test_round4_already_0011_bundle_rolls_back_missing_prelude_when_postlude_fails(
    tmp_path: Path,
) -> None:
    """A failed postlude must not damage an already-applied 0011 database."""
    old_dir = _copy_migrations(
        tmp_path / "failure-already-0011-old", exclude={PRELUDE_ID, INTEGRITY_ID}
    )
    fixture = make_workflow_fixture(
        tmp_path / "failure-already-0011-db", migrations_dir=old_dir
    )
    source_id = _source_id(fixture)
    _, result_id = _seed_terminal_file_result(
        fixture,
        source_id=source_id,
        total_rows=1,
        processed_rows=1,
        row_error_count=0,
        error=_mapping_required_error(),
    )
    before_ledger = {
        str(row["migration_id"]): str(row["checksum"])
        for row in fixture.connection.execute(
            "SELECT migration_id, checksum FROM schema_migrations"
        ).fetchall()
    }

    failing_dir = _copy_migrations(
        tmp_path / "failure-already-0011-current",
        exclude={PRELUDE_ID, INTEGRITY_ID},
    )
    (failing_dir / f"{PRELUDE_ID}.sql").write_text(
        "DROP TRIGGER IF EXISTS job_file_results_scope_update;\n", encoding="utf-8"
    )
    (failing_dir / f"{INTEGRITY_ID}.sql").write_text(
        "SELECT * FROM deliberately_missing_round4_table;\n", encoding="utf-8"
    )

    with pytest.raises(sqlite3.OperationalError, match="deliberately_missing"):
        apply_migrations(fixture.connection, failing_dir)

    after_ledger = {
        str(row["migration_id"]): str(row["checksum"])
        for row in fixture.connection.execute(
            "SELECT migration_id, checksum FROM schema_migrations"
        ).fetchall()
    }
    assert after_ledger == before_ledger
    assert COMMITTED_0011_SHA256 == after_ledger["0011_task8_round3_integrity"]
    assert _result_counts(fixture, (result_id,)) == {result_id: (1, 1, 0, "PARTIAL")}
    assert "row_error_count" in {
        str(row[1])
        for row in fixture.connection.execute("PRAGMA table_info(job_file_results)")
    }
    assert (
        fixture.connection.execute(
            """
            SELECT COUNT(*) FROM sqlite_master
            WHERE type = 'trigger' AND name = 'job_file_results_scope_update'
            """
        ).fetchone()[0]
        == 1
    )


def test_round4_restore_accepts_checksum_valid_0011_history_from_before_prelude() -> (
    None
):
    """A real 0011 backup predates the lower-sorted prelude but remains upgradeable."""
    bundled = BackupService._bundled_schema()
    historical_0011 = tuple(
        item
        for item in bundled
        if item["version"] <= "0011_task8_round3_integrity"
        and item["version"] not in {PRELUDE_ID, "0010b_task8_round5_count_capture"}
    )

    BackupService._validate_migration_history(historical_0011)
