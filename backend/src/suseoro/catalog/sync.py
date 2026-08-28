"""Atomic full-snapshot activation and MARC delta synchronization."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from suseoro.catalog.contracts import (
    CatalogRecord,
    CatalogVersion,
    DeltaApplyResult,
    DeltaFile,
    DeltaWindow,
    ParserStatus,
    SourceType,
)
from suseoro.catalog.repository import CatalogRepository
from suseoro.security.sessions import format_utc, utc_now

SEOUL = ZoneInfo("Asia/Seoul")


class CatalogValidationError(ValueError):
    pass


class SourcePolicyError(ValueError):
    pass


class FullSnapshotRequired(RuntimeError):
    pass


class ActivationConfirmationRequired(RuntimeError):
    def __init__(
        self, reason: str, *, previous_count: int | None, new_count: int
    ) -> None:
        self.reason = reason
        self.previous_count = previous_count
        self.new_count = new_count
        super().__init__(reason)


@contextmanager
def _atomic(connection: sqlite3.Connection):
    name = "task5_" + uuid.uuid4().hex
    connection.execute(f"SAVEPOINT {name}")
    try:
        yield
    except BaseException:
        connection.execute(f"ROLLBACK TO {name}")
        connection.execute(f"RELEASE {name}")
        raise
    else:
        connection.execute(f"RELEASE {name}")


def _validate_records(records: tuple[CatalogRecord, ...]) -> None:
    seen: set[str] = set()
    for record in records:
        source_item_id = record.source_item_id.strip()
        if not source_item_id:
            raise CatalogValidationError("source item ID is required")
        if source_item_id in seen:
            raise CatalogValidationError(
                f"duplicate source item ID in catalog input: {source_item_id}"
            )
        if not record.title.strip():
            raise CatalogValidationError(
                f"catalog source item {source_item_id} has no title"
            )
        seen.add(source_item_id)


class CatalogSyncService:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.repository = CatalogRepository(connection)

    @staticmethod
    def _allow_full(source_type: SourceType) -> None:
        if source_type == SourceType.DLS_API:
            raise SourcePolicyError(
                "DLS API is disabled until formally approved and compatibility-verified"
            )
        if source_type not in (SourceType.DLS_MARC, SourceType.DLS_EXCEL):
            raise SourcePolicyError("unsupported catalog full snapshot source")

    @staticmethod
    def _allow_delta(source_type: SourceType) -> None:
        if source_type != SourceType.DLS_MARC:
            raise SourcePolicyError(
                "delta synchronization is supported only for DLS MARC"
            )

    def stage_full_snapshot(
        self,
        *,
        school_id: str,
        source_type: SourceType,
        records,
        source_document_id: str | None = None,
        as_of_date: date | None = None,
        created_at: datetime | None = None,
    ) -> CatalogVersion:
        source_type = SourceType(source_type)
        self._allow_full(source_type)
        bounded_records = tuple(records)
        _validate_records(bounded_records)
        now = created_at or utc_now()
        local_date = as_of_date or now.astimezone(SEOUL).date()
        with _atomic(self.connection):
            version = self.repository.create_staging_version(
                school_id=school_id,
                source_type=source_type,
                import_mode="FULL_SNAPSHOT",
                source_document_id=source_document_id,
                as_of_local_date=local_date,
                created_at=now,
            )
            for record in bounded_records:
                self.repository.put_holding(
                    version_id=version.id,
                    school_id=school_id,
                    source_type=source_type,
                    record=record,
                    created_at=now,
                )
            self.repository.refresh_item_count(version.id)
        return self.repository.version(version.id)

    def activate_staged(
        self,
        version_id: str,
        *,
        confirm_anomaly: bool = False,
        activated_at: datetime | None = None,
    ) -> CatalogVersion:
        staged = self.repository.version(version_id)
        if staged is None or staged.status != "STAGING":
            raise CatalogValidationError(
                "catalog version is not an activatable staging version"
            )
        if staged.import_mode != "FULL_SNAPSHOT":
            raise CatalogValidationError(
                "delta versions are activated only by delta sync"
            )
        previous = self.repository.active_version(staged.school_id)
        if not confirm_anomaly:
            if staged.item_count == 0:
                raise ActivationConfirmationRequired(
                    "EMPTY_SNAPSHOT",
                    previous_count=previous.item_count if previous else None,
                    new_count=0,
                )
            if previous is not None and previous.item_count:
                change = (
                    abs(staged.item_count - previous.item_count) / previous.item_count
                )
                if change > 0.30:
                    raise ActivationConfirmationRequired(
                        "SNAPSHOT_SIZE_CHANGE",
                        previous_count=previous.item_count,
                        new_count=staged.item_count,
                    )
        now = activated_at or utc_now()
        local_date = staged.as_of_local_date or now.astimezone(SEOUL).date()
        with _atomic(self.connection):
            current = self.repository.active_version(staged.school_id)
            if current is not None:
                self.connection.execute(
                    "UPDATE catalog_versions SET status = 'SUPERSEDED' WHERE id = ? AND status = 'ACTIVE'",
                    (current.id,),
                )
            updated = self.connection.execute(
                """
                UPDATE catalog_versions
                SET status = 'ACTIVE', activated_at = ?, anomaly_confirmed = ?
                WHERE id = ? AND status = 'STAGING'
                """,
                (format_utc(now), int(confirm_anomaly), staged.id),
            )
            if updated.rowcount != 1:
                raise CatalogValidationError("catalog staging activation was lost")
            self.connection.execute(
                """
                INSERT INTO catalog_source_state (
                    school_id, source_type, active_version_id,
                    watermark_local_date, last_full_snapshot_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (school_id) DO UPDATE SET
                    source_type = excluded.source_type,
                    active_version_id = excluded.active_version_id,
                    watermark_local_date = excluded.watermark_local_date,
                    last_full_snapshot_at = excluded.last_full_snapshot_at,
                    updated_at = excluded.updated_at
                """,
                (
                    staged.school_id,
                    staged.source_type.value,
                    staged.id,
                    local_date.isoformat(),
                    format_utc(now),
                    format_utc(now),
                ),
            )
        return self.repository.version(staged.id)

    def import_full_snapshot(
        self,
        *,
        school_id: str,
        source_type: SourceType,
        records,
        confirm_anomaly: bool = False,
        source_document_id: str | None = None,
        as_of_date: date | None = None,
        activated_at: datetime | None = None,
    ) -> CatalogVersion:
        now = activated_at or utc_now()
        staged = self.stage_full_snapshot(
            school_id=school_id,
            source_type=source_type,
            records=records,
            source_document_id=source_document_id,
            as_of_date=as_of_date,
            created_at=now,
        )
        return self.activate_staged(
            staged.id, confirm_anomaly=confirm_anomaly, activated_at=now
        )

    def delta_window(self, school_id: str, *, through_date: date) -> DeltaWindow:
        state = self.repository.source_state(school_id)
        if state is None or state.watermark_local_date is None:
            raise FullSnapshotRequired("a full MARC snapshot is required before deltas")
        if through_date < state.watermark_local_date:
            raise CatalogValidationError("delta watermark cannot move backwards")
        return DeltaWindow(
            start=state.watermark_local_date - timedelta(days=2), end=through_date
        )

    @staticmethod
    def _batch_key(
        registration_file: DeltaFile, update_file: DeltaFile, through_date: date
    ) -> str:
        canonical = json.dumps(
            {
                "registration": [
                    registration_file.source_file_sha256,
                    registration_file.parser_version,
                ],
                "update": [
                    update_file.source_file_sha256,
                    update_file.parser_version,
                ],
                "through": through_date.isoformat(),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def _record_partial_delta(
        self,
        *,
        school_id: str,
        registration_file: DeltaFile,
        update_file: DeltaFile,
        through_date: date,
        batch_key: str,
        now: datetime,
    ) -> DeltaApplyResult:
        with _atomic(self.connection):
            self.connection.execute(
                """
                INSERT INTO catalog_delta_batches (
                    id, school_id, batch_key, registration_sha256,
                    registration_parser_version, registration_status,
                    update_sha256, update_parser_version, update_status,
                    through_local_date, status, error_json, created_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PARTIAL_FAILURE', ?, ?, ?)
                ON CONFLICT (school_id, batch_key) DO UPDATE SET
                    registration_status = excluded.registration_status,
                    update_status = excluded.update_status,
                    status = 'PARTIAL_FAILURE',
                    error_json = excluded.error_json,
                    completed_at = excluded.completed_at
                """,
                (
                    str(uuid.uuid4()),
                    school_id,
                    batch_key,
                    registration_file.source_file_sha256,
                    registration_file.parser_version,
                    registration_file.status.value,
                    update_file.source_file_sha256,
                    update_file.parser_version,
                    update_file.status.value,
                    through_date.isoformat(),
                    json.dumps(
                        {
                            "code": "BOTH_DELTA_FILES_REQUIRED",
                            "registration": registration_file.status.value,
                            "update": update_file.status.value,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    format_utc(now),
                    format_utc(now),
                ),
            )
        state = self.repository.source_state(school_id)
        return DeltaApplyResult(
            applied=False,
            status="PARTIAL_FAILURE",
            catalog_version_id=state.active_version_id if state else None,
            watermark_local_date=state.watermark_local_date if state else None,
        )

    def apply_delta(
        self,
        *,
        school_id: str,
        source_type: SourceType,
        registration_file: DeltaFile,
        update_file: DeltaFile,
        through_date: date,
        now: datetime | None = None,
    ) -> DeltaApplyResult:
        source_type = SourceType(source_type)
        self._allow_delta(source_type)
        current_time = now or utc_now()
        state = self.repository.source_state(school_id)
        active = self.repository.active_version(school_id)
        if (
            state is None
            or active is None
            or state.source_type != SourceType.DLS_MARC
            or active.source_type != SourceType.DLS_MARC
        ):
            raise FullSnapshotRequired("an active full MARC snapshot is required")
        if (
            state.watermark_local_date is not None
            and through_date < state.watermark_local_date
        ):
            raise CatalogValidationError("delta watermark cannot move backwards")
        local_today = current_time.astimezone(SEOUL).date()
        last_full_local = state.last_full_snapshot_at.astimezone(SEOUL).date()
        if (local_today - last_full_local).days >= 90:
            raise FullSnapshotRequired("a full MARC snapshot is required every 90 days")
        batch_key = self._batch_key(registration_file, update_file, through_date)
        existing = self.connection.execute(
            """
            SELECT status, catalog_version_id FROM catalog_delta_batches
            WHERE school_id = ? AND batch_key = ?
            """,
            (school_id, batch_key),
        ).fetchone()
        if existing is not None and existing["status"] == "SUCCESS":
            return DeltaApplyResult(
                applied=True,
                status="SUCCESS",
                catalog_version_id=existing["catalog_version_id"],
                watermark_local_date=state.watermark_local_date,
                idempotent=True,
            )
        both_succeeded = all(
            file.status == ParserStatus.SUCCESS and file.activation_allowed
            for file in (registration_file, update_file)
        )
        if not both_succeeded:
            return self._record_partial_delta(
                school_id=school_id,
                registration_file=registration_file,
                update_file=update_file,
                through_date=through_date,
                batch_key=batch_key,
                now=current_time,
            )
        _validate_records(registration_file.records)
        _validate_records(update_file.records)
        with _atomic(self.connection):
            self.connection.execute(
                """
                INSERT INTO catalog_delta_batches (
                    id, school_id, batch_key, registration_sha256,
                    registration_parser_version, registration_status,
                    update_sha256, update_parser_version, update_status,
                    through_local_date, status, created_at
                ) VALUES (?, ?, ?, ?, ?, 'SUCCESS', ?, ?, 'SUCCESS', ?, 'PENDING', ?)
                ON CONFLICT (school_id, batch_key) DO UPDATE SET
                    registration_status = 'SUCCESS', update_status = 'SUCCESS',
                    status = 'PENDING', error_json = NULL, completed_at = NULL
                """,
                (
                    str(uuid.uuid4()),
                    school_id,
                    batch_key,
                    registration_file.source_file_sha256,
                    registration_file.parser_version,
                    update_file.source_file_sha256,
                    update_file.parser_version,
                    through_date.isoformat(),
                    format_utc(current_time),
                ),
            )
            staged = self.repository.create_staging_version(
                school_id=school_id,
                source_type=SourceType.DLS_MARC,
                import_mode="DELTA",
                parent_version_id=active.id,
                as_of_local_date=through_date,
                created_at=current_time,
            )
            self.repository.copy_version(
                source_version_id=active.id,
                target_version_id=staged.id,
                now=current_time,
            )
            # Registration changes are applied first.  Update-file records are
            # deliberately second so that the DLS update export wins conflicts.
            for record in (*registration_file.records, *update_file.records):
                self.repository.put_holding(
                    version_id=staged.id,
                    school_id=school_id,
                    source_type=SourceType.DLS_MARC,
                    record=record,
                    created_at=current_time,
                )
            count = self.repository.refresh_item_count(staged.id)
            self.connection.execute(
                "UPDATE catalog_versions SET status = 'SUPERSEDED' WHERE id = ? AND status = 'ACTIVE'",
                (active.id,),
            )
            activated = self.connection.execute(
                """
                UPDATE catalog_versions
                SET status = 'ACTIVE', activated_at = ?, item_count = ?
                WHERE id = ? AND status = 'STAGING'
                """,
                (format_utc(current_time), count, staged.id),
            )
            if activated.rowcount != 1:
                raise CatalogValidationError("delta activation was lost")
            state_updated = self.connection.execute(
                """
                UPDATE catalog_source_state
                SET active_version_id = ?, watermark_local_date = ?, updated_at = ?
                WHERE school_id = ? AND source_type = 'DLS_MARC'
                """,
                (
                    staged.id,
                    through_date.isoformat(),
                    format_utc(current_time),
                    school_id,
                ),
            )
            if state_updated.rowcount != 1:
                raise CatalogValidationError(
                    "active MARC source changed during delta sync"
                )
            self.connection.execute(
                """
                UPDATE catalog_delta_batches
                SET status = 'SUCCESS', catalog_version_id = ?,
                    error_json = NULL, completed_at = ?
                WHERE school_id = ? AND batch_key = ?
                """,
                (staged.id, format_utc(current_time), school_id, batch_key),
            )
        return DeltaApplyResult(
            applied=True,
            status="SUCCESS",
            catalog_version_id=staged.id,
            watermark_local_date=through_date,
        )
