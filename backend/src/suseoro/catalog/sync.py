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
from suseoro.db.connection import verify_fts_integrity
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
def _atomic(connection: sqlite3.Connection, *, immediate: bool = False):
    owns_transaction = immediate and not connection.in_transaction
    if owns_transaction:
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()
        return
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
    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        _allow_unbound_sources: bool = False,
    ) -> None:
        self.connection = connection
        self.repository = CatalogRepository(connection)
        self._allow_unbound_sources = _allow_unbound_sources

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

    def _validate_source_document(
        self,
        *,
        source_document_id: str | None,
        school_id: str,
        role: str,
        formats: tuple[str, ...],
        records: tuple[CatalogRecord, ...],
        allow_unbound: bool,
    ) -> sqlite3.Row | None:
        if source_document_id is None:
            if allow_unbound:
                return None
            raise CatalogValidationError("a bound source document is required")
        document = self.connection.execute(
            """
            SELECT sd.*, sf.sha256, sf.detected_format AS file_format,
                   sf.id AS source_file_id
            FROM source_documents sd
            JOIN source_files sf ON sf.id = sd.source_file_id
            WHERE sd.id = ?
            """,
            (source_document_id,),
        ).fetchone()
        if document is None or document["school_id"] != school_id:
            raise CatalogValidationError(
                "source document does not belong to the requested school"
            )
        if document["role"] != role:
            raise CatalogValidationError(f"source document role must be {role}")
        if (
            document["detected_format"] not in formats
            or document["file_format"] != document["detected_format"]
        ):
            raise CatalogValidationError("source document format is not allowed")
        if document["status"] in ("PENDING", "FAILED"):
            raise CatalogValidationError("source document parsing did not succeed")
        if not document["activation_allowed"]:
            raise CatalogValidationError("source document is not safe for activation")
        rows = self.connection.execute(
            "SELECT id, status FROM source_rows WHERE source_document_id = ?",
            (source_document_id,),
        ).fetchall()
        successful_ids = {row["id"] for row in rows if row["status"] == "SUCCESS"}
        record_row_ids = tuple(record.source_row_id for record in records)
        record_ids = set(record_row_ids)
        if (
            None in record_ids
            or record_ids != successful_ids
            or len(record_row_ids) != len(record_ids)
        ):
            raise CatalogValidationError(
                "catalog records must account for every successful source document row"
            )
        return document

    def stage_full_snapshot(
        self,
        *,
        school_id: str,
        source_type: SourceType,
        records,
        source_document_id: str | None = None,
        as_of_date: date | None = None,
        created_at: datetime | None = None,
        _allow_unbound_source: bool | None = None,
    ) -> CatalogVersion:
        source_type = SourceType(source_type)
        self._allow_full(source_type)
        bounded_records = tuple(records)
        _validate_records(bounded_records)
        formats = (
            ("MARC",)
            if source_type == SourceType.DLS_MARC
            else ("CSV", "TSV", "TXT", "XLS", "XLSX", "XLSB", "ODS")
        )
        self._validate_source_document(
            source_document_id=source_document_id,
            school_id=school_id,
            role="CATALOG_FULL",
            formats=formats,
            records=bounded_records,
            allow_unbound=(
                self._allow_unbound_sources
                if _allow_unbound_source is None
                else _allow_unbound_source
            ),
        )
        now = created_at or utc_now()
        local_date = as_of_date or now.astimezone(SEOUL).date()
        with _atomic(self.connection):
            expected = self.repository.active_version(school_id)
            version = self.repository.create_staging_version(
                school_id=school_id,
                source_type=source_type,
                import_mode="FULL_SNAPSHOT",
                source_document_id=source_document_id,
                as_of_local_date=local_date,
                expected_active_version_id=expected.id if expected else None,
                expected_active_item_count=expected.item_count if expected else None,
                expected_active_source_type=expected.source_type if expected else None,
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
        now = activated_at or utc_now()
        with _atomic(self.connection, immediate=True):
            staged_row = self.connection.execute(
                "SELECT * FROM catalog_versions WHERE id = ?", (version_id,)
            ).fetchone()
            if staged_row is None or staged_row["status"] != "STAGING":
                raise CatalogValidationError(
                    "catalog version is not an activatable staging version"
                )
            if staged_row["import_mode"] != "FULL_SNAPSHOT":
                raise CatalogValidationError(
                    "delta versions are activated only by delta sync"
                )
            actual_count = self.connection.execute(
                "SELECT COUNT(*) FROM holdings WHERE catalog_version_id = ?",
                (version_id,),
            ).fetchone()[0]
            normalized_count = self.connection.execute(
                "SELECT COUNT(*) FROM normalized_works WHERE catalog_version_id = ?",
                (version_id,),
            ).fetchone()[0]
            fts_count = self.connection.execute(
                "SELECT COUNT(*) FROM holding_search_fts WHERE catalog_version_id = ?",
                (version_id,),
            ).fetchone()[0]
            if staged_row["item_count"] != actual_count:
                raise CatalogValidationError("staged catalog item count changed")
            if normalized_count != actual_count or fts_count != actual_count:
                raise CatalogValidationError(
                    "staged catalog representation count changed"
                )
            try:
                verify_fts_integrity(self.connection)
            except sqlite3.DatabaseError as error:
                raise CatalogValidationError(
                    "FTS search posting integrity check failed"
                ) from error

            current = self.repository.active_version(staged_row["school_id"])
            expected_id = staged_row["expected_active_version_id"]
            if (current.id if current else None) != expected_id:
                raise CatalogValidationError("active catalog changed after staging")
            if current is not None and (
                current.item_count != staged_row["expected_active_item_count"]
                or current.source_type.value
                != staged_row["expected_active_source_type"]
            ):
                raise CatalogValidationError("active catalog count or source changed")
            state = self.connection.execute(
                "SELECT * FROM catalog_source_state WHERE school_id = ?",
                (staged_row["school_id"],),
            ).fetchone()
            if current is not None and (
                state is None
                or state["active_version_id"] != current.id
                or state["source_type"] != current.source_type.value
            ):
                raise CatalogValidationError("active catalog source state changed")

            if not confirm_anomaly:
                if actual_count == 0:
                    raise ActivationConfirmationRequired(
                        "EMPTY_SNAPSHOT",
                        previous_count=current.item_count if current else None,
                        new_count=0,
                    )
                if current is not None and current.item_count:
                    change = abs(actual_count - current.item_count) / current.item_count
                    if change > 0.30:
                        raise ActivationConfirmationRequired(
                            "SNAPSHOT_SIZE_CHANGE",
                            previous_count=current.item_count,
                            new_count=actual_count,
                        )

            local_date = (
                date.fromisoformat(staged_row["as_of_local_date"])
                if staged_row["as_of_local_date"]
                else now.astimezone(SEOUL).date()
            )
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
                (format_utc(now), int(confirm_anomaly), version_id),
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
                    staged_row["school_id"],
                    staged_row["source_type"],
                    version_id,
                    local_date.isoformat(),
                    format_utc(now),
                    format_utc(now),
                ),
            )
        return self.repository.version(version_id)

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
        _allow_unbound_source: bool | None = None,
    ) -> CatalogVersion:
        now = activated_at or utc_now()
        staged = self.stage_full_snapshot(
            school_id=school_id,
            source_type=source_type,
            records=records,
            source_document_id=source_document_id,
            as_of_date=as_of_date,
            created_at=now,
            _allow_unbound_source=_allow_unbound_source,
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
                    registration_file.source_document_id,
                    registration_file.source_file_sha256,
                    registration_file.parser_version,
                ],
                "update": [
                    update_file.source_document_id,
                    update_file.source_file_sha256,
                    update_file.parser_version,
                ],
                "through": through_date.isoformat(),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def _validate_delta_binding(
        self,
        *,
        school_id: str,
        delta_file: DeltaFile,
        role: str,
        allow_unbound: bool,
        expected_start_date: date,
        expected_through_date: date,
    ) -> dict:
        if delta_file.source_document_id is None:
            if not allow_unbound:
                raise CatalogValidationError("delta source document is required")
            return {
                "document_id": None,
                "source_file_id": None,
                "rows": (),
                "total": len(delta_file.records),
                "success": len(delta_file.records),
                "errors": 0,
            }
        document = self.connection.execute(
            """
            SELECT sd.*, sf.id AS source_file_id, sf.sha256,
                   sf.detected_format AS file_format
            FROM source_documents sd
            JOIN source_files sf ON sf.id = sd.source_file_id
            WHERE sd.id = ?
            """,
            (delta_file.source_document_id,),
        ).fetchone()
        if document is None or document["school_id"] != school_id:
            raise CatalogValidationError("delta source document school mismatch")
        if (
            delta_file.window is None
            or not delta_file.window.start_inclusive
            or not delta_file.window.end_inclusive
            or delta_file.window.start != expected_start_date
            or delta_file.window.end != expected_through_date
            or document["requested_start_local_date"] != expected_start_date.isoformat()
            or document["requested_through_local_date"]
            != expected_through_date.isoformat()
        ):
            raise CatalogValidationError(
                "delta source document and request window must agree"
            )
        if document["role"] != role:
            raise CatalogValidationError(f"delta source document role must be {role}")
        if document["detected_format"] != "MARC" or document["file_format"] != "MARC":
            raise CatalogValidationError("delta source document format must be MARC")
        if document["sha256"] != delta_file.source_file_sha256:
            raise CatalogValidationError("delta source file hash mismatch")
        if document["parser_version"] != delta_file.parser_version:
            raise CatalogValidationError("delta parser version mismatch")
        if bool(document["activation_allowed"]) != delta_file.activation_allowed:
            raise CatalogValidationError("delta activation_allowed mismatch")
        parser_succeeded = document["status"] in ("SUCCESS", "ROW_ERROR") and bool(
            document["activation_allowed"]
        )
        expected_status = (
            ParserStatus.SUCCESS if parser_succeeded else ParserStatus.FAILED
        )
        if delta_file.status != expected_status:
            raise CatalogValidationError(
                "delta parser status does not match source document"
            )
        rows = self.connection.execute(
            """
            SELECT id, status, error_code, error_message
            FROM source_rows WHERE source_document_id = ? ORDER BY source_row, id
            """,
            (delta_file.source_document_id,),
        ).fetchall()
        successful_ids = {row["id"] for row in rows if row["status"] == "SUCCESS"}
        record_row_ids = tuple(record.source_row_id for record in delta_file.records)
        record_ids = set(record_row_ids)
        if parser_succeeded and (
            None in record_ids
            or record_ids != successful_ids
            or len(record_row_ids) != len(record_ids)
        ):
            raise CatalogValidationError(
                "delta records must account for every successful source row"
            )
        if not parser_succeeded and delta_file.records:
            raise CatalogValidationError(
                "failed delta documents cannot provide records"
            )
        return {
            "document_id": document["id"],
            "source_file_id": document["source_file_id"],
            "rows": rows,
            "total": len(rows),
            "success": len(successful_ids),
            "errors": len(rows) - len(successful_ids),
        }

    def _persist_delta_row_accounting(
        self, *, batch_id: str, school_id: str, role: str, binding: dict, now: datetime
    ) -> None:
        for row in binding["rows"]:
            outcome = "APPLIED" if row["status"] == "SUCCESS" else "ROW_ERROR"
            reason = (
                None
                if outcome == "APPLIED"
                else row["error_code"] or "SOURCE_ROW_ERROR"
            )
            self.connection.execute(
                """
                INSERT INTO catalog_delta_row_results (
                    id, batch_id, school_id, role, source_document_id,
                    source_row_id, outcome, reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (batch_id, role, source_row_id) DO UPDATE SET
                    outcome = excluded.outcome, reason = excluded.reason
                """,
                (
                    str(uuid.uuid4()),
                    batch_id,
                    school_id,
                    role,
                    binding["document_id"],
                    row["id"],
                    outcome,
                    reason,
                    format_utc(now),
                ),
            )

    def _record_partial_delta(
        self,
        *,
        school_id: str,
        registration_file: DeltaFile,
        update_file: DeltaFile,
        through_date: date,
        requested_start_date: date,
        batch_key: str,
        registration_binding: dict,
        update_binding: dict,
        now: datetime,
    ) -> DeltaApplyResult:
        with _atomic(self.connection):
            self.connection.execute(
                """
                INSERT INTO catalog_delta_batches (
                    id, school_id, batch_key, registration_sha256,
                    registration_parser_version, registration_status,
                    update_sha256, update_parser_version, update_status,
                    registration_source_document_id, update_source_document_id,
                    requested_start_local_date, through_local_date,
                    registration_total_rows, registration_success_rows,
                    registration_error_rows, update_total_rows,
                    update_success_rows, update_error_rows,
                    status, error_json, created_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          'PARTIAL_FAILURE', ?, ?, ?)
                ON CONFLICT (school_id, batch_key) DO UPDATE SET
                    registration_status = excluded.registration_status,
                    update_status = excluded.update_status,
                    registration_source_document_id = excluded.registration_source_document_id,
                    update_source_document_id = excluded.update_source_document_id,
                    requested_start_local_date = excluded.requested_start_local_date,
                    registration_total_rows = excluded.registration_total_rows,
                    registration_success_rows = excluded.registration_success_rows,
                    registration_error_rows = excluded.registration_error_rows,
                    update_total_rows = excluded.update_total_rows,
                    update_success_rows = excluded.update_success_rows,
                    update_error_rows = excluded.update_error_rows,
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
                    registration_binding["document_id"],
                    update_binding["document_id"],
                    requested_start_date.isoformat(),
                    through_date.isoformat(),
                    registration_binding["total"],
                    registration_binding["success"],
                    registration_binding["errors"],
                    update_binding["total"],
                    update_binding["success"],
                    update_binding["errors"],
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
            batch_id = self.connection.execute(
                "SELECT id FROM catalog_delta_batches WHERE school_id = ? AND batch_key = ?",
                (school_id, batch_key),
            ).fetchone()[0]
            self._persist_delta_row_accounting(
                batch_id=batch_id,
                school_id=school_id,
                role="REGISTRATION",
                binding=registration_binding,
                now=now,
            )
            self._persist_delta_row_accounting(
                batch_id=batch_id,
                school_id=school_id,
                role="UPDATE",
                binding=update_binding,
                now=now,
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
        _allow_unbound_sources: bool | None = None,
    ) -> DeltaApplyResult:
        source_type = SourceType(source_type)
        self._allow_delta(source_type)
        current_time = now or utc_now()
        local_today = current_time.astimezone(SEOUL).date()
        if through_date > local_today:
            raise CatalogValidationError(
                "delta through date cannot be after the current Asia/Seoul local date"
            )
        state = self.repository.source_state(school_id)
        active = self.repository.active_version(school_id)
        if (
            state is None
            or active is None
            or state.source_type != SourceType.DLS_MARC
            or active.source_type != SourceType.DLS_MARC
        ):
            raise FullSnapshotRequired("an active full MARC snapshot is required")
        requested_start_date = (
            state.watermark_local_date - timedelta(days=2)
            if state.watermark_local_date is not None
            else through_date - timedelta(days=2)
        )
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
        if (
            state.watermark_local_date is not None
            and through_date < state.watermark_local_date
        ):
            raise CatalogValidationError("delta watermark cannot move backwards")
        last_full_local = state.last_full_snapshot_at.astimezone(SEOUL).date()
        if (local_today - last_full_local).days >= 90:
            raise FullSnapshotRequired("a full MARC snapshot is required every 90 days")
        both_succeeded = all(
            file.status == ParserStatus.SUCCESS and file.activation_allowed
            for file in (registration_file, update_file)
        )
        if both_succeeded:
            _validate_records(registration_file.records)
            _validate_records(update_file.records)
        with _atomic(self.connection, immediate=True):
            locked_active = self.repository.active_version(school_id)
            locked_state = self.repository.source_state(school_id)
            if (
                locked_active is None
                or locked_state is None
                or locked_active.id != active.id
                or locked_state.active_version_id != active.id
                or locked_state.watermark_local_date != state.watermark_local_date
            ):
                raise CatalogValidationError(
                    "active MARC source changed during delta sync"
                )
            requested_start_date = (
                locked_state.watermark_local_date - timedelta(days=2)
                if locked_state.watermark_local_date is not None
                else through_date - timedelta(days=2)
            )
            allow_unbound = (
                self._allow_unbound_sources
                if _allow_unbound_sources is None
                else _allow_unbound_sources
            )
            if (
                registration_file.source_document_id is not None
                and registration_file.source_document_id
                == update_file.source_document_id
            ):
                raise CatalogValidationError("delta role inputs must be distinct files")
            registration_binding = self._validate_delta_binding(
                school_id=school_id,
                delta_file=registration_file,
                role="CATALOG_DELTA_REGISTRATION",
                allow_unbound=allow_unbound,
                expected_start_date=requested_start_date,
                expected_through_date=through_date,
            )
            update_binding = self._validate_delta_binding(
                school_id=school_id,
                delta_file=update_file,
                role="CATALOG_DELTA_UPDATE",
                allow_unbound=allow_unbound,
                expected_start_date=requested_start_date,
                expected_through_date=through_date,
            )
            if registration_binding["document_id"] is not None and (
                registration_binding["document_id"] == update_binding["document_id"]
                or registration_binding["source_file_id"]
                == update_binding["source_file_id"]
            ):
                raise CatalogValidationError("delta role inputs must be distinct files")
            if not both_succeeded:
                return self._record_partial_delta(
                    school_id=school_id,
                    registration_file=registration_file,
                    update_file=update_file,
                    through_date=through_date,
                    requested_start_date=requested_start_date,
                    batch_key=batch_key,
                    registration_binding=registration_binding,
                    update_binding=update_binding,
                    now=current_time,
                )
            self.connection.execute(
                """
                INSERT INTO catalog_delta_batches (
                    id, school_id, batch_key, registration_sha256,
                    registration_parser_version, registration_status,
                    update_sha256, update_parser_version, update_status,
                    registration_source_document_id, update_source_document_id,
                    requested_start_local_date, through_local_date,
                    registration_total_rows, registration_success_rows,
                    registration_error_rows, update_total_rows,
                    update_success_rows, update_error_rows, status, created_at
                ) VALUES (?, ?, ?, ?, ?, 'SUCCESS', ?, ?, 'SUCCESS', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          'PENDING', ?)
                ON CONFLICT (school_id, batch_key) DO UPDATE SET
                    registration_status = 'SUCCESS', update_status = 'SUCCESS',
                    registration_source_document_id = excluded.registration_source_document_id,
                    update_source_document_id = excluded.update_source_document_id,
                    requested_start_local_date = excluded.requested_start_local_date,
                    registration_total_rows = excluded.registration_total_rows,
                    registration_success_rows = excluded.registration_success_rows,
                    registration_error_rows = excluded.registration_error_rows,
                    update_total_rows = excluded.update_total_rows,
                    update_success_rows = excluded.update_success_rows,
                    update_error_rows = excluded.update_error_rows,
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
                    registration_binding["document_id"],
                    update_binding["document_id"],
                    requested_start_date.isoformat(),
                    through_date.isoformat(),
                    registration_binding["total"],
                    registration_binding["success"],
                    registration_binding["errors"],
                    update_binding["total"],
                    update_binding["success"],
                    update_binding["errors"],
                    format_utc(current_time),
                ),
            )
            batch_id = self.connection.execute(
                "SELECT id FROM catalog_delta_batches WHERE school_id = ? AND batch_key = ?",
                (school_id, batch_key),
            ).fetchone()[0]
            self._persist_delta_row_accounting(
                batch_id=batch_id,
                school_id=school_id,
                role="REGISTRATION",
                binding=registration_binding,
                now=current_time,
            )
            self._persist_delta_row_accounting(
                batch_id=batch_id,
                school_id=school_id,
                role="UPDATE",
                binding=update_binding,
                now=current_time,
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
