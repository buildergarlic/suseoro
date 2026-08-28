from __future__ import annotations

import importlib
import importlib.util
import json
import sqlite3
import uuid
from types import SimpleNamespace

import pytest

from suseoro.db.connection import connect
from suseoro.db.migrations import apply_migrations

SCHOOL_ID = "30000000-0000-4000-8000-000000000001"
OTHER_SCHOOL_ID = "30000000-0000-4000-8000-000000000002"
WORKSPACE_ID = "30000000-0000-4000-8000-000000000003"
NOW = "2026-08-28T00:00:00Z"


def _api() -> SimpleNamespace:
    modules = (
        "suseoro.catalog.contracts",
        "suseoro.catalog.sync",
        "suseoro.services.comparison",
        "suseoro.jobs.repository",
    )
    missing = [name for name in modules if importlib.util.find_spec(name) is None]
    assert not missing, f"Task 5 modules are not implemented: {', '.join(missing)}"
    contracts = importlib.import_module(modules[0])
    sync = importlib.import_module(modules[1])
    comparison = importlib.import_module(modules[2])
    jobs = importlib.import_module(modules[3])
    return SimpleNamespace(
        CatalogRecord=contracts.CatalogRecord,
        CatalogSyncService=lambda connection: sync.CatalogSyncService(
            connection, _allow_unbound_sources=True
        ),
        ComparisonService=comparison.ComparisonService,
        JobRepository=jobs.JobRepository,
        SourceType=contracts.SourceType,
    )


def _database(tmp_path):
    connection = connect(tmp_path / "comparison.sqlite3")
    apply_migrations(connection)
    for school_id, name in (
        (SCHOOL_ID, "비교 테스트 학교"),
        (OTHER_SCHOOL_ID, "다른 학교"),
    ):
        connection.execute(
            "INSERT INTO schools (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (school_id, name, NOW, NOW),
        )
    connection.execute(
        """
        INSERT INTO acquisition_workspaces (
            id, school_id, name, status, created_at, updated_at
        ) VALUES (?, ?, ?, 'DRAFT', ?, ?)
        """,
        (WORKSPACE_ID, SCHOOL_ID, "2026년 2학기", NOW, NOW),
    )
    return connection


def _source_document(
    connection,
    *,
    school_id: str = SCHOOL_ID,
    document_status: str = "SUCCESS",
    rows: list[dict],
    sha_digit: str,
) -> tuple[str, list[str]]:
    source_file_id = str(uuid.uuid4())
    document_id = str(uuid.uuid4())
    connection.execute(
        """
        INSERT INTO source_files (
            id, sha256, size_bytes, storage_path, original_filename,
            detected_format, created_at
        ) VALUES (?, ?, 10, ?, ?, 'XLSX', ?)
        """,
        (
            source_file_id,
            sha_digit * 64,
            f"immutable/{sha_digit * 64}",
            f"source-{sha_digit}.xlsx",
            NOW,
        ),
    )
    completed = NOW if document_status != "PENDING" else None
    connection.execute(
        """
        INSERT INTO source_documents (
            id, source_file_id, school_id, role, parser_version, status,
            detected_format, created_at, completed_at
        ) VALUES (?, ?, ?, 'PURCHASE_REQUEST', 'tabular-v1', ?, 'XLSX', ?, ?)
        """,
        (document_id, source_file_id, school_id, document_status, NOW, completed),
    )
    row_ids: list[str] = []
    for index, row in enumerate(rows, start=1):
        row_id = str(uuid.uuid4())
        row_ids.append(row_id)
        status = row.get("status", "SUCCESS")
        fields = row.get("fields", {})
        raw = row.get("raw", fields)
        connection.execute(
            """
            INSERT INTO source_rows (
                id, source_document_id, sheet_name, source_row, status,
                raw_json, fields_json, warnings_json, error_code,
                error_message, created_at
            ) VALUES (?, ?, '도서목록', ?, ?, ?, ?, '[]', ?, ?, ?)
            """,
            (
                row_id,
                document_id,
                index,
                status,
                json.dumps(raw, ensure_ascii=False),
                json.dumps(fields, ensure_ascii=False),
                row.get("error_code"),
                row.get("error_message"),
                NOW,
            ),
        )
    return document_id, row_ids


def _field(value, raw=None):
    return {"value": value, "raw_value": value if raw is None else raw, "warnings": []}


def test_comparison_accounts_every_logical_row_and_keeps_file_partial_success(
    tmp_path,
) -> None:
    """Skipping parser errors or rolling back sibling files would make totals lie."""
    api = _api()
    with _database(tmp_path) as connection:
        api.CatalogSyncService(connection).import_full_snapshot(
            school_id=SCHOOL_ID,
            source_type=api.SourceType.DLS_MARC,
            records=(
                api.CatalogRecord(
                    source_item_id="H-001",
                    isbn="9780306406157",
                    title="이미 있는 책",
                    authors=("김하늘",),
                ),
            ),
            confirm_anomaly=True,
        )
        partial_document, partial_rows = _source_document(
            connection,
            document_status="ROW_ERROR",
            sha_digit="a",
            rows=[
                {
                    "fields": {
                        "isbn": _field("978-0-306-40615-7"),
                        "title": _field("이미 있는 책"),
                        "author": _field("김하늘"),
                    },
                    "raw": {"ISBN": "978-0-306-40615-7", "자료명": "이미 있는 책"},
                },
                {
                    "status": "ROW_ERROR",
                    "raw": {"자료명": ""},
                    "error_code": "MISSING_TITLE",
                    "error_message": "자료명이 없습니다",
                },
            ],
        )
        success_document, success_rows = _source_document(
            connection,
            sha_digit="b",
            rows=[
                {
                    "fields": {
                        "title": _field("새로운 수서 후보"),
                        "author": _field("새 저자"),
                    }
                }
            ],
        )
        failed_document, failed_rows = _source_document(
            connection,
            document_status="FAILED",
            sha_digit="c",
            rows=[
                {
                    "status": "ROW_ERROR",
                    "raw": {"byte_offset": 27},
                    "error_code": "STRUCTURE_UNTRUSTWORTHY",
                    "error_message": "파일 구조를 신뢰할 수 없습니다",
                }
            ],
        )

        summary = api.ComparisonService(connection).compare_documents(
            school_id=SCHOOL_ID,
            workspace_id=WORKSPACE_ID,
            source_document_ids=(
                partial_document,
                success_document,
                failed_document,
            ),
        )
        outcomes = {
            row["source_row_id"]: row["outcome"]
            for row in connection.execute(
                "SELECT source_row_id, outcome FROM comparison_row_results WHERE workspace_id = ?",
                (WORKSPACE_ID,),
            )
        }
        file_statuses = {
            row["source_document_id"]: row["status"]
            for row in connection.execute(
                "SELECT source_document_id, status FROM comparison_file_results WHERE workspace_id = ?",
                (WORKSPACE_ID,),
            )
        }

    assert summary.total_rows == 4
    assert summary.counts == {
        "CANDIDATE": 1,
        "NEEDS_REVIEW": 0,
        "EXCLUDED": 1,
        "ROW_ERROR": 2,
    }
    assert outcomes[partial_rows[0]] == "EXCLUDED"
    assert outcomes[partial_rows[1]] == "ROW_ERROR"
    assert outcomes[success_rows[0]] == "CANDIDATE"
    assert outcomes[failed_rows[0]] == "ROW_ERROR"
    assert file_statuses == {
        partial_document: "PARTIAL",
        success_document: "SUCCESS",
        failed_document: "FAILED",
    }


def test_structural_rows_never_become_recommendations_or_candidate_decisions(
    tmp_path,
) -> None:
    """Blurring ROW_ERROR into NEEDS_REVIEW would violate source-row accounting."""
    api = _api()
    with _database(tmp_path) as connection:
        document_id, row_ids = _source_document(
            connection,
            document_status="ROW_ERROR",
            sha_digit="d",
            rows=[
                {
                    "status": "ROW_ERROR",
                    "raw": {"페이지": 2, "원문": "깨진 구조"},
                    "error_code": "PDF_ROW_DAMAGED",
                    "error_message": "행을 읽을 수 없습니다",
                }
            ],
        )

        api.ComparisonService(connection).compare_documents(
            school_id=SCHOOL_ID,
            workspace_id=WORKSPACE_ID,
            source_document_ids=(document_id,),
        )
        recommendation_count = connection.execute(
            "SELECT COUNT(*) FROM recommendations WHERE source_row_id = ?",
            (row_ids[0],),
        ).fetchone()[0]
        decision_count = connection.execute(
            """
            SELECT COUNT(*) FROM candidate_decisions cd
            JOIN recommendations r ON r.id = cd.recommendation_id
            WHERE r.source_row_id = ?
            """,
            (row_ids[0],),
        ).fetchone()[0]
        result = connection.execute(
            "SELECT outcome, reason FROM comparison_row_results WHERE source_row_id = ?",
            (row_ids[0],),
        ).fetchone()

    assert recommendation_count == 0
    assert decision_count == 0
    assert (result["outcome"], result["reason"]) == (
        "ROW_ERROR",
        "PDF_ROW_DAMAGED",
    )


def test_comparison_persists_reason_evidence_and_original_sha_provenance(
    tmp_path,
) -> None:
    """Dropping evidence or canonical source lineage would make review unauditable."""
    api = _api()
    with _database(tmp_path) as connection:
        api.CatalogSyncService(connection).import_full_snapshot(
            school_id=SCHOOL_ID,
            source_type=api.SourceType.DLS_MARC,
            records=(
                api.CatalogRecord(
                    source_item_id="H-001",
                    isbn="9780306406157",
                    title="원문 보존",
                    authors=("저자",),
                ),
            ),
            confirm_anomaly=True,
        )
        document_id, row_ids = _source_document(
            connection,
            sha_digit="e",
            rows=[
                {
                    "fields": {
                        "isbn": _field("9780306406157"),
                        "title": _field("원문 보존"),
                        "author": _field("저자 지음", raw="저자 지음"),
                    },
                    "raw": {"ISBN 원문": "9780306406157", "저자 원문": "저자 지음"},
                }
            ],
        )

        api.ComparisonService(connection).compare_documents(
            school_id=SCHOOL_ID,
            workspace_id=WORKSPACE_ID,
            source_document_ids=(document_id,),
        )
        persisted = connection.execute(
            """
            SELECT cd.outcome, cd.reason, cd.evidence_holding_id,
                   r.original_json, r.source_row_id, sf.sha256,
                   sd.parser_version, sr.sheet_name, sr.source_row
            FROM candidate_decisions cd
            JOIN recommendations r ON r.id = cd.recommendation_id
            JOIN source_rows sr ON sr.id = r.source_row_id
            JOIN source_documents sd ON sd.id = sr.source_document_id
            JOIN source_files sf ON sf.id = sd.source_file_id
            WHERE r.source_row_id = ?
            """,
            (row_ids[0],),
        ).fetchone()

    assert persisted["outcome"] == "EXCLUDED"
    assert persisted["reason"] == "EXACT_VALID_ISBN"
    assert persisted["evidence_holding_id"]
    assert json.loads(persisted["original_json"]) == {
        "ISBN 원문": "9780306406157",
        "저자 원문": "저자 지음",
    }
    assert persisted["source_row_id"] == row_ids[0]
    assert persisted["sha256"] == "e" * 64
    assert persisted["parser_version"] == "tabular-v1"
    assert (persisted["sheet_name"], persisted["source_row"]) == ("도서목록", 1)


def test_comparison_is_school_scoped_and_idempotent_on_rerun(tmp_path) -> None:
    """Cross-school matching or duplicate rerun rows would expose/corrupt data."""
    api = _api()
    with _database(tmp_path) as connection:
        api.CatalogSyncService(connection).import_full_snapshot(
            school_id=OTHER_SCHOOL_ID,
            source_type=api.SourceType.DLS_MARC,
            records=(
                api.CatalogRecord(
                    source_item_id="OTHER-001",
                    isbn="9780306406157",
                    title="다른 학교 책",
                    authors=("타교 저자",),
                ),
            ),
            confirm_anomaly=True,
        )
        document_id, row_ids = _source_document(
            connection,
            sha_digit="f",
            rows=[
                {
                    "fields": {
                        "isbn": _field("9780306406157"),
                        "title": _field("다른 학교 책"),
                        "author": _field("타교 저자"),
                    }
                }
            ],
        )
        service = api.ComparisonService(connection)

        first = service.compare_documents(
            school_id=SCHOOL_ID,
            workspace_id=WORKSPACE_ID,
            source_document_ids=(document_id,),
        )
        second = service.compare_documents(
            school_id=SCHOOL_ID,
            workspace_id=WORKSPACE_ID,
            source_document_ids=(document_id,),
        )
        recommendation_count = connection.execute(
            "SELECT COUNT(*) FROM recommendations WHERE workspace_id = ?",
            (WORKSPACE_ID,),
        ).fetchone()[0]
        result_count = connection.execute(
            "SELECT COUNT(*) FROM comparison_row_results WHERE workspace_id = ?",
            (WORKSPACE_ID,),
        ).fetchone()[0]
        outcome = connection.execute(
            "SELECT outcome FROM comparison_row_results WHERE source_row_id = ?",
            (row_ids[0],),
        ).fetchone()[0]

    assert first.counts["CANDIDATE"] == 1
    assert second.counts == first.counts
    assert recommendation_count == 1
    assert result_count == 1
    assert outcome == "CANDIDATE"


def test_file_status_is_failed_when_all_rows_error_or_no_logical_rows(tmp_path) -> None:
    """PARTIAL is reserved for a real mixture of successful and failed rows."""
    api = _api()
    with _database(tmp_path) as connection:
        all_error, _ = _source_document(
            connection,
            document_status="ROW_ERROR",
            sha_digit="1",
            rows=[
                {
                    "status": "ROW_ERROR",
                    "error_code": "BROKEN_ONE",
                    "error_message": "첫 행 손상",
                },
                {
                    "status": "ROW_ERROR",
                    "error_code": "BROKEN_TWO",
                    "error_message": "둘째 행 손상",
                },
            ],
        )
        empty, _ = _source_document(
            connection,
            document_status="SUCCESS",
            sha_digit="2",
            rows=[],
        )

        summary = api.ComparisonService(connection).compare_documents(
            school_id=SCHOOL_ID,
            workspace_id=WORKSPACE_ID,
            source_document_ids=(all_error, empty),
        )
        errors = {
            row["source_document_id"]: json.loads(row["error_json"])
            for row in connection.execute(
                """
                SELECT source_document_id, error_json
                FROM comparison_file_results
                WHERE workspace_id = ?
                """,
                (WORKSPACE_ID,),
            )
        }

    assert summary.file_statuses == {all_error: "FAILED", empty: "FAILED"}
    assert errors[all_error]["code"] == "ALL_LOGICAL_ROWS_FAILED"
    assert errors[empty]["code"] == "NO_LOGICAL_ROWS"


def test_supplied_job_must_match_comparison_school_workspace_and_type(tmp_path) -> None:
    """Attaching comparison rows to an unrelated durable job corrupts recovery scope."""
    api = _api()
    with _database(tmp_path) as connection:
        document_id, _ = _source_document(
            connection,
            sha_digit="3",
            rows=[{"fields": {"title": _field("후보")}}],
        )
        unrelated = api.JobRepository(connection).create(
            school_id=SCHOOL_ID,
            workspace_id=WORKSPACE_ID,
            job_type="EXPORT",
            payload={},
        )

        with pytest.raises(ValueError, match="COMPARE"):
            api.ComparisonService(connection).compare_documents(
                school_id=SCHOOL_ID,
                workspace_id=WORKSPACE_ID,
                source_document_ids=(document_id,),
                job_id=unrelated.id,
            )

    assert (
        connection.execute(
            "SELECT COUNT(*) FROM job_file_results WHERE job_id = ?",
            (unrelated.id,),
        ).fetchone()[0]
        == 0
    )


def test_database_rejects_cross_school_comparison_graph_writes(tmp_path) -> None:
    """Service-only checks are insufficient when direct SQL can cross tenant boundaries."""
    with _database(tmp_path) as connection:
        document_id, row_ids = _source_document(
            connection,
            school_id=OTHER_SCHOOL_ID,
            sha_digit="4",
            rows=[{"fields": {"title": _field("타교")}}],
        )
        with pytest.raises(sqlite3.IntegrityError, match="scope"):
            connection.execute(
                """
                INSERT INTO recommendations (
                    id, school_id, workspace_id, source_document_id, source_row_id,
                    original_title, original_authors_json, original_json,
                    title_key, subtitle_key, author_key, publisher_key,
                    volume_key, edition_key, series_key, created_at
                ) VALUES (?, ?, ?, ?, ?, '타교', '[]', '{}', '타교', '', '', '', '', '', '', ?)
                """,
                (
                    str(uuid.uuid4()),
                    SCHOOL_ID,
                    WORKSPACE_ID,
                    document_id,
                    row_ids[0],
                    NOW,
                ),
            )
        own_document, _ = _source_document(
            connection,
            school_id=SCHOOL_ID,
            sha_digit="5",
            rows=[{"fields": {"title": _field("본교")}}],
        )
        _api().ComparisonService(connection).compare_documents(
            school_id=SCHOOL_ID,
            workspace_id=WORKSPACE_ID,
            source_document_ids=(own_document,),
        )
        recommendation_id = connection.execute(
            "SELECT id FROM recommendations WHERE source_document_id = ?",
            (own_document,),
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match="scope"):
            connection.execute(
                "UPDATE recommendations SET school_id = ? WHERE id = ?",
                (OTHER_SCHOOL_ID, recommendation_id),
            )
