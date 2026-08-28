from __future__ import annotations

import importlib
import importlib.util
import json
import shutil
import sqlite3
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import suseoro.db.migrations as migration_module
from suseoro.db.connection import connect
from suseoro.db.migrations import MigrationChecksumMismatch, apply_migrations

SCHOOL_ID = "20000000-0000-4000-8000-000000000001"
OTHER_SCHOOL_ID = "20000000-0000-4000-8000-000000000002"
NOW = "2026-08-28T00:00:00Z"


def _api() -> SimpleNamespace:
    modules = (
        "suseoro.catalog.contracts",
        "suseoro.catalog.repository",
        "suseoro.catalog.sync",
    )
    missing = [name for name in modules if importlib.util.find_spec(name) is None]
    assert not missing, f"Task 5 modules are not implemented: {', '.join(missing)}"
    contracts = importlib.import_module(modules[0])
    repository = importlib.import_module(modules[1])
    sync = importlib.import_module(modules[2])
    return SimpleNamespace(
        ActivationConfirmationRequired=sync.ActivationConfirmationRequired,
        CatalogRecord=contracts.CatalogRecord,
        CatalogRepository=repository.CatalogRepository,
        CatalogSyncService=lambda connection: sync.CatalogSyncService(
            connection, _allow_unbound_sources=True
        ),
        ProductionCatalogSyncService=sync.CatalogSyncService,
        CatalogValidationError=sync.CatalogValidationError,
        DeltaFile=contracts.DeltaFile,
        DeltaWindow=contracts.DeltaWindow,
        FullSnapshotRequired=sync.FullSnapshotRequired,
        HoldingStatus=contracts.HoldingStatus,
        ParserStatus=contracts.ParserStatus,
        SourcePolicyError=sync.SourcePolicyError,
        SourceType=contracts.SourceType,
    )


def _database(tmp_path):
    connection = connect(tmp_path / "catalog.sqlite3")
    apply_migrations(connection)
    connection.execute(
        "INSERT INTO schools (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (SCHOOL_ID, "장서 테스트 학교", NOW, NOW),
    )
    return connection


def _records(count: int, *, prefix: str = "H"):
    api = _api()
    return tuple(
        api.CatalogRecord(
            source_item_id=f"{prefix}-{index:03d}",
            isbn=None,
            title=f"원문 제목 {index}",
            subtitle=f"원문 부제 {index}",
            authors=(f"저자 {index}",),
            publisher="출판사",
            volume=str(index % 3 + 1),
            edition="초판",
            raw_fields={"원래열": f"원문 값 {index}"},
        )
        for index in range(count)
    )


def _delta_file(api, sha: str, records=(), status=None):
    return api.DeltaFile(
        source_file_sha256=sha * 64,
        parser_version="marc-v1",
        status=status or api.ParserStatus.SUCCESS,
        records=tuple(records),
        activation_allowed=(status or api.ParserStatus.SUCCESS)
        == api.ParserStatus.SUCCESS,
    )


def _marc_document(
    connection,
    *,
    role: str,
    sha_digit: str,
    rows: tuple[dict, ...] = (),
    school_id: str = SCHOOL_ID,
    status: str = "SUCCESS",
    activation_allowed: bool = True,
    window_start: date | None = None,
    window_end: date | None = None,
):
    if role in ("CATALOG_DELTA_REGISTRATION", "CATALOG_DELTA_UPDATE"):
        window_start = window_start or date(2026, 8, 18)
        window_end = window_end or date(2026, 8, 28)
    file_id = str(uuid.uuid4())
    document_id = str(uuid.uuid4())
    connection.execute(
        """
        INSERT INTO source_files (
            id, sha256, size_bytes, storage_path, original_filename,
            detected_format, created_at
        ) VALUES (?, ?, 10, ?, 'catalog.mrc', 'MARC', ?)
        """,
        (file_id, sha_digit * 64, f"catalog/{sha_digit}.mrc", NOW),
    )
    connection.execute(
        """
        INSERT INTO source_documents (
            id, source_file_id, school_id, role, parser_version, status,
            detected_format, activation_allowed, requested_start_local_date,
            requested_through_local_date, created_at, completed_at
        ) VALUES (?, ?, ?, ?, 'marc-v1', ?, 'MARC', ?, ?, ?, ?, ?)
        """,
        (
            document_id,
            file_id,
            school_id,
            role,
            status,
            int(activation_allowed),
            window_start.isoformat() if window_start else None,
            window_end.isoformat() if window_end else None,
            NOW,
            NOW,
        ),
    )
    row_ids = []
    for index, row in enumerate(rows, start=1):
        row_id = str(uuid.uuid4())
        row_ids.append(row_id)
        connection.execute(
            """
            INSERT INTO source_rows (
                id, source_document_id, source_row, status, raw_json,
                fields_json, warnings_json, error_code, error_message, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, '[]', ?, ?, ?)
            """,
            (
                row_id,
                document_id,
                index,
                row.get("status", "SUCCESS"),
                json.dumps(row.get("raw", {}), ensure_ascii=False),
                json.dumps(row.get("fields", {}), ensure_ascii=False),
                row.get("error_code"),
                row.get("error_message"),
                NOW,
            ),
        )
    return document_id, tuple(row_ids)


def _bind_delta_window(connection, document_id: str, *, start: date, end: date) -> None:
    connection.execute(
        """
        UPDATE source_documents
        SET requested_start_local_date = ?, requested_through_local_date = ?
        WHERE id = ?
        """,
        (start.isoformat(), end.isoformat(), document_id),
    )


def test_full_snapshot_requires_confirmation_for_zero_and_more_than_thirty_percent(
    tmp_path,
) -> None:
    """Silently activating empty or anomalously sized snapshots must fail."""
    api = _api()
    with _database(tmp_path) as connection:
        sync = api.CatalogSyncService(connection)
        first = sync.import_full_snapshot(
            school_id=SCHOOL_ID,
            source_type=api.SourceType.DLS_MARC,
            records=_records(10),
            confirm_anomaly=True,
        )
        empty = sync.stage_full_snapshot(
            school_id=SCHOOL_ID,
            source_type=api.SourceType.DLS_MARC,
            records=(),
        )

        with pytest.raises(api.ActivationConfirmationRequired) as zero_error:
            sync.activate_staged(empty.id)
        assert zero_error.value.reason == "EMPTY_SNAPSHOT"
        assert sync.repository.active_version(SCHOOL_ID).id == first.id

        larger = sync.stage_full_snapshot(
            school_id=SCHOOL_ID,
            source_type=api.SourceType.DLS_MARC,
            records=_records(14, prefix="L"),
        )
        with pytest.raises(api.ActivationConfirmationRequired) as size_error:
            sync.activate_staged(larger.id)
        assert size_error.value.reason == "SNAPSHOT_SIZE_CHANGE"
        assert size_error.value.previous_count == 10
        assert size_error.value.new_count == 14
        assert sync.repository.active_version(SCHOOL_ID).id == first.id

        activated = sync.activate_staged(larger.id, confirm_anomaly=True)

    assert activated.id == larger.id
    assert activated.status == "ACTIVE"


def test_snapshot_validation_is_atomic_and_active_versions_are_immutable(
    tmp_path,
) -> None:
    """A duplicate stable ID or later holding mutation must not corrupt an active version."""
    api = _api()
    with _database(tmp_path) as connection:
        sync = api.CatalogSyncService(connection)
        active = sync.import_full_snapshot(
            school_id=SCHOOL_ID,
            source_type=api.SourceType.DLS_MARC,
            records=_records(2),
            confirm_anomaly=True,
        )
        before_versions = connection.execute(
            "SELECT COUNT(*) FROM catalog_versions"
        ).fetchone()[0]
        duplicate = api.CatalogRecord(
            source_item_id="DUPLICATE",
            isbn="9780306406157",
            title="중복",
            authors=("저자",),
        )

        with pytest.raises(api.CatalogValidationError, match="source item"):
            sync.stage_full_snapshot(
                school_id=SCHOOL_ID,
                source_type=api.SourceType.DLS_MARC,
                records=(duplicate, duplicate),
            )

        assert sync.repository.active_version(SCHOOL_ID).id == active.id
        assert (
            connection.execute("SELECT COUNT(*) FROM catalog_versions").fetchone()[0]
            == before_versions
        )
        holding_id = connection.execute(
            "SELECT id FROM holdings WHERE catalog_version_id = ? LIMIT 1", (active.id,)
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE holdings SET original_title = '변조' WHERE id = ?",
                (holding_id,),
            )


def test_source_policy_allows_marc_full_delta_and_excel_full_only(tmp_path) -> None:
    """Adding Excel delta or an unapproved API source would violate the DLS policy."""
    api = _api()
    with _database(tmp_path) as connection:
        sync = api.CatalogSyncService(connection)
        sync.import_full_snapshot(
            school_id=SCHOOL_ID,
            source_type=api.SourceType.DLS_EXCEL,
            records=_records(1),
            confirm_anomaly=True,
        )
        with pytest.raises(api.SourcePolicyError, match="delta"):
            sync.apply_delta(
                school_id=SCHOOL_ID,
                source_type=api.SourceType.DLS_EXCEL,
                registration_file=_delta_file(api, "a"),
                update_file=_delta_file(api, "b"),
                through_date=date(2026, 8, 28),
            )
        with pytest.raises(api.SourcePolicyError, match="approved"):
            sync.stage_full_snapshot(
                school_id=SCHOOL_ID,
                source_type=api.SourceType.DLS_API,
                records=_records(1),
            )


def test_activating_a_new_source_leaves_one_active_source_type_per_school(
    tmp_path,
) -> None:
    """Failing to supersede the old source could mix MARC and Excel evidence."""
    api = _api()
    with _database(tmp_path) as connection:
        sync = api.CatalogSyncService(connection)
        marc = sync.import_full_snapshot(
            school_id=SCHOOL_ID,
            source_type=api.SourceType.DLS_MARC,
            records=_records(2),
            confirm_anomaly=True,
        )
        excel = sync.import_full_snapshot(
            school_id=SCHOOL_ID,
            source_type=api.SourceType.DLS_EXCEL,
            records=_records(2, prefix="E"),
            confirm_anomaly=True,
        )
        active_rows = connection.execute(
            "SELECT id, source_type FROM catalog_versions WHERE school_id = ? AND status = 'ACTIVE'",
            (SCHOOL_ID,),
        ).fetchall()

    assert [(row["id"], row["source_type"]) for row in active_rows] == [
        (excel.id, api.SourceType.DLS_EXCEL.value)
    ]
    assert marc.id != excel.id


def test_marc_delta_window_is_seoul_local_inclusive_with_two_day_overlap(
    tmp_path,
) -> None:
    """Using UTC dates or a one-day overlap can miss late DLS changes."""
    api = _api()
    activated_at = datetime(2026, 8, 27, 16, 30, tzinfo=UTC)  # 2026-08-28 in Seoul
    with _database(tmp_path) as connection:
        sync = api.CatalogSyncService(connection)
        sync.import_full_snapshot(
            school_id=SCHOOL_ID,
            source_type=api.SourceType.DLS_MARC,
            records=_records(1),
            confirm_anomaly=True,
            activated_at=activated_at,
            as_of_date=date(2026, 8, 28),
        )

        window = sync.delta_window(SCHOOL_ID, through_date=date(2026, 9, 2))

    assert window.start == date(2026, 8, 26)
    assert window.end == date(2026, 9, 2)
    assert window.start_inclusive is True
    assert window.end_inclusive is True


def test_marc_delta_is_idempotent_update_wins_and_preserves_stable_identity(
    tmp_path,
) -> None:
    """Replaying overlap files or applying registration after update must not duplicate/revert."""
    api = _api()
    with _database(tmp_path) as connection:
        sync = api.CatalogSyncService(connection)
        sync.import_full_snapshot(
            school_id=SCHOOL_ID,
            source_type=api.SourceType.DLS_MARC,
            records=(
                api.CatalogRecord(
                    source_item_id="BIB-001",
                    isbn="9780306406157",
                    title="이전 제목",
                    authors=("저자",),
                ),
            ),
            confirm_anomaly=True,
            as_of_date=date(2026, 8, 20),
        )
        registration = _delta_file(
            api,
            "a",
            (
                api.CatalogRecord(
                    source_item_id="BIB-001",
                    isbn="9780306406157",
                    title="등록 파일 제목",
                    authors=("저자",),
                ),
                api.CatalogRecord(
                    source_item_id="BIB-002",
                    isbn="9781861972712",
                    title="새 책",
                    authors=("새 저자",),
                ),
            ),
        )
        update = _delta_file(
            api,
            "b",
            (
                api.CatalogRecord(
                    source_item_id="BIB-001",
                    isbn="9780306406157",
                    title="갱신 파일 제목",
                    authors=("저자",),
                ),
            ),
        )

        first = sync.apply_delta(
            school_id=SCHOOL_ID,
            source_type=api.SourceType.DLS_MARC,
            registration_file=registration,
            update_file=update,
            through_date=date(2026, 8, 28),
        )
        stable_before = connection.execute(
            "SELECT stable_id, original_title FROM holdings WHERE catalog_version_id = ? AND source_item_id = 'BIB-001'",
            (first.catalog_version_id,),
        ).fetchone()
        rerun = sync.apply_delta(
            school_id=SCHOOL_ID,
            source_type=api.SourceType.DLS_MARC,
            registration_file=registration,
            update_file=update,
            through_date=date(2026, 8, 28),
        )
        later = sync.apply_delta(
            school_id=SCHOOL_ID,
            source_type=api.SourceType.DLS_MARC,
            registration_file=_delta_file(api, "c"),
            update_file=_delta_file(api, "d"),
            through_date=date(2026, 8, 29),
            now=datetime(2026, 8, 29, 3, 0, tzinfo=UTC),
        )
        old_overlap_rerun = sync.apply_delta(
            school_id=SCHOOL_ID,
            source_type=api.SourceType.DLS_MARC,
            registration_file=registration,
            update_file=update,
            through_date=date(2026, 8, 28),
        )
        active_count = connection.execute(
            "SELECT COUNT(*) FROM holdings WHERE catalog_version_id = ?",
            (first.catalog_version_id,),
        ).fetchone()[0]

    assert first.applied is True
    assert stable_before["original_title"] == "갱신 파일 제목"
    assert stable_before["stable_id"]
    assert active_count == 2
    assert rerun.idempotent is True
    assert rerun.catalog_version_id == first.catalog_version_id
    assert later.watermark_local_date == date(2026, 8, 29)
    assert old_overlap_rerun.idempotent is True
    assert old_overlap_rerun.watermark_local_date == date(2026, 8, 29)


def test_delta_partial_failure_keeps_active_version_and_watermark(tmp_path) -> None:
    """Advancing after only one DLS file succeeds can create a permanent gap."""
    api = _api()
    with _database(tmp_path) as connection:
        sync = api.CatalogSyncService(connection)
        active = sync.import_full_snapshot(
            school_id=SCHOOL_ID,
            source_type=api.SourceType.DLS_MARC,
            records=_records(1),
            confirm_anomaly=True,
            as_of_date=date(2026, 8, 20),
        )
        failed_update = _delta_file(api, "d", status=api.ParserStatus.FAILED)

        result = sync.apply_delta(
            school_id=SCHOOL_ID,
            source_type=api.SourceType.DLS_MARC,
            registration_file=_delta_file(api, "c", _records(1, prefix="NEW")),
            update_file=failed_update,
            through_date=date(2026, 8, 28),
        )
        state = sync.repository.source_state(SCHOOL_ID)

    assert result.applied is False
    assert result.status == "PARTIAL_FAILURE"
    assert sync.repository.active_version(SCHOOL_ID).id == active.id
    assert state.watermark_local_date == date(2026, 8, 20)


def test_delta_requires_a_full_snapshot_at_ninety_days(tmp_path) -> None:
    """Allowing indefinite deltas would retain deletions that deltas never report."""
    api = _api()
    current = datetime(2026, 8, 28, 0, 0, tzinfo=UTC)
    old = current - timedelta(days=90)
    with _database(tmp_path) as connection:
        sync = api.CatalogSyncService(connection)
        sync.import_full_snapshot(
            school_id=SCHOOL_ID,
            source_type=api.SourceType.DLS_MARC,
            records=_records(1),
            confirm_anomaly=True,
            activated_at=old,
            as_of_date=date(2026, 5, 30),
        )

        with pytest.raises(api.FullSnapshotRequired):
            sync.apply_delta(
                school_id=SCHOOL_ID,
                source_type=api.SourceType.DLS_MARC,
                registration_file=_delta_file(api, "e"),
                update_file=_delta_file(api, "f"),
                through_date=date(2026, 8, 28),
                now=current,
            )


@pytest.mark.parametrize(
    ("new_count", "needs_confirmation"),
    [(0, True), (7, False), (13, False), (6, True), (14, True)],
)
def test_snapshot_confirmation_boundaries_are_exact(
    tmp_path, new_count: int, needs_confirmation: bool
) -> None:
    """Changing the inclusive 30% boundary would block safe snapshots or admit anomalies."""
    api = _api()
    with _database(tmp_path) as connection:
        sync = api.CatalogSyncService(connection)
        original = sync.import_full_snapshot(
            school_id=SCHOOL_ID,
            source_type=api.SourceType.DLS_MARC,
            records=_records(10),
            confirm_anomaly=True,
        )
        staged = sync.stage_full_snapshot(
            school_id=SCHOOL_ID,
            source_type=api.SourceType.DLS_MARC,
            records=_records(new_count, prefix="BOUNDARY"),
        )

        if needs_confirmation:
            with pytest.raises(api.ActivationConfirmationRequired):
                sync.activate_staged(staged.id)
            assert sync.repository.active_version(SCHOOL_ID).id == original.id
        else:
            activated = sync.activate_staged(staged.id)
            assert activated.id == staged.id


def test_activation_recounts_and_rejects_stale_staging_metadata(tmp_path) -> None:
    """Trusting a mutable item_count would bypass the activation size gate."""
    api = _api()
    with _database(tmp_path) as connection:
        sync = api.CatalogSyncService(connection)
        original = sync.import_full_snapshot(
            school_id=SCHOOL_ID,
            source_type=api.SourceType.DLS_MARC,
            records=_records(10),
            confirm_anomaly=True,
        )
        staged = sync.stage_full_snapshot(
            school_id=SCHOOL_ID,
            source_type=api.SourceType.DLS_MARC,
            records=_records(14, prefix="STALE"),
        )
        connection.execute(
            "UPDATE catalog_versions SET item_count = 10 WHERE id = ?", (staged.id,)
        )

        with pytest.raises(api.CatalogValidationError, match="count"):
            sync.activate_staged(staged.id)

        assert sync.repository.active_version(SCHOOL_ID).id == original.id


def test_activation_rejects_a_snapshot_staged_against_an_old_active_version(
    tmp_path,
) -> None:
    """A stale staging import must not supersede a newer catalog activation."""
    api = _api()
    with _database(tmp_path) as connection:
        sync = api.CatalogSyncService(connection)
        sync.import_full_snapshot(
            school_id=SCHOOL_ID,
            source_type=api.SourceType.DLS_MARC,
            records=_records(10),
            confirm_anomaly=True,
        )
        stale = sync.stage_full_snapshot(
            school_id=SCHOOL_ID,
            source_type=api.SourceType.DLS_MARC,
            records=_records(10, prefix="STALE"),
        )
        newer = sync.stage_full_snapshot(
            school_id=SCHOOL_ID,
            source_type=api.SourceType.DLS_MARC,
            records=_records(10, prefix="NEWER"),
        )
        sync.activate_staged(newer.id)

        with pytest.raises(api.CatalogValidationError, match="changed"):
            sync.activate_staged(stale.id)

        assert sync.repository.active_version(SCHOOL_ID).id == newer.id


def test_active_and_superseded_catalog_rows_are_sealed_against_inserts(
    tmp_path,
) -> None:
    """Insert-only mutation would evade the original active-row update/delete triggers."""
    api = _api()
    with _database(tmp_path) as connection:
        sync = api.CatalogSyncService(connection)
        first = sync.import_full_snapshot(
            school_id=SCHOOL_ID,
            source_type=api.SourceType.DLS_MARC,
            records=_records(1),
            confirm_anomaly=True,
        )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                """
                INSERT INTO holdings
                SELECT ?, ?, catalog_version_id, school_id, ?, source_row_id,
                       isbn_input, isbn13, original_title, original_subtitle,
                       original_authors_json, original_publisher, original_volume,
                       original_edition, original_series, publication_date, price,
                       pages, kdc, registration_number, call_number, location,
                       holding_status, raw_json, created_at
                FROM holdings WHERE catalog_version_id = ? LIMIT 1
                """,
                (str(uuid.uuid4()), str(uuid.uuid4()), "LATE", first.id),
            )

        second = sync.import_full_snapshot(
            school_id=SCHOOL_ID,
            source_type=api.SourceType.DLS_MARC,
            records=_records(1, prefix="SECOND"),
            confirm_anomaly=True,
        )
        assert second.id != first.id
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                """
                INSERT INTO holdings
                SELECT ?, ?, catalog_version_id, school_id, ?, source_row_id,
                       isbn_input, isbn13, original_title, original_subtitle,
                       original_authors_json, original_publisher, original_volume,
                       original_edition, original_series, publication_date, price,
                       pages, kdc, registration_number, call_number, location,
                       holding_status, raw_json, created_at
                FROM holdings WHERE catalog_version_id = ? LIMIT 1
                """,
                (
                    str(uuid.uuid4()),
                    str(uuid.uuid4()),
                    "SUPERSEDED-LATE",
                    first.id,
                ),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                """
                UPDATE normalized_works SET title_key = 'mutated'
                WHERE catalog_version_id = ?
                """,
                (first.id,),
            )


def test_fts_projection_is_read_only_and_staging_updates_refresh_it(tmp_path) -> None:
    """Every normal reopened app connection must reject direct FTS mutations."""
    api = _api()
    database_path = tmp_path / "catalog.sqlite3"
    with _database(tmp_path) as connection:
        sync = api.CatalogSyncService(connection)
        staged = sync.stage_full_snapshot(
            school_id=SCHOOL_ID,
            source_type=api.SourceType.DLS_MARC,
            records=_records(1),
        )
        sync.repository.put_holding(
            version_id=staged.id,
            school_id=SCHOOL_ID,
            source_type=api.SourceType.DLS_MARC,
            record=api.CatalogRecord(
                source_item_id="H-000", title="트리거로 갱신된 제목", authors=("저자",)
            ),
        )
        indexed = connection.execute(
            "SELECT search_text FROM holding_search_fts WHERE catalog_version_id = ?",
            (staged.id,),
        ).fetchone()[0]
        connection.commit()

    mutation_sql = (
        "DELETE FROM holding_search_fts_index",
        "DELETE FROM holding_search_fts_index_data",
        "UPDATE holding_search_fts_index_data SET block = block",
        """
        WITH protected_rows AS (
            SELECT id FROM holding_search_fts_index_data WHERE 0
        )
        DELETE FROM holding_search_fts_index_data
        WHERE id IN (SELECT id FROM protected_rows)
        """,
        """
        INSERT INTO holding_search_fts_index_data(id, block)
        SELECT max(id) + 1, block FROM holding_search_fts_index_data
        """,
    )
    for statement in mutation_sql:
        with (
            connect(database_path) as reopened,
            pytest.raises(sqlite3.DatabaseError, match="authorized|protected"),
        ):
            reopened.execute(statement)
    with connect(database_path) as reopened:
        with pytest.raises(sqlite3.DatabaseError, match="authorized|protected"):
            reopened.executescript(
                "SELECT 1; DELETE FROM holding_search_fts_index_data;"
            )
        with pytest.raises(sqlite3.DatabaseError, match="authorized|protected"):
            reopened.cursor().execute("DELETE FROM holding_search_fts_index_data")

    assert "트리" in indexed


def test_failed_migration_restores_fts_protection_on_the_same_connection(
    tmp_path,
) -> None:
    """A failed trusted maintenance scope must not leave its connection writable."""
    database_path = tmp_path / "catalog.sqlite3"
    with _database(tmp_path) as connection:
        connection.commit()

    changed_migrations = tmp_path / "changed-migrations"
    changed_migrations.mkdir()
    (changed_migrations / "0004b_catalog_hardening.sql").write_text(
        "SELECT 1;\n", encoding="utf-8"
    )
    with connect(database_path) as reopened:
        with pytest.raises(MigrationChecksumMismatch, match="0004b"):
            apply_migrations(reopened, changed_migrations)
        with pytest.raises(sqlite3.DatabaseError, match="authorized|protected"):
            sqlite3.Connection.execute(reopened, "DELETE FROM holding_search_fts_index")


def test_activation_detects_corrupted_fts_postings_after_reconnect(tmp_path) -> None:
    """External-content row COUNT can look correct while postings are missing."""
    api = _api()
    database_path = tmp_path / "catalog.sqlite3"
    with _database(tmp_path) as connection:
        staged = api.CatalogSyncService(connection).stage_full_snapshot(
            school_id=SCHOOL_ID,
            source_type=api.SourceType.DLS_MARC,
            records=_records(2),
        )
        connection.commit()

    raw = sqlite3.connect(database_path)
    raw.execute("DELETE FROM holding_search_fts_index")
    raw.commit()
    raw.close()

    with connect(database_path) as reopened:
        with pytest.raises(api.CatalogValidationError, match="FTS|search"):
            api.CatalogSyncService(reopened).activate_staged(
                staged.id, confirm_anomaly=True
            )
        status = reopened.execute(
            "SELECT status FROM catalog_versions WHERE id = ?", (staged.id,)
        ).fetchone()[0]

    assert status == "STAGING"


def test_parent_identity_links_cannot_be_reparented_after_insert(tmp_path) -> None:
    """Moving a staging holding or tenant parent can bypass child-side scope triggers."""
    from suseoro.jobs.repository import JobRepository

    api = _api()
    with _database(tmp_path) as connection:
        connection.execute(
            "INSERT INTO schools (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (OTHER_SCHOOL_ID, "다른 학교", NOW, NOW),
        )
        sync = api.CatalogSyncService(connection)
        first = sync.stage_full_snapshot(
            school_id=SCHOOL_ID,
            source_type=api.SourceType.DLS_MARC,
            records=_records(1, prefix="FIRST"),
        )
        second = sync.stage_full_snapshot(
            school_id=SCHOOL_ID,
            source_type=api.SourceType.DLS_MARC,
            records=_records(1, prefix="SECOND"),
        )
        holding_id = connection.execute(
            "SELECT id FROM holdings WHERE catalog_version_id = ?", (first.id,)
        ).fetchone()[0]
        source_document_id, _ = _marc_document(
            connection,
            role="CATALOG_DELTA_REGISTRATION",
            sha_digit="f",
        )
        source_file_id = connection.execute(
            "SELECT source_file_id FROM source_documents WHERE id = ?",
            (source_document_id,),
        ).fetchone()[0]
        workspace_id = str(uuid.uuid4())
        connection.execute(
            """
            INSERT INTO acquisition_workspaces (
                id, school_id, name, status, created_at, updated_at
            ) VALUES (?, ?, '이동 금지', 'DRAFT', ?, ?)
            """,
            (workspace_id, SCHOOL_ID, NOW, NOW),
        )
        job = JobRepository(connection).create(
            school_id=SCHOOL_ID,
            workspace_id=workspace_id,
            job_type="COMPARE",
            payload={"source_document_ids": []},
        )

        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE holdings SET catalog_version_id = ? WHERE id = ?",
                (second.id, holding_id),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE catalog_versions SET school_id = ? WHERE id = ?",
                (OTHER_SCHOOL_ID, first.id),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE source_files SET sha256 = ? WHERE id = ?",
                ("0" * 64, source_file_id),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE source_documents SET role = 'CATALOG_DELTA_UPDATE' WHERE id = ?",
                (source_document_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE source_documents SET status = 'FAILED' WHERE id = ?",
                (source_document_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                """
                UPDATE source_documents SET requested_start_local_date = '2026-08-19'
                WHERE id = ?
                """,
                (source_document_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE acquisition_workspaces SET school_id = ? WHERE id = ?",
                (OTHER_SCHOOL_ID, workspace_id),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE durable_jobs SET job_type = 'IMPORT' WHERE id = ?",
                (job.id,),
            )

        connection.execute(
            "UPDATE acquisition_workspaces SET status = 'REVIEWING' WHERE id = ?",
            (workspace_id,),
        )
        connection.execute(
            "UPDATE durable_jobs SET stage = 'VALIDATING' WHERE id = ?", (job.id,)
        )

    assert first.status == "STAGING"


def test_full_snapshot_requires_a_valid_bound_source_document(tmp_path) -> None:
    """A production catalog activation without immutable source provenance is unauditable."""
    api = _api()
    with _database(tmp_path) as connection:
        sync = api.ProductionCatalogSyncService(connection)
        with pytest.raises(api.CatalogValidationError, match="source document"):
            sync.import_full_snapshot(
                school_id=SCHOOL_ID,
                source_type=api.SourceType.DLS_MARC,
                records=_records(1),
                confirm_anomaly=True,
            )

        document_id, row_ids = _marc_document(
            connection,
            role="CATALOG_FULL",
            sha_digit="1",
            rows=({"fields": {"title": "원문"}},),
        )
        bound_record = api.CatalogRecord(
            source_item_id="MARC-001",
            source_row_id=row_ids[0],
            title="원문",
        )
        active = sync.import_full_snapshot(
            school_id=SCHOOL_ID,
            source_type=api.SourceType.DLS_MARC,
            records=(bound_record,),
            source_document_id=document_id,
            confirm_anomaly=True,
        )

    assert active.source_type == api.SourceType.DLS_MARC


def test_marc_delta_binds_distinct_roles_and_persists_window_and_row_accounting(
    tmp_path,
) -> None:
    """Caller-provided hashes without role-bound documents cannot prove delta lineage."""
    api = _api()
    with _database(tmp_path) as connection:
        full_doc, full_rows = _marc_document(
            connection,
            role="CATALOG_FULL",
            sha_digit="2",
            rows=({"fields": {"title": "기준"}},),
        )
        sync = api.ProductionCatalogSyncService(connection)
        sync.import_full_snapshot(
            school_id=SCHOOL_ID,
            source_type=api.SourceType.DLS_MARC,
            records=(
                api.CatalogRecord(
                    source_item_id="BASE-001",
                    source_row_id=full_rows[0],
                    title="기준",
                ),
            ),
            source_document_id=full_doc,
            confirm_anomaly=True,
            as_of_date=date(2026, 8, 20),
        )
        registration_doc, registration_rows = _marc_document(
            connection,
            role="CATALOG_DELTA_REGISTRATION",
            sha_digit="3",
            rows=(
                {"fields": {"title": "신규"}},
                {
                    "status": "ROW_ERROR",
                    "error_code": "MARC_RECORD_DAMAGED",
                    "error_message": "손상 레코드",
                },
            ),
            status="ROW_ERROR",
        )
        update_doc, update_rows = _marc_document(
            connection,
            role="CATALOG_DELTA_UPDATE",
            sha_digit="4",
            rows=({"fields": {"title": "갱신"}},),
        )
        registration = api.DeltaFile(
            source_document_id=registration_doc,
            source_file_sha256="3" * 64,
            parser_version="marc-v1",
            status=api.ParserStatus.SUCCESS,
            records=(
                api.CatalogRecord(
                    source_item_id="NEW-001",
                    source_row_id=registration_rows[0],
                    title="신규",
                ),
            ),
            window=api.DeltaWindow(date(2026, 8, 18), date(2026, 8, 28)),
        )
        update = api.DeltaFile(
            source_document_id=update_doc,
            source_file_sha256="4" * 64,
            parser_version="marc-v1",
            status=api.ParserStatus.SUCCESS,
            records=(
                api.CatalogRecord(
                    source_item_id="BASE-001",
                    source_row_id=update_rows[0],
                    title="갱신",
                ),
            ),
            window=api.DeltaWindow(date(2026, 8, 18), date(2026, 8, 28)),
        )

        result = sync.apply_delta(
            school_id=SCHOOL_ID,
            source_type=api.SourceType.DLS_MARC,
            registration_file=registration,
            update_file=update,
            through_date=date(2026, 8, 28),
        )
        batch = connection.execute(
            "SELECT * FROM catalog_delta_batches WHERE catalog_version_id = ?",
            (result.catalog_version_id,),
        ).fetchone()
        row_results = connection.execute(
            """
            SELECT role, source_row_id, outcome, reason
            FROM catalog_delta_row_results
            WHERE batch_id = ? ORDER BY role, source_row_id
            """,
            (batch["id"],),
        ).fetchall()

    assert batch["registration_source_document_id"] == registration_doc
    assert batch["update_source_document_id"] == update_doc
    assert batch["requested_start_local_date"] == "2026-08-18"
    assert batch["through_local_date"] == "2026-08-28"
    assert len(row_results) == 3
    assert {row["outcome"] for row in row_results} == {"APPLIED", "ROW_ERROR"}
    assert any(row["reason"] == "MARC_RECORD_DAMAGED" for row in row_results)


def test_bound_delta_documents_and_contracts_must_agree_with_inclusive_window(
    tmp_path,
) -> None:
    """A role-bound file from another export window must not advance this watermark."""
    api = _api()
    window = api.DeltaWindow(date(2026, 8, 18), date(2026, 8, 28))
    wrong_window = api.DeltaWindow(date(2026, 8, 19), date(2026, 8, 28))
    with _database(tmp_path) as connection:
        full_doc, full_rows = _marc_document(
            connection,
            role="CATALOG_FULL",
            sha_digit="c",
            rows=({"fields": {"title": "기준"}},),
        )
        sync = api.ProductionCatalogSyncService(connection)
        sync.import_full_snapshot(
            school_id=SCHOOL_ID,
            source_type=api.SourceType.DLS_MARC,
            records=(
                api.CatalogRecord(
                    source_item_id="BASE-WINDOW",
                    source_row_id=full_rows[0],
                    title="기준",
                ),
            ),
            source_document_id=full_doc,
            confirm_anomaly=True,
            as_of_date=date(2026, 8, 20),
        )
        registration_doc, registration_rows = _marc_document(
            connection,
            role="CATALOG_DELTA_REGISTRATION",
            sha_digit="d",
            rows=({"fields": {"title": "등록"}},),
        )
        update_doc, update_rows = _marc_document(
            connection,
            role="CATALOG_DELTA_UPDATE",
            sha_digit="e",
            rows=({"fields": {"title": "갱신"}},),
        )
        for document_id in (registration_doc, update_doc):
            _bind_delta_window(
                connection,
                document_id,
                start=window.start,
                end=window.end,
            )
        registration = api.DeltaFile(
            source_document_id=registration_doc,
            source_file_sha256="d" * 64,
            parser_version="marc-v1",
            status=api.ParserStatus.SUCCESS,
            records=(
                api.CatalogRecord(
                    source_item_id="NEW-WINDOW",
                    source_row_id=registration_rows[0],
                    title="등록",
                ),
            ),
            window=window,
        )
        wrong_update = api.DeltaFile(
            source_document_id=update_doc,
            source_file_sha256="e" * 64,
            parser_version="marc-v1",
            status=api.ParserStatus.SUCCESS,
            records=(
                api.CatalogRecord(
                    source_item_id="BASE-WINDOW",
                    source_row_id=update_rows[0],
                    title="갱신",
                ),
            ),
            window=wrong_window,
        )

        with pytest.raises(api.CatalogValidationError, match="window"):
            sync.apply_delta(
                school_id=SCHOOL_ID,
                source_type=api.SourceType.DLS_MARC,
                registration_file=registration,
                update_file=wrong_update,
                through_date=window.end,
            )

        result = sync.apply_delta(
            school_id=SCHOOL_ID,
            source_type=api.SourceType.DLS_MARC,
            registration_file=registration,
            update_file=api.DeltaFile(
                source_document_id=update_doc,
                source_file_sha256="e" * 64,
                parser_version="marc-v1",
                status=api.ParserStatus.SUCCESS,
                records=wrong_update.records,
                window=window,
            ),
            through_date=window.end,
        )
        batch = connection.execute(
            "SELECT * FROM catalog_delta_batches WHERE catalog_version_id = ?",
            (result.catalog_version_id,),
        ).fetchone()

    assert batch["requested_start_local_date"] == "2026-08-18"
    assert batch["through_local_date"] == "2026-08-28"


def test_delta_rejects_same_file_for_both_roles_and_arbitrary_hashes(tmp_path) -> None:
    """One physical file or a forged digest must never satisfy both delta roles."""
    api = _api()
    with _database(tmp_path) as connection:
        full_doc, full_rows = _marc_document(
            connection,
            role="CATALOG_FULL",
            sha_digit="5",
            rows=({"fields": {"title": "기준"}},),
        )
        sync = api.ProductionCatalogSyncService(connection)
        sync.import_full_snapshot(
            school_id=SCHOOL_ID,
            source_type=api.SourceType.DLS_MARC,
            records=(
                api.CatalogRecord(
                    source_item_id="BASE",
                    source_row_id=full_rows[0],
                    title="기준",
                ),
            ),
            source_document_id=full_doc,
            confirm_anomaly=True,
            as_of_date=date(2026, 8, 20),
        )
        registration_doc, _ = _marc_document(
            connection,
            role="CATALOG_DELTA_REGISTRATION",
            sha_digit="6",
        )
        update_doc, _ = _marc_document(
            connection,
            role="CATALOG_DELTA_UPDATE",
            sha_digit="8",
        )
        same = api.DeltaFile(
            source_document_id=registration_doc,
            source_file_sha256="6" * 64,
            parser_version="marc-v1",
            status=api.ParserStatus.SUCCESS,
            window=api.DeltaWindow(date(2026, 8, 18), date(2026, 8, 28)),
        )
        forged = api.DeltaFile(
            source_document_id=registration_doc,
            source_file_sha256="7" * 64,
            parser_version="marc-v1",
            status=api.ParserStatus.SUCCESS,
            window=api.DeltaWindow(date(2026, 8, 18), date(2026, 8, 28)),
        )
        valid_update = api.DeltaFile(
            source_document_id=update_doc,
            source_file_sha256="8" * 64,
            parser_version="marc-v1",
            status=api.ParserStatus.SUCCESS,
            window=api.DeltaWindow(date(2026, 8, 18), date(2026, 8, 28)),
        )

        with pytest.raises(api.CatalogValidationError, match="distinct"):
            sync.apply_delta(
                school_id=SCHOOL_ID,
                source_type=api.SourceType.DLS_MARC,
                registration_file=same,
                update_file=same,
                through_date=date(2026, 8, 28),
            )
        with pytest.raises(api.CatalogValidationError, match="hash"):
            sync.apply_delta(
                school_id=SCHOOL_ID,
                source_type=api.SourceType.DLS_MARC,
                registration_file=forged,
                update_file=valid_update,
                through_date=date(2026, 8, 28),
            )


def test_delta_rejects_a_future_asia_seoul_watermark(tmp_path) -> None:
    """A caller-controlled future date would suppress every later incremental export."""
    api = _api()
    now = datetime(2026, 8, 28, 3, 0, tzinfo=UTC)
    with _database(tmp_path) as connection:
        sync = api.CatalogSyncService(connection)
        sync.import_full_snapshot(
            school_id=SCHOOL_ID,
            source_type=api.SourceType.DLS_MARC,
            records=_records(1),
            confirm_anomaly=True,
            as_of_date=date(2026, 8, 20),
            activated_at=now,
        )

        with pytest.raises(api.CatalogValidationError, match="future|local date"):
            sync.apply_delta(
                school_id=SCHOOL_ID,
                source_type=api.SourceType.DLS_MARC,
                registration_file=_delta_file(api, "a"),
                update_file=_delta_file(api, "b"),
                through_date=date(2099, 1, 1),
                now=now,
            )


def test_delta_revalidates_both_provenance_bindings_inside_write_transaction(
    tmp_path,
) -> None:
    """Pre-transaction validation leaves SHA, status, and window open to a race."""
    api = _api()
    now = datetime(2026, 8, 28, 3, 0, tzinfo=UTC)
    with _database(tmp_path) as connection:
        sync = api.CatalogSyncService(connection)
        sync.import_full_snapshot(
            school_id=SCHOOL_ID,
            source_type=api.SourceType.DLS_MARC,
            records=_records(1),
            confirm_anomaly=True,
            as_of_date=date(2026, 8, 20),
            activated_at=now,
        )
        connection.commit()
        observed_transactions: list[bool] = []
        original_validate = sync._validate_delta_binding

        def observe_transaction(**kwargs):
            observed_transactions.append(connection.in_transaction)
            return original_validate(**kwargs)

        sync._validate_delta_binding = observe_transaction
        sync.apply_delta(
            school_id=SCHOOL_ID,
            source_type=api.SourceType.DLS_MARC,
            registration_file=_delta_file(api, "c"),
            update_file=_delta_file(api, "d"),
            through_date=date(2026, 8, 28),
            now=now,
        )

    assert observed_transactions == [True, True]


def test_catalog_roles_flow_through_real_marc_parser_and_parser_cache(tmp_path) -> None:
    """DB-only role expansion is unusable when typed parser/cache contracts reject it."""
    from suseoro.ingestion.contracts import DocumentRole
    from suseoro.ingestion.parsers.marc import parse_marc
    from suseoro.ingestion.templates import ParserCache

    roles = (
        DocumentRole.CATALOG_FULL,
        DocumentRole.CATALOG_DELTA_REGISTRATION,
        DocumentRole.CATALOG_DELTA_UPDATE,
    )
    with _database(tmp_path) as connection:
        for index, role in enumerate(roles, start=1):
            target = tmp_path / f"{role.value}.mrc"
            target.write_bytes(b"")
            digest = f"{index:x}" * 64
            parsed = parse_marc(target, role=role, sha256=digest)
            connection.execute(
                """
                INSERT INTO source_files (
                    id, sha256, size_bytes, storage_path, detected_format, created_at
                ) VALUES (?, ?, 0, ?, 'MARC', ?)
                """,
                (str(uuid.uuid4()), digest, str(target), NOW),
            )
            first = ParserCache(connection).get_or_parse(
                sha256=digest,
                parser_version=parsed.parser_version,
                role=role,
                parse=lambda parsed=parsed: {
                    "role": parsed.role.value,
                    "activation_allowed": parsed.activation_allowed,
                },
            )
            cached = ParserCache(connection).get_or_parse(
                sha256=digest,
                parser_version=parsed.parser_version,
                role=role,
                parse=lambda: {"unexpected": True},
            )

            assert first.result["role"] == role.value
            assert cached.cached is True
            assert cached.result == first.result


def test_forward_migration_expands_formats_without_losing_b5fc8_rows(tmp_path) -> None:
    """Rebuilding ingestion checks must preserve prior rows and foreign keys on upgrade."""
    current_dir = Path(migration_module.__file__).with_name("migrations")
    old_dir = tmp_path / "b5fc8-migrations"
    old_dir.mkdir()
    for source in current_dir.glob("*.sql"):
        if source.stem not in (
            "0004a_catalog_integrity",
            "0004b_catalog_hardening",
        ):
            shutil.copy2(source, old_dir / source.name)
    connection = connect(tmp_path / "upgrade.sqlite3")
    apply_migrations(connection, old_dir)
    connection.execute(
        "INSERT INTO schools (id, name, created_at, updated_at) VALUES (?, '업그레이드', ?, ?)",
        (SCHOOL_ID, NOW, NOW),
    )
    old_file = str(uuid.uuid4())
    old_document = str(uuid.uuid4())
    connection.execute(
        """
        INSERT INTO source_files (
            id, sha256, size_bytes, storage_path, detected_format, created_at
        ) VALUES (?, ?, 1, 'old.xlsx', 'XLSX', ?)
        """,
        (old_file, "9" * 64, NOW),
    )
    connection.execute(
        """
        INSERT INTO source_documents (
            id, source_file_id, school_id, role, parser_version, status,
            detected_format, created_at, completed_at
        ) VALUES (?, ?, ?, 'INVENTORY', 'tabular-v1', 'SUCCESS', 'XLSX', ?, ?)
        """,
        (old_document, old_file, SCHOOL_ID, NOW, NOW),
    )
    connection.commit()

    apply_migrations(connection, current_dir)
    for index, detected_format in enumerate(("DOCX", "HWPX", "PDF", "HWP", "MARC")):
        file_id = str(uuid.uuid4())
        connection.execute(
            """
            INSERT INTO source_files (
                id, sha256, size_bytes, storage_path, detected_format, created_at
            ) VALUES (?, ?, 1, ?, ?, ?)
            """,
            (
                file_id,
                f"{index + 1:x}" * 64,
                f"new.{detected_format.lower()}",
                detected_format,
                NOW,
            ),
        )
    preserved = connection.execute(
        """
        SELECT sf.detected_format AS file_format, sd.detected_format AS document_format
        FROM source_documents sd JOIN source_files sf ON sf.id = sd.source_file_id
        WHERE sd.id = ?
        """,
        (old_document,),
    ).fetchone()
    foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
    latest = connection.execute(
        "SELECT migration_id FROM schema_migrations ORDER BY migration_id DESC LIMIT 1"
    ).fetchone()[0]
    connection.close()

    assert (preserved["file_format"], preserved["document_format"]) == (
        "XLSX",
        "XLSX",
    )
    assert foreign_keys == []
    assert latest == "0004b_catalog_hardening"


def test_forward_migration_upgrades_populated_0731aa1_schema_without_data_loss(
    tmp_path,
) -> None:
    """0004b must upgrade the committed 0731aa1 schema rather than assuming fresh DBs."""
    current_dir = Path(migration_module.__file__).with_name("migrations")
    old_dir = tmp_path / "0731aa1-migrations"
    old_dir.mkdir()
    for source in current_dir.glob("*.sql"):
        if source.stem != "0004b_catalog_hardening":
            shutil.copy2(source, old_dir / source.name)
    connection = connect(tmp_path / "0731aa1-upgrade.sqlite3")
    apply_migrations(connection, old_dir)
    connection.execute(
        "INSERT INTO schools (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (SCHOOL_ID, "0731 업그레이드", NOW, NOW),
    )
    sync = _api().CatalogSyncService(connection)
    active = sync.import_full_snapshot(
        school_id=SCHOOL_ID,
        source_type=_api().SourceType.DLS_MARC,
        records=_records(2, prefix="UPGRADE"),
        confirm_anomaly=True,
    )
    connection.commit()

    apply_migrations(connection, current_dir)
    preserved = connection.execute(
        "SELECT COUNT(*) FROM holdings WHERE catalog_version_id = ?", (active.id,)
    ).fetchone()[0]
    latest = connection.execute(
        "SELECT migration_id FROM schema_migrations ORDER BY migration_id DESC LIMIT 1"
    ).fetchone()[0]
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(source_documents)")
    }
    foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
    connection.close()

    assert preserved == 2
    assert latest == "0004b_catalog_hardening"
    assert {
        "requested_start_local_date",
        "requested_through_local_date",
    } <= columns
    assert foreign_keys == []
