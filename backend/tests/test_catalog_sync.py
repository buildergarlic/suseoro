from __future__ import annotations

import importlib
import importlib.util
import sqlite3
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

from suseoro.db.connection import connect
from suseoro.db.migrations import apply_migrations

SCHOOL_ID = "20000000-0000-4000-8000-000000000001"
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
        CatalogSyncService=sync.CatalogSyncService,
        CatalogValidationError=sync.CatalogValidationError,
        DeltaFile=contracts.DeltaFile,
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
