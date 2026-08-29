"""Streaming source uploads and durable ingestion job routes."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import date
from pathlib import Path
from time import monotonic
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from suseoro.api.common import decode_cursor, page
from suseoro.api.dependencies import (
    AuthenticatedUser,
    current_user,
    database_connection,
    require_idempotency_key,
    require_if_match,
    require_request_id,
    require_role,
)
from suseoro.api.errors import domain_not_found, public_error_message
from suseoro.api.routes.events import publish_event
from suseoro.api.schemas import ApiErrorResponse, MappingRequired, UploadResponse
from suseoro.catalog.contracts import (
    CatalogRecord,
    DeltaFile,
    DeltaWindow,
    ParserStatus,
    SourceType,
)
from suseoro.catalog.sync import (
    ActivationConfirmationRequired,
    CatalogSyncService,
    CatalogValidationError,
    FullSnapshotRequired,
    SourcePolicyError,
)
from suseoro.db.connection import connect
from suseoro.ingestion.contracts import DocumentRole
from suseoro.ingestion.file_store import (
    FileTooLarge,
    ImmutableFileStore,
    StagedFile,
    StagedUploadRejected,
)
from suseoro.jobs.handlers import parser_version_for_format
from suseoro.jobs.public_errors import (
    sanitize_public_job_error,
    sanitize_public_mapping_required,
)
from suseoro.jobs.repository import JobRepository
from suseoro.jobs.source_snapshot import authoritative_comparison_sources
from suseoro.security.sessions import format_utc, utc_now
from suseoro.services.audit import record_audit_event
from suseoro.services.idempotency import (
    complete_idempotent_request,
    reserve_idempotency_key,
)
from suseoro.services.upload_idempotency import (
    UploadClaimLease,
    acquire_upload_claim,
    begin_upload_mutation,
    cleanup_and_release_upload_claim,
    complete_upload_claim,
    reserve_upload_idempotency_key,
    upload_scope_replay_allowance,
    validate_existing_upload_idempotency_key,
)

router = APIRouter(prefix="/api/v2", tags=["sources"])

UPLOAD_CLAIM_WAIT_DEADLINE_SECONDS = 2.0
UPLOAD_CLAIM_LEASE_SECONDS = 30.0


def _reopen_terminal_analysis_for_source_correction(
    connection: sqlite3.Connection,
    *,
    school_id: str,
    workspace_id: str,
    actor_id: str,
    request_id: str,
    audit_action: str,
) -> bool:
    """Return a terminal comparison to DRAFT before a source is corrected."""
    workspace = connection.execute(
        """
        SELECT status, row_version FROM acquisition_workspaces
        WHERE id = ? AND school_id = ?
        """,
        (workspace_id, school_id),
    ).fetchone()
    if workspace is None:
        raise domain_not_found("WORKSPACE_NOT_FOUND")
    if workspace["status"] == "DRAFT":
        return False
    if workspace["status"] != "ANALYZING":
        raise HTTPException(
            status_code=409,
            detail={"code": "SOURCE_COMPARISON_IN_PROGRESS"},
        )
    active_comparison = connection.execute(
        """
        SELECT 1 FROM durable_jobs
        WHERE school_id = ? AND workspace_id = ? AND job_type = 'COMPARE'
          AND status IN ('QUEUED', 'RUNNING', 'CANCEL_REQUESTED')
        LIMIT 1
        """,
        (school_id, workspace_id),
    ).fetchone()
    recoverable_comparison = connection.execute(
        """
        SELECT 1 FROM durable_jobs
        WHERE school_id = ? AND workspace_id = ? AND job_type = 'COMPARE'
          AND status IN ('FAILED', 'PARTIAL', 'CANCELLED')
        LIMIT 1
        """,
        (school_id, workspace_id),
    ).fetchone()
    downstream_approval = connection.execute(
        """
        SELECT 1 FROM approval_rows
        WHERE school_id = ? AND workspace_id = ? LIMIT 1
        """,
        (school_id, workspace_id),
    ).fetchone()
    if (
        active_comparison is not None
        or recoverable_comparison is None
        or downstream_approval is not None
    ):
        raise HTTPException(
            status_code=409,
            detail={"code": "SOURCE_COMPARISON_IN_PROGRESS"},
        )
    now = format_utc(utc_now())
    connection.execute(
        """
        DELETE FROM edit_locks
        WHERE school_id = ? AND entity_type = 'candidate_decision'
          AND entity_id IN (
            SELECT id FROM candidate_decisions WHERE workspace_id = ?
          )
        """,
        (school_id, workspace_id),
    )
    connection.execute(
        "DELETE FROM comparison_row_results WHERE workspace_id = ?",
        (workspace_id,),
    )
    connection.execute(
        "DELETE FROM candidate_decisions WHERE workspace_id = ?",
        (workspace_id,),
    )
    connection.execute(
        "DELETE FROM recommendations WHERE workspace_id = ?",
        (workspace_id,),
    )
    connection.execute(
        "DELETE FROM comparison_file_results WHERE workspace_id = ?",
        (workspace_id,),
    )
    updated = connection.execute(
        """
        UPDATE acquisition_workspaces
        SET status = 'DRAFT', row_version = row_version + 1,
            updated_at = ?
        WHERE id = ? AND school_id = ? AND status = 'ANALYZING'
        """,
        (now, workspace_id, school_id),
    )
    if updated.rowcount != 1:
        raise HTTPException(
            status_code=409,
            detail={"code": "SOURCE_COMPARISON_IN_PROGRESS"},
        )
    record_audit_event(
        connection,
        actor_id=actor_id,
        school_id=school_id,
        action=audit_action,
        entity_type="acquisition_workspace",
        entity_id=workspace_id,
        before={
            "status": "ANALYZING",
            "row_version": workspace["row_version"],
        },
        after={
            "status": "DRAFT",
            "row_version": int(workspace["row_version"]) + 1,
        },
        request_id=request_id,
    )
    return True


def _reserve_repair_generation(
    database_path: Path,
    *,
    obligation_id: str,
    school_id: str,
    workspace_id: str,
    generation: int,
    role: DocumentRole,
    vendor_scope: str,
    requested_start_local_date: str | None,
    requested_through_local_date: str | None,
    upload_claim_id: str,
    actor_id: str,
    request_id: str,
    confirm_configuration: bool = False,
) -> None:
    """Reserve the newest user selection before the upload mutation lock.

    This separate durable fence lets a later selection supersede an older
    request even while that older request is still staging or waiting to write.
    """
    with connect(database_path) as reservation:
        reservation.execute("BEGIN IMMEDIATE")
        current = reservation.execute(
            """
            SELECT generation, resolved_source_document_id,
                   pending_source_document_id, role,
                   vendor_scope, requested_start_local_date,
                   requested_through_local_date,
                   active_upload_claim_id, workspace.status AS workspace_status,
                   workspace.row_version AS workspace_row_version
            FROM upload_repair_obligations AS repair
            JOIN acquisition_workspaces AS workspace
              ON workspace.id = repair.workspace_id
             AND workspace.school_id = repair.school_id
            WHERE repair.id = ? AND repair.school_id = ?
              AND repair.workspace_id = ?
            """,
            (obligation_id, school_id, workspace_id),
        ).fetchone()
        if current is None or generation < int(current["generation"]):
            reservation.rollback()
            raise HTTPException(
                status_code=409, detail={"code": "UPLOAD_REPAIR_SUPERSEDED"}
            )
        if current["workspace_status"] == "ANALYZING":
            _reopen_terminal_analysis_for_source_correction(
                reservation,
                actor_id=actor_id,
                school_id=school_id,
                workspace_id=workspace_id,
                request_id=request_id,
                audit_action="UPLOAD_REPAIR_REOPENED_WORKSPACE",
            )
        elif current["workspace_status"] != "DRAFT":
            reservation.rollback()
            raise HTTPException(
                status_code=409,
                detail={"code": "SOURCE_COMPARISON_IN_PROGRESS"},
            )
        if generation == int(current["generation"]):
            if current["active_upload_claim_id"] == upload_claim_id:
                reservation.rollback()
                return
            reservation.rollback()
            raise HTTPException(
                status_code=409, detail={"code": "UPLOAD_REPAIR_SUPERSEDED"}
            )
        if current["role"] not in {"UNKNOWN", role.value}:
            reservation.rollback()
            raise HTTPException(
                status_code=422, detail={"code": "UPLOAD_REPAIR_ROLE_MISMATCH"}
            )
        if current["role"] == "UNKNOWN" and (
            not confirm_configuration or role == DocumentRole.UNKNOWN
        ):
            reservation.rollback()
            raise HTTPException(
                status_code=422,
                detail={"code": "UPLOAD_REPAIR_CONFIGURATION_CONFIRMATION_REQUIRED"},
            )
        if current["role"] != "UNKNOWN" and (
            current["vendor_scope"] != vendor_scope
            or current["requested_start_local_date"] != requested_start_local_date
            or current["requested_through_local_date"] != requested_through_local_date
        ):
            reservation.rollback()
            raise HTTPException(
                status_code=422,
                detail={"code": "UPLOAD_REPAIR_CONFIG_MISMATCH"},
            )
        if current["role"] == "UNKNOWN" and role != DocumentRole.UNKNOWN:
            reservation.execute(
                """
                UPDATE upload_repair_obligations
                SET role = ?, vendor_scope = ?, requested_start_local_date = ?,
                    requested_through_local_date = ?, generation = ?,
                    status = 'REPAIRING', active_upload_claim_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    role.value,
                    vendor_scope,
                    requested_start_local_date,
                    requested_through_local_date,
                    generation,
                    upload_claim_id,
                    format_utc(utc_now()),
                    obligation_id,
                ),
            )
            record_audit_event(
                reservation,
                actor_id=actor_id,
                school_id=school_id,
                action="UPLOAD_REPAIR_CONFIGURATION_CONFIRMED",
                entity_type="upload_repair_obligation",
                entity_id=obligation_id,
                before={"role": "UNKNOWN", "generation": current["generation"]},
                after={"role": role.value, "generation": generation},
                request_id=request_id,
            )
        else:
            reservation.execute(
                """
                UPDATE upload_repair_obligations
                SET generation = ?, status = 'REPAIRING',
                    active_upload_claim_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    generation,
                    upload_claim_id,
                    format_utc(utc_now()),
                    obligation_id,
                ),
            )
        reservation.commit()


class SourceMapping(BaseModel):
    role: DocumentRole
    mapping: dict[str, str] = Field(default_factory=dict)
    remember_template: bool = False
    vendor_scope: str = Field(default="*", min_length=1, max_length=200)


class CatalogStageRequest(BaseModel):
    source_type: SourceType


class CatalogActivateRequest(BaseModel):
    confirm_anomaly: bool = False


class CatalogDeltaApplyRequest(BaseModel):
    source_type: SourceType
    registration_source_id: str
    update_source_id: str
    requested_start_local_date: date
    requested_through_local_date: date


def _field_value(fields: dict[str, Any], name: str) -> Any:
    value = fields.get(name)
    return value.get("value") if isinstance(value, dict) else value


def _catalog_version(version) -> dict[str, Any]:
    return {
        "id": version.id,
        "school_id": version.school_id,
        "source_type": version.source_type.value,
        "import_mode": version.import_mode,
        "status": version.status,
        "item_count": version.item_count,
        "as_of_local_date": (
            version.as_of_local_date.isoformat() if version.as_of_local_date else None
        ),
        "created_at": (format_utc(version.created_at) if version.created_at else None),
        "activated_at": (
            format_utc(version.activated_at) if version.activated_at else None
        ),
    }


def _catalog_records_for_source(
    connection: sqlite3.Connection, source_id: str, *, require_stable_id: bool = False
) -> tuple[CatalogRecord, ...]:
    rows = connection.execute(
        """
        SELECT id, raw_json, fields_json FROM source_rows
        WHERE source_document_id = ? AND status = 'SUCCESS'
        ORDER BY source_row, id
        """,
        (source_id,),
    ).fetchall()
    records = []
    for row in rows:
        raw = json.loads(row["raw_json"])
        fields = json.loads(row["fields_json"])
        registration = _field_value(fields, "registration_number")
        if require_stable_id and not registration:
            raise CatalogValidationError(
                "delta catalog rows require a stable registration number"
            )
        records.append(
            CatalogRecord(
                source_item_id=str(registration or row["id"]),
                source_row_id=row["id"],
                registration_number=(str(registration) if registration else None),
                isbn=_field_value(fields, "isbn"),
                title=str(_field_value(fields, "title") or ""),
                authors=tuple(
                    part.strip()
                    for part in str(_field_value(fields, "author") or "").split(";")
                    if part.strip()
                ),
                publisher=_field_value(fields, "publisher"),
                call_number=_field_value(fields, "call_number"),
                raw_fields=raw,
            )
        )
    return tuple(records)


def _delta_file_for_source(
    connection: sqlite3.Connection,
    *,
    school_id: str,
    source_id: str,
    expected_role: DocumentRole,
    window: DeltaWindow,
) -> DeltaFile:
    source = _source_row(connection, school_id, source_id)
    if source is None:
        raise CatalogValidationError("delta source document was not found")
    if source["configured_role"] != expected_role:
        raise CatalogValidationError(
            f"delta source document role must be {expected_role.value}"
        )
    activation_allowed = bool(source["activation_allowed"])
    rows = connection.execute(
        """
        SELECT status FROM source_rows
        WHERE source_document_id = ? ORDER BY source_row, id
        """,
        (source_id,),
    ).fetchall()
    parser_succeeded = (
        source["status"] == "SUCCESS"
        and activation_allowed
        and bool(rows)
        and all(row["status"] == "SUCCESS" for row in rows)
    )
    return DeltaFile(
        source_document_id=source_id,
        source_file_sha256=source["sha256"],
        parser_version=source["parser_version"],
        status=(ParserStatus.SUCCESS if parser_succeeded else ParserStatus.FAILED),
        records=(
            _catalog_records_for_source(connection, source_id, require_stable_id=True)
            if parser_succeeded
            else ()
        ),
        activation_allowed=activation_allowed,
        window=window,
    )


def _workspace_exists(connection, school_id: str, workspace_id: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM acquisition_workspaces WHERE id = ? AND school_id = ?",
            (workspace_id, school_id),
        ).fetchone()
        is not None
    )


def _source_processing_active(
    connection: sqlite3.Connection, *, school_id: str, source_id: str
) -> bool:
    return (
        connection.execute(
            """
            SELECT 1
            FROM durable_jobs AS job,
                 json_each(job.payload_json, '$.source_document_ids') AS source
            WHERE job.school_id = ?
              AND job.job_type IN ('INGEST', 'PARSE')
              AND job.status IN ('QUEUED', 'RUNNING', 'CANCEL_REQUESTED')
              AND source.value = ?
            LIMIT 1
            """,
            (school_id, source_id),
        ).fetchone()
        is not None
    )


def _source_row(connection, school_id: str, source_id: str):
    return connection.execute(
        """
        SELECT document.*, file.sha256, file.size_bytes, file.original_filename,
               COALESCE(config.role, document.role) AS configured_role,
               COALESCE(config.row_version, 1) AS row_version,
               COALESCE(config.mapping_json, '{}') AS mapping_json,
               COALESCE(config.vendor_scope, '*') AS configured_vendor_scope,
               COALESCE(config.remember_template, 0) AS remember_template
        FROM source_documents document
        JOIN source_files file ON file.id = document.source_file_id
        LEFT JOIN source_configurations config ON config.source_document_id = document.id
        WHERE document.id = ? AND document.school_id = ?
        """,
        (source_id, school_id),
    ).fetchone()


def _source(row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "filename": row["original_filename"],
        "sha256": row["sha256"],
        "size_bytes": row["size_bytes"],
        "role": row["configured_role"],
        "status": row["status"],
        "detected_format": row["detected_format"],
        "mapping": json.loads(row["mapping_json"]),
        "vendor_scope": row["configured_vendor_scope"],
        "remember_template": bool(row["remember_template"]),
        "requested_start_local_date": row["requested_start_local_date"],
        "requested_through_local_date": row["requested_through_local_date"],
        "parsed_config_version": row["parsed_config_version"],
        "row_version": row["row_version"],
        "created_at": row["created_at"],
        "completed_at": row["completed_at"],
        "latest_job_id": None,
        "latest_result": None,
    }


def _source_with_discovery(
    connection: sqlite3.Connection,
    row,
    *,
    workspace_id: str | None = None,
) -> dict[str, Any]:
    result = _source(row)
    latest = connection.execute(
        """
        SELECT job.id
        FROM durable_jobs AS job
        WHERE job.school_id = ? AND job.job_type IN ('INGEST', 'PARSE')
          AND (? IS NULL OR job.workspace_id = ?)
          AND EXISTS (
              SELECT 1
              FROM json_each(job.payload_json, '$.source_document_ids') AS source
              WHERE source.value = ?
          )
        ORDER BY job.created_at DESC, job.id DESC LIMIT 1
        """,
        (row["school_id"], workspace_id, workspace_id, row["id"]),
    ).fetchone()
    if latest is not None:
        result["latest_job_id"] = latest["id"]
        latest_result = next(
            (
                item
                for item in JobRepository(connection).file_results(latest["id"])
                if item["source_document_id"] == row["id"]
            ),
            None,
        )
        if latest_result is not None:
            result["latest_result"] = _public_job_file_result(
                latest_result, fallback_code="PARSER_FAILURE"
            )
    return result


@router.post(
    "/workspaces/{workspace_id}/sources",
    status_code=202,
    response_model=UploadResponse,
    responses={
        207: {"model": UploadResponse, "description": "일부 파일만 접수됨"},
        413: {"model": ApiErrorResponse, "description": "업로드 용량 제한 초과"},
    },
    summary="원본 자료 올리기",
    operation_id="uploadSources",
)
def upload_sources(
    workspace_id: str,
    request: Request,
    response: Response,
    files: list[UploadFile] = File(...),
    role: DocumentRole = Form(DocumentRole.UNKNOWN),
    vendor_scope: str | None = Form(None),
    requested_start_local_date: str | None = Form(None),
    requested_through_local_date: str | None = Form(None),
    repair_obligation_id: str | None = Form(None),
    repair_generation: int | None = Form(None),
    replacement_source_document_id: str | None = Form(None),
    confirm_repair_configuration: bool = Form(False),
    user: AuthenticatedUser = Depends(require_role("OPERATOR")),
    connection: sqlite3.Connection = Depends(database_connection),
    idempotency_key: str = Depends(require_idempotency_key),
    request_id: str = Depends(require_request_id),
):
    if not _workspace_exists(connection, user.school_id, workspace_id):
        raise domain_not_found("WORKSPACE_NOT_FOUND")
    settings = request.app.state.settings
    if (repair_obligation_id is None) != (repair_generation is None):
        raise HTTPException(status_code=422, detail={"code": "INVALID_REPAIR_REQUEST"})
    if repair_obligation_id is not None and (
        len(files) != 1 or repair_generation is None or repair_generation < 1
    ):
        raise HTTPException(status_code=422, detail={"code": "INVALID_REPAIR_REQUEST"})
    if replacement_source_document_id is not None and (
        repair_obligation_id is not None or len(files) != 1
    ):
        raise HTTPException(
            status_code=422, detail={"code": "INVALID_REPLACEMENT_REQUEST"}
        )
    normalized_vendor_scope = vendor_scope.strip() or "*" if vendor_scope else "*"
    if repair_obligation_id is not None:
        repair_contract = connection.execute(
            """
            SELECT role, vendor_scope, requested_start_local_date,
                   requested_through_local_date
            FROM upload_repair_obligations
            WHERE id = ? AND school_id = ? AND workspace_id = ?
            """,
            (repair_obligation_id, user.school_id, workspace_id),
        ).fetchone()
        if repair_contract is None:
            raise HTTPException(
                status_code=409, detail={"code": "UPLOAD_REPAIR_SUPERSEDED"}
            )
        if repair_contract["role"] == "UNKNOWN" and (
            not confirm_repair_configuration or role == DocumentRole.UNKNOWN
        ):
            raise HTTPException(
                status_code=422,
                detail={"code": "UPLOAD_REPAIR_CONFIGURATION_CONFIRMATION_REQUIRED"},
            )
        if repair_contract["role"] != "UNKNOWN":
            if repair_contract["role"] != role.value:
                raise HTTPException(
                    status_code=422,
                    detail={"code": "UPLOAD_REPAIR_ROLE_MISMATCH"},
                )
            if (
                vendor_scope is not None
                and normalized_vendor_scope != repair_contract["vendor_scope"]
            ):
                raise HTTPException(
                    status_code=422,
                    detail={"code": "UPLOAD_REPAIR_CONFIG_MISMATCH"},
                )
            submitted_window = (
                requested_start_local_date,
                requested_through_local_date,
            )
            stored_window = (
                repair_contract["requested_start_local_date"],
                repair_contract["requested_through_local_date"],
            )
            if any(value is not None for value in submitted_window) and (
                submitted_window != stored_window
            ):
                raise HTTPException(
                    status_code=422,
                    detail={"code": "UPLOAD_REPAIR_CONFIG_MISMATCH"},
                )
            role = DocumentRole(repair_contract["role"])
            normalized_vendor_scope = repair_contract["vendor_scope"]
            requested_start_local_date = repair_contract["requested_start_local_date"]
            requested_through_local_date = repair_contract[
                "requested_through_local_date"
            ]
    vendor_scope = normalized_vendor_scope
    route = f"POST /api/v2/workspaces/{workspace_id}/sources"
    replay_allowance = upload_scope_replay_allowance(
        settings.database_path,
        school_id=user.school_id,
        actor_id=user.id,
        route=route,
        key=idempotency_key,
    )
    filenames = [upload.filename or "unnamed" for upload in files]
    replay_shape = replay_allowance is not None and replay_allowance.matches(filenames)
    if len(files) > settings.upload_max_files and not replay_shape:
        raise HTTPException(
            status_code=413, detail={"code": "UPLOAD_BATCH_LIMIT_EXCEEDED"}
        )
    delta_roles = {
        DocumentRole.CATALOG_DELTA_REGISTRATION,
        DocumentRole.CATALOG_DELTA_UPDATE,
    }
    requested_start = requested_through = None
    if role in delta_roles and replacement_source_document_id is None:
        if not requested_start_local_date or not requested_through_local_date:
            raise HTTPException(
                status_code=422, detail={"code": "DELTA_WINDOW_REQUIRED"}
            )
        try:
            requested_start = date.fromisoformat(requested_start_local_date)
            requested_through = date.fromisoformat(requested_through_local_date)
        except ValueError as error:
            raise HTTPException(
                status_code=422, detail={"code": "INVALID_DELTA_WINDOW"}
            ) from error
        if requested_start > requested_through:
            raise HTTPException(
                status_code=422, detail={"code": "INVALID_DELTA_WINDOW"}
            )
    elif replacement_source_document_id is None and (
        requested_start_local_date or requested_through_local_date
    ):
        raise HTTPException(status_code=422, detail={"code": "INVALID_DELTA_WINDOW"})
    replay_max_batch_bytes = (
        max(settings.upload_max_batch_bytes, replay_allowance.max_batch_bytes)
        if replay_shape and replay_allowance is not None
        else settings.upload_max_batch_bytes
    )
    store = ImmutableFileStore(
        settings.sources_dir,
        max_bytes=settings.upload_max_file_bytes,
    )
    now = format_utc(utc_now())
    prepared: list[dict[str, Any]] = []
    staged_files: list[StagedFile] = []
    client_files: list[dict[str, Any]] = []
    newly_published: list[Path] = []
    aggregate_bytes = 0
    upload_lease: UploadClaimLease | None = None
    claim_deadline: float | None = None

    def counted_chunks(upload: UploadFile):
        nonlocal aggregate_bytes
        while True:
            chunk = upload.file.read(64 * 1024)
            if not chunk:
                return
            aggregate_bytes += len(chunk)
            if aggregate_bytes > replay_max_batch_bytes:
                raise HTTPException(
                    status_code=413,
                    detail={"code": "UPLOAD_BATCH_LIMIT_EXCEEDED"},
                )
            yield chunk

    try:
        for upload_index, upload in enumerate(files):
            filename = upload.filename or "unnamed"
            content_type = upload.content_type
            store.max_bytes = (
                replay_allowance.stage_limit(
                    upload_index, settings.upload_max_file_bytes
                )
                if replay_shape and replay_allowance is not None
                else settings.upload_max_file_bytes
            )
            try:
                staged = store.stage(counted_chunks(upload), filename=filename)
                staged_files.append(staged)
                client_files.append(
                    {
                        "filename": filename,
                        "content_type": content_type,
                        "sha256": staged.sha256,
                        "size_bytes": staged.size,
                    }
                )
                prepared.append({"filename": filename, "staged": staged, "error": None})
            except StagedUploadRejected as rejected:
                error = rejected.cause
                code = (
                    "FILE_TOO_LARGE"
                    if isinstance(error, FileTooLarge)
                    else "UNSUPPORTED_FILE_TYPE"
                )
                client_files.append(
                    {
                        "filename": filename,
                        "content_type": content_type,
                        "sha256": rejected.sha256,
                        "size_bytes": rejected.size,
                    }
                )
                prepared.append(
                    {
                        "filename": filename,
                        "staged": None,
                        "error": {"code": code, "message": str(error)},
                    }
                )
            finally:
                upload.file.close()

        request_body = {"workspace_id": workspace_id, "files": client_files}
        if replacement_source_document_id is None:
            request_body.update(
                {
                    "role": role.value,
                    "vendor_scope": vendor_scope.strip() or "*",
                    "requested_start_local_date": (
                        requested_start.isoformat() if requested_start else None
                    ),
                    "requested_through_local_date": (
                        requested_through.isoformat() if requested_through else None
                    ),
                }
            )
        if repair_obligation_id is not None:
            request_body["repair_obligation_id"] = repair_obligation_id
            request_body["repair_generation"] = repair_generation
            request_body["confirm_repair_configuration"] = confirm_repair_configuration
        if replacement_source_document_id is not None:
            request_body["replacement_source_document_id"] = (
                replacement_source_document_id
            )
        validate_existing_upload_idempotency_key(
            connection,
            school_id=user.school_id,
            actor_id=user.id,
            route=route,
            key=idempotency_key,
            request_body=request_body,
            current_file_errors=[
                item["error"]["code"] if item["error"] else None for item in prepared
            ],
        )
        claim_deadline = monotonic() + UPLOAD_CLAIM_WAIT_DEADLINE_SECONDS
        claim = acquire_upload_claim(
            settings.database_path,
            school_id=user.school_id,
            actor_id=user.id,
            route=route,
            key=idempotency_key,
            request_body=request_body,
            wait_deadline_seconds=UPLOAD_CLAIM_WAIT_DEADLINE_SECONDS,
            lease_seconds=UPLOAD_CLAIM_LEASE_SECONDS,
        )
        if not isinstance(claim, UploadClaimLease):
            if claim.status >= 400:
                return JSONResponse(status_code=claim.status, content=claim.body)
            response.status_code = claim.status
            return claim.body
        upload_lease = claim
        if repair_obligation_id is not None and repair_generation is not None:
            _reserve_repair_generation(
                settings.database_path,
                obligation_id=repair_obligation_id,
                school_id=user.school_id,
                workspace_id=workspace_id,
                generation=repair_generation,
                role=role,
                vendor_scope=vendor_scope,
                requested_start_local_date=(
                    requested_start.isoformat() if requested_start else None
                ),
                requested_through_local_date=(
                    requested_through.isoformat() if requested_through else None
                ),
                upload_claim_id=upload_lease.id,
                actor_id=user.id,
                request_id=request_id,
                confirm_configuration=confirm_repair_configuration,
            )
        begin_upload_mutation(
            connection,
            upload_lease,
            wait_deadline_seconds=max(0.0, claim_deadline - monotonic()),
        )
        legacy_replay = reserve_upload_idempotency_key(
            connection,
            school_id=user.school_id,
            actor_id=user.id,
            route=route,
            key=idempotency_key,
            request_body=request_body,
            current_file_errors=[
                item["error"]["code"] if item["error"] else None for item in prepared
            ],
        )
        if legacy_replay is not None:
            complete_upload_claim(
                connection,
                upload_lease,
                status=legacy_replay.status,
                body=legacy_replay.body,
            )
            connection.commit()
            upload_lease = None
            if legacy_replay.status >= 400:
                return JSONResponse(
                    status_code=legacy_replay.status, content=legacy_replay.body
                )
            response.status_code = legacy_replay.status
            return legacy_replay.body

        replacement_contract = None
        source_mapping_json = "{}"
        source_remember_template = 0
        if replacement_source_document_id is not None:
            if any(item["staged"] is None for item in prepared):
                raise HTTPException(
                    status_code=422,
                    detail={"code": "SOURCE_REPLACEMENT_FILE_REJECTED"},
                )
            replacement_contract = connection.execute(
                """
                SELECT COALESCE(config.role, document.role) AS configured_role,
                       COALESCE(config.vendor_scope, '*') AS vendor_scope,
                       COALESCE(config.mapping_json, '{}') AS mapping_json,
                       COALESCE(config.remember_template, 0) AS remember_template,
                       document.requested_start_local_date,
                       document.requested_through_local_date,
                       config.row_version,
                       (
                         SELECT COUNT(*) FROM workspace_sources all_links
                         WHERE all_links.source_document_id = document.id
                           AND all_links.school_id = document.school_id
                       ) AS workspace_link_count
                FROM source_documents document
                JOIN workspace_sources link ON link.source_document_id = document.id
                LEFT JOIN source_configurations config
                  ON config.source_document_id = document.id
                WHERE document.id = ? AND document.school_id = ?
                  AND link.workspace_id = ? AND link.school_id = ?
                """,
                (
                    replacement_source_document_id,
                    user.school_id,
                    workspace_id,
                    user.school_id,
                ),
            ).fetchone()
            if (
                replacement_contract is None
                or replacement_contract["configured_role"] == DocumentRole.UNKNOWN.value
            ):
                raise HTTPException(
                    status_code=409,
                    detail={"code": "SOURCE_REPLACEMENT_SUPERSEDED"},
                )
            if int(replacement_contract["workspace_link_count"]) != 1:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "SOURCE_REPLACEMENT_SHARED"},
                )
            role = DocumentRole(replacement_contract["configured_role"])
            vendor_scope = replacement_contract["vendor_scope"]
            source_mapping_json = replacement_contract["mapping_json"]
            source_remember_template = int(replacement_contract["remember_template"])
            requested_start_local_date = replacement_contract[
                "requested_start_local_date"
            ]
            requested_through_local_date = replacement_contract[
                "requested_through_local_date"
            ]
            requested_start = (
                date.fromisoformat(requested_start_local_date)
                if requested_start_local_date
                else None
            )
            requested_through = (
                date.fromisoformat(requested_through_local_date)
                if requested_through_local_date
                else None
            )

        workspace_status = connection.execute(
            "SELECT status FROM acquisition_workspaces WHERE id = ? AND school_id = ?",
            (workspace_id, user.school_id),
        ).fetchone()["status"]
        if (
            replacement_source_document_id is not None
            and role == DocumentRole.PURCHASE_REQUEST
            and workspace_status == "ANALYZING"
        ):
            _reopen_terminal_analysis_for_source_correction(
                connection,
                school_id=user.school_id,
                workspace_id=workspace_id,
                actor_id=user.id,
                request_id=request_id,
                audit_action="SOURCE_CORRECTION_REOPENED_WORKSPACE",
            )
            workspace_status = "DRAFT"
        if role == DocumentRole.PURCHASE_REQUEST and workspace_status != "DRAFT":
            raise HTTPException(
                status_code=409,
                detail={"code": "SOURCE_COMPARISON_IN_PROGRESS"},
            )

        repair_obligation = None
        if repair_obligation_id is not None:
            repair_obligation = connection.execute(
                """
                SELECT * FROM upload_repair_obligations
                WHERE id = ? AND school_id = ? AND workspace_id = ?
                """,
                (repair_obligation_id, user.school_id, workspace_id),
            ).fetchone()
            if (
                repair_obligation is None
                or repair_obligation["status"] != "REPAIRING"
                or repair_generation != int(repair_obligation["generation"])
                or repair_obligation["active_upload_claim_id"] != upload_lease.id
            ):
                raise HTTPException(
                    status_code=409, detail={"code": "UPLOAD_REPAIR_SUPERSEDED"}
                )

        items: list[dict[str, Any]] = []
        accepted_ids: list[str] = []
        for item_index, item in enumerate(prepared):
            staged = item["staged"]
            if staged is None:
                obligation_id = None
                obligation_generation = None
                if repair_obligation_id is None:
                    obligation_id = str(uuid.uuid4())
                    connection.execute(
                        """
                        INSERT INTO upload_repair_obligations (
                            id, school_id, workspace_id, upload_claim_id, actor_id,
                            file_index, filename, content_sha256, size_bytes,
                            role, vendor_scope, requested_start_local_date,
                            requested_through_local_date, error_code, status,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                  'UNRESOLVED', ?, ?)
                        """,
                        (
                            obligation_id,
                            user.school_id,
                            workspace_id,
                            upload_lease.id,
                            user.id,
                            item_index,
                            item["filename"],
                            client_files[item_index]["sha256"],
                            client_files[item_index]["size_bytes"],
                            role.value,
                            vendor_scope.strip() or "*",
                            requested_start.isoformat() if requested_start else None,
                            requested_through.isoformat()
                            if requested_through
                            else None,
                            item["error"]["code"],
                            now,
                            now,
                        ),
                    )
                    obligation_generation = 0
                else:
                    obligation_id = repair_obligation_id
                    obligation_generation = repair_generation
                items.append(
                    {
                        "filename": item["filename"],
                        "status": "FAILED",
                        "source_id": None,
                        "error": item["error"],
                        "repair_obligation_id": obligation_id,
                        "repair_generation": obligation_generation,
                    }
                )
                continue

            stored = store.publish(staged)
            if stored.created:
                newly_published.append(stored.path)
            existing = connection.execute(
                "SELECT id FROM source_files WHERE sha256 = ?", (stored.sha256,)
            ).fetchone()
            source_file_id = existing["id"] if existing else str(uuid.uuid4())
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO source_files (
                        id, sha256, size_bytes, storage_path, original_filename,
                        detected_format, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source_file_id,
                        stored.sha256,
                        stored.size,
                        str(stored.path),
                        stored.original_filename,
                        stored.detected_format,
                        now,
                    ),
                )
            source_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO source_documents (
                    id, source_file_id, school_id, role, parser_version,
                    status, detected_format, requested_start_local_date,
                    requested_through_local_date, created_at
                ) VALUES (?, ?, ?, ?, ?, 'PENDING', ?, ?, ?, ?)
                """,
                (
                    source_id,
                    source_file_id,
                    user.school_id,
                    role,
                    parser_version_for_format(stored.detected_format),
                    stored.detected_format,
                    requested_start.isoformat() if requested_start else None,
                    requested_through.isoformat() if requested_through else None,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO workspace_sources (
                    workspace_id, source_document_id, school_id, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (workspace_id, source_id, user.school_id, now),
            )
            connection.execute(
                """
                INSERT INTO source_configurations (
                    source_document_id, school_id, role, mapping_json,
                    vendor_scope, remember_template, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_id,
                    user.school_id,
                    role,
                    source_mapping_json,
                    vendor_scope.strip() or "*",
                    source_remember_template,
                    now,
                ),
            )
            accepted_ids.append(source_id)
            items.append(
                {
                    "filename": item["filename"],
                    "status": "ACCEPTED",
                    "source_id": source_id,
                    "error": None,
                    "repair_obligation_id": repair_obligation_id,
                    "repair_generation": repair_generation,
                }
            )
            if repair_obligation_id is not None:
                previous_source_id = (
                    repair_obligation["pending_source_document_id"]
                    or repair_obligation["resolved_source_document_id"]
                )
                accepted = connection.execute(
                    """
                    UPDATE upload_repair_obligations
                    SET status = 'REPAIRING', pending_source_document_id = ?,
                        resolved_source_document_id = NULL, updated_at = ?
                    WHERE id = ? AND status = 'REPAIRING' AND generation = ?
                      AND active_upload_claim_id = ?
                    """,
                    (
                        source_id,
                        now,
                        repair_obligation_id,
                        repair_generation,
                        upload_lease.id,
                    ),
                )
                if accepted.rowcount != 1:
                    raise HTTPException(
                        status_code=409, detail={"code": "UPLOAD_REPAIR_SUPERSEDED"}
                    )
                if previous_source_id is not None and previous_source_id != source_id:
                    connection.execute(
                        """
                        DELETE FROM workspace_sources
                        WHERE workspace_id = ? AND source_document_id = ?
                          AND school_id = ?
                        """,
                        (workspace_id, previous_source_id, user.school_id),
                    )
                record_audit_event(
                    connection,
                    actor_id=user.id,
                    school_id=user.school_id,
                    action="UPLOAD_REPAIR_REPLACEMENT_ACCEPTED",
                    entity_type="upload_repair_obligation",
                    entity_id=repair_obligation_id,
                    before={
                        "status": "REPAIRING",
                        "generation": repair_generation,
                        "source_id": previous_source_id,
                    },
                    after={"status": "REPAIRING", "pending_source_id": source_id},
                    request_id=request_id,
                )

        if replacement_source_document_id is not None and accepted_ids:
            superseded = connection.execute(
                """
                UPDATE source_configurations
                SET role = 'UNKNOWN', row_version = row_version + 1,
                    updated_at = ?
                WHERE source_document_id = ? AND school_id = ? AND role = ?
                """,
                (
                    now,
                    replacement_source_document_id,
                    user.school_id,
                    role.value,
                ),
            )
            if superseded.rowcount != 1:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "SOURCE_REPLACEMENT_SUPERSEDED"},
                )
            record_audit_event(
                connection,
                actor_id=user.id,
                school_id=user.school_id,
                action="SOURCE_DOCUMENT_REPLACED",
                entity_type="source_document",
                entity_id=replacement_source_document_id,
                before={"role": role.value},
                after={
                    "role": DocumentRole.UNKNOWN.value,
                    "replacement_source_document_id": accepted_ids[0],
                },
                request_id=request_id,
            )

        job = None
        if accepted_ids:
            job = JobRepository(connection).create(
                school_id=user.school_id,
                workspace_id=workspace_id,
                job_type="INGEST",
                payload={
                    "source_document_ids": accepted_ids,
                    "role": role,
                    "request_id": request_id,
                },
                progress_total=len(accepted_ids),
            )
        result = {"job_id": job.id if job else None, "items": items}
        status_code = 207 if any(item["status"] == "FAILED" for item in items) else 202
        record_audit_event(
            connection,
            actor_id=user.id,
            school_id=user.school_id,
            action="SOURCES_UPLOADED",
            entity_type="durable_job",
            entity_id=job.id if job else None,
            before=None,
            after={
                "accepted": len(accepted_ids),
                "failed": len(items) - len(accepted_ids),
            },
            request_id=request_id,
        )
        if job:
            publish_event(
                connection,
                school_id=user.school_id,
                workspace_id=workspace_id,
                event_type="job.progress",
                data={
                    "job_id": job.id,
                    "status": job.status,
                    "stage": job.stage,
                    "current": 0,
                    "total": len(accepted_ids),
                },
            )
        complete_idempotent_request(
            connection,
            school_id=user.school_id,
            actor_id=user.id,
            route=route,
            key=idempotency_key,
            status=status_code,
            body=result,
        )
        complete_upload_claim(
            connection,
            upload_lease,
            status=status_code,
            body=result,
        )
        connection.commit()
        upload_lease = None
        response.status_code = status_code
        return result
    except Exception:
        connection.rollback()
        if upload_lease is not None:
            cleanup_and_release_upload_claim(
                settings.database_path,
                upload_lease,
                newly_published,
                wait_deadline_seconds=(
                    max(0.0, claim_deadline - monotonic())
                    if claim_deadline is not None
                    else 0.0
                ),
            )
        raise
    finally:
        for staged in staged_files:
            store.discard(staged)


@router.get(
    "/workspaces/{workspace_id}/sources",
    summary="원본 자료 목록 보기",
    operation_id="listSources",
)
def list_sources(
    workspace_id: str,
    user: AuthenticatedUser = Depends(current_user),
    connection: sqlite3.Connection = Depends(database_connection),
    status: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
):
    decoded = decode_cursor(cursor, 2)
    clauses = ["link.school_id = ?", "link.workspace_id = ?"]
    parameters: list[object] = [user.school_id, workspace_id]
    if status:
        clauses.append("document.status = ?")
        parameters.append(status)
    if decoded:
        clauses.append(
            "(link.created_at < ? OR (link.created_at = ? AND document.id < ?))"
        )
        parameters.extend((decoded[0], decoded[0], decoded[1]))
    rows = connection.execute(
        f"""
        SELECT document.*, file.sha256, file.size_bytes, file.original_filename,
               COALESCE(config.role, document.role) AS configured_role,
               COALESCE(config.row_version, 1) AS row_version,
               COALESCE(config.mapping_json, '{{}}') AS mapping_json,
               COALESCE(config.vendor_scope, '*') AS configured_vendor_scope,
               COALESCE(config.remember_template, 0) AS remember_template
        FROM workspace_sources link
        JOIN source_documents document ON document.id = link.source_document_id
        JOIN source_files file ON file.id = document.source_file_id
        LEFT JOIN source_configurations config ON config.source_document_id = document.id
        WHERE {" AND ".join(clauses)}
        ORDER BY link.created_at DESC, document.id DESC LIMIT ?
        """,
        (*parameters, limit + 1),
    ).fetchall()
    items = [
        _source_with_discovery(connection, row, workspace_id=workspace_id)
        for row in rows
    ]
    return page(
        items, limit=limit, cursor_values=lambda item: (item["created_at"], item["id"])
    )


@router.get(
    "/workspaces/{workspace_id}/upload-repairs",
    summary="다시 올려야 하는 파일 보기",
    operation_id="listUploadRepairs",
)
def list_upload_repairs(
    workspace_id: str,
    user: AuthenticatedUser = Depends(current_user),
    connection: sqlite3.Connection = Depends(database_connection),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
):
    if not _workspace_exists(connection, user.school_id, workspace_id):
        raise domain_not_found("WORKSPACE_NOT_FOUND")
    decoded = decode_cursor(cursor, 2)
    clauses = [
        "school_id = ?",
        "workspace_id = ?",
        "status != 'RESOLVED'",
    ]
    parameters: list[object] = [user.school_id, workspace_id]
    if decoded:
        clauses.append("(created_at < ? OR (created_at = ? AND id < ?))")
        parameters.extend((decoded[0], decoded[0], decoded[1]))
    rows = connection.execute(
        f"""
        SELECT id, filename, error_code, status, generation, role, vendor_scope,
               requested_start_local_date, requested_through_local_date,
               resolved_source_document_id, created_at, updated_at
        FROM upload_repair_obligations
        WHERE {" AND ".join(clauses)}
        ORDER BY created_at DESC, id DESC LIMIT ?
        """,
        (*parameters, limit + 1),
    ).fetchall()
    items = [
        {
            "id": row["id"],
            "filename": row["filename"],
            "error": {
                "code": row["error_code"],
                "message": public_error_message(row["error_code"]),
            },
            "status": row["status"],
            "generation": row["generation"],
            "role": row["role"],
            "vendor_scope": row["vendor_scope"],
            "requested_start_local_date": row["requested_start_local_date"],
            "requested_through_local_date": row["requested_through_local_date"],
            "configuration_confirmation_required": row["role"] == "UNKNOWN",
            "resolved_source_id": row["resolved_source_document_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]
    return page(
        items, limit=limit, cursor_values=lambda item: (item["created_at"], item["id"])
    )


@router.get(
    "/sources/{source_id}", summary="원본 자료 상태 보기", operation_id="getSource"
)
def get_source(
    source_id: str,
    response: Response,
    user: AuthenticatedUser = Depends(current_user),
    connection: sqlite3.Connection = Depends(database_connection),
):
    row = _source_row(connection, user.school_id, source_id)
    if row is None:
        raise domain_not_found("SOURCE_NOT_FOUND")
    response.headers["ETag"] = f'"{row["row_version"]}"'
    return _source_with_discovery(connection, row)


@router.patch(
    "/sources/{source_id}/mapping",
    summary="자료 역할과 열 연결 저장하기",
    operation_id="updateSourceMapping",
)
def update_source_mapping(
    source_id: str,
    payload: SourceMapping,
    response: Response,
    user: AuthenticatedUser = Depends(require_role("OPERATOR")),
    connection: sqlite3.Connection = Depends(database_connection),
    submitted_version: int = Depends(require_if_match),
    idempotency_key: str = Depends(require_idempotency_key),
    request_id: str = Depends(require_request_id),
):
    current = _source_row(connection, user.school_id, source_id)
    if current is None:
        raise domain_not_found("SOURCE_NOT_FOUND")
    route = f"PATCH /api/v2/sources/{source_id}/mapping"
    replay = reserve_idempotency_key(
        connection,
        school_id=user.school_id,
        actor_id=user.id,
        route=route,
        key=idempotency_key,
        request_body={**payload.model_dump(), "row_version": submitted_version},
    )
    if replay is not None:
        connection.rollback()
        response.headers["ETag"] = f'"{replay.body["row_version"]}"'
        return replay.body
    current = _source_row(connection, user.school_id, source_id)
    if current is None:
        connection.rollback()
        raise domain_not_found("SOURCE_NOT_FOUND")
    linked_workspaces = connection.execute(
        """
        SELECT workspace.id, workspace.status
        FROM workspace_sources link
        JOIN acquisition_workspaces workspace ON workspace.id = link.workspace_id
        WHERE link.source_document_id = ? AND link.school_id = ?
        """,
        (source_id, user.school_id),
    ).fetchall()
    if len(linked_workspaces) != 1:
        raise HTTPException(
            status_code=409,
            detail={"code": "SOURCE_SHARED_ACROSS_WORKSPACES"},
        )
    comparison_authoritative = (
        current["configured_role"] == DocumentRole.PURCHASE_REQUEST.value
        or payload.role == DocumentRole.PURCHASE_REQUEST
    )
    if comparison_authoritative:
        for linked in linked_workspaces:
            if linked["status"] == "ANALYZING":
                _reopen_terminal_analysis_for_source_correction(
                    connection,
                    school_id=user.school_id,
                    workspace_id=linked["id"],
                    actor_id=user.id,
                    request_id=request_id,
                    audit_action="SOURCE_CORRECTION_REOPENED_WORKSPACE",
                )
        non_draft = connection.execute(
            """
            SELECT 1 FROM workspace_sources link
            JOIN acquisition_workspaces workspace ON workspace.id = link.workspace_id
            WHERE link.source_document_id = ? AND link.school_id = ?
              AND workspace.status != 'DRAFT'
            LIMIT 1
            """,
            (source_id, user.school_id),
        ).fetchone()
        if non_draft is not None:
            raise HTTPException(
                status_code=409,
                detail={"code": "SOURCE_COMPARISON_IN_PROGRESS"},
            )
    if _source_processing_active(
        connection, school_id=user.school_id, source_id=source_id
    ):
        raise HTTPException(
            status_code=409, detail={"code": "SOURCE_PROCESSING_IN_PROGRESS"}
        )
    from suseoro.services.concurrency import VersionConflict

    updated = connection.execute(
        """
        UPDATE source_configurations SET role = ?, mapping_json = ?,
            remember_template = ?, vendor_scope = ?,
            row_version = row_version + 1, updated_at = ?
        WHERE source_document_id = ? AND school_id = ? AND row_version = ?
        """,
        (
            payload.role,
            json.dumps(payload.mapping, ensure_ascii=False, sort_keys=True),
            int(payload.remember_template),
            payload.vendor_scope.strip(),
            format_utc(utc_now()),
            source_id,
            user.school_id,
            submitted_version,
        ),
    )
    if updated.rowcount != 1:
        raise VersionConflict(current["row_version"], submitted_version)
    connection.execute(
        "DELETE FROM source_rows WHERE source_document_id = ?", (source_id,)
    )
    connection.execute(
        """
        UPDATE source_documents
        SET status = 'PENDING', completed_at = NULL,
            parsed_config_version = NULL
        WHERE id = ? AND school_id = ?
        """,
        (source_id, user.school_id),
    )
    row = _source_row(connection, user.school_id, source_id)
    result = _source(row)
    record_audit_event(
        connection,
        actor_id=user.id,
        school_id=user.school_id,
        action="SOURCE_MAPPING_UPDATED",
        entity_type="source_document",
        entity_id=source_id,
        before={
            "role": current["configured_role"],
            "mapping": json.loads(current["mapping_json"]),
            "row_version": current["row_version"],
        },
        after={
            "role": payload.role,
            "mapping": payload.mapping,
            "row_version": row["row_version"],
        },
        request_id=request_id,
    )
    complete_idempotent_request(
        connection,
        school_id=user.school_id,
        actor_id=user.id,
        route=route,
        key=idempotency_key,
        status=200,
        body=result,
    )
    connection.commit()
    response.headers["ETag"] = f'"{row["row_version"]}"'
    return result


@router.post(
    "/sources/{source_id}/parse",
    status_code=202,
    summary="자료 분석 시작하기",
    operation_id="parseSource",
)
def parse_source(
    source_id: str,
    user: AuthenticatedUser = Depends(require_role("OPERATOR")),
    connection: sqlite3.Connection = Depends(database_connection),
    idempotency_key: str = Depends(require_idempotency_key),
    request_id: str = Depends(require_request_id),
):
    linked_rows = connection.execute(
        """
        SELECT link.workspace_id, workspace.status AS workspace_status
        FROM source_documents document
        JOIN workspace_sources link ON link.source_document_id = document.id
        JOIN acquisition_workspaces workspace ON workspace.id = link.workspace_id
        WHERE document.id = ? AND document.school_id = ?
        """,
        (source_id, user.school_id),
    ).fetchall()
    if not linked_rows:
        raise domain_not_found("SOURCE_NOT_FOUND")
    if len(linked_rows) != 1:
        raise HTTPException(
            status_code=409,
            detail={"code": "SOURCE_SHARED_ACROSS_WORKSPACES"},
        )
    row = linked_rows[0]
    route = f"POST /api/v2/sources/{source_id}/parse"
    replay = reserve_idempotency_key(
        connection,
        school_id=user.school_id,
        actor_id=user.id,
        route=route,
        key=idempotency_key,
        request_body={"source_id": source_id},
    )
    if replay is not None:
        connection.rollback()
        return replay.body
    source_state = connection.execute(
        """
        SELECT COALESCE(config.role, document.role) AS configured_role,
               MAX(CASE WHEN workspace.status != 'DRAFT' THEN 1 ELSE 0 END)
                   AS has_fenced_workspace
        FROM source_documents document
        JOIN workspace_sources link ON link.source_document_id = document.id
        JOIN acquisition_workspaces workspace ON workspace.id = link.workspace_id
        LEFT JOIN source_configurations config
          ON config.source_document_id = document.id
        WHERE document.id = ? AND document.school_id = ?
        GROUP BY document.id
        """,
        (source_id, user.school_id),
    ).fetchone()
    if source_state is None:
        raise domain_not_found("SOURCE_NOT_FOUND")
    if (
        source_state["configured_role"] == DocumentRole.PURCHASE_REQUEST.value
        and source_state["has_fenced_workspace"]
    ):
        raise HTTPException(
            status_code=409,
            detail={"code": "SOURCE_COMPARISON_IN_PROGRESS"},
        )
    if _source_processing_active(
        connection, school_id=user.school_id, source_id=source_id
    ):
        raise HTTPException(
            status_code=409, detail={"code": "SOURCE_PROCESSING_IN_PROGRESS"}
        )
    connection.execute(
        "DELETE FROM source_rows WHERE source_document_id = ?", (source_id,)
    )
    connection.execute(
        """
        UPDATE source_documents
        SET status = 'PENDING', completed_at = NULL,
            parsed_config_version = NULL
        WHERE id = ? AND school_id = ?
        """,
        (source_id, user.school_id),
    )
    job = JobRepository(connection).create(
        school_id=user.school_id,
        workspace_id=row["workspace_id"],
        job_type="PARSE",
        payload={"source_document_ids": [source_id], "request_id": request_id},
        progress_total=1,
    )
    result = {"job_id": job.id, "status": job.status}
    record_audit_event(
        connection,
        actor_id=user.id,
        school_id=user.school_id,
        action="SOURCE_PARSE_QUEUED",
        entity_type="durable_job",
        entity_id=job.id,
        before=None,
        after={"source_id": source_id, "status": job.status},
        request_id=request_id,
    )
    complete_idempotent_request(
        connection,
        school_id=user.school_id,
        actor_id=user.id,
        route=route,
        key=idempotency_key,
        status=202,
        body=result,
    )
    connection.commit()
    return result


@router.post(
    "/sources/{source_id}/catalog/staging",
    status_code=201,
    summary="장서 후보 버전 검증하기",
    operation_id="stageCatalogSnapshot",
)
def stage_catalog_snapshot(
    source_id: str,
    payload: CatalogStageRequest,
    user: AuthenticatedUser = Depends(require_role("OPERATOR")),
    connection: sqlite3.Connection = Depends(database_connection),
    idempotency_key: str = Depends(require_idempotency_key),
    request_id: str = Depends(require_request_id),
):
    route = f"POST /api/v2/sources/{source_id}/catalog/staging"
    replay = reserve_idempotency_key(
        connection,
        school_id=user.school_id,
        actor_id=user.id,
        route=route,
        key=idempotency_key,
        request_body=payload.model_dump(),
    )
    if replay:
        connection.rollback()
        return replay.body
    source = _source_row(connection, user.school_id, source_id)
    if source is None:
        raise domain_not_found("SOURCE_NOT_FOUND")
    records = _catalog_records_for_source(connection, source_id)
    try:
        staged = CatalogSyncService(connection).stage_full_snapshot(
            school_id=user.school_id,
            source_type=payload.source_type,
            source_document_id=source_id,
            records=records,
        )
    except (CatalogValidationError, ValueError) as error:
        raise HTTPException(
            status_code=422,
            detail={"code": "CATALOG_VALIDATION_FAILED", "message": str(error)},
        ) from error
    result = _catalog_version(staged)
    record_audit_event(
        connection,
        actor_id=user.id,
        school_id=user.school_id,
        action="CATALOG_STAGING_CREATED",
        entity_type="catalog_version",
        entity_id=staged.id,
        before=None,
        after=result,
        request_id=request_id,
    )
    complete_idempotent_request(
        connection,
        school_id=user.school_id,
        actor_id=user.id,
        route=route,
        key=idempotency_key,
        status=201,
        body=result,
    )
    connection.commit()
    return result


@router.post(
    "/catalog/versions/{version_id}/activate",
    summary="검증된 장서 버전 활성화하기",
    operation_id="activateCatalogVersion",
)
def activate_catalog_version(
    version_id: str,
    payload: CatalogActivateRequest,
    user: AuthenticatedUser = Depends(require_role("OPERATOR")),
    connection: sqlite3.Connection = Depends(database_connection),
    idempotency_key: str = Depends(require_idempotency_key),
    request_id: str = Depends(require_request_id),
):
    route = f"POST /api/v2/catalog/versions/{version_id}/activate"
    replay = reserve_idempotency_key(
        connection,
        school_id=user.school_id,
        actor_id=user.id,
        route=route,
        key=idempotency_key,
        request_body=payload.model_dump(),
    )
    if replay:
        connection.rollback()
        return replay.body
    current = connection.execute(
        "SELECT * FROM catalog_versions WHERE id = ? AND school_id = ?",
        (version_id, user.school_id),
    ).fetchone()
    if current is None:
        raise domain_not_found("CATALOG_VERSION_NOT_FOUND")
    try:
        activated = CatalogSyncService(connection).activate_staged(
            version_id, confirm_anomaly=payload.confirm_anomaly
        )
    except ActivationConfirmationRequired as error:
        raise HTTPException(
            status_code=409,
            detail={"code": "CATALOG_ACTIVATION_CONFIRMATION_REQUIRED"},
        ) from error
    except CatalogValidationError as error:
        raise HTTPException(
            status_code=409,
            detail={"code": "CATALOG_ACTIVATION_FAILED", "message": str(error)},
        ) from error
    result = _catalog_version(activated)
    record_audit_event(
        connection,
        actor_id=user.id,
        school_id=user.school_id,
        action="CATALOG_VERSION_ACTIVATED",
        entity_type="catalog_version",
        entity_id=version_id,
        before=dict(current),
        after=result,
        request_id=request_id,
    )
    complete_idempotent_request(
        connection,
        school_id=user.school_id,
        actor_id=user.id,
        route=route,
        key=idempotency_key,
        status=200,
        body=result,
    )
    connection.commit()
    return result


@router.post(
    "/catalog/deltas",
    summary="DLS 장서 증분 두 파일 적용하기",
    operation_id="applyCatalogDelta",
)
def apply_catalog_delta(
    payload: CatalogDeltaApplyRequest,
    user: AuthenticatedUser = Depends(require_role("OPERATOR")),
    connection: sqlite3.Connection = Depends(database_connection),
    idempotency_key: str = Depends(require_idempotency_key),
    request_id: str = Depends(require_request_id),
):
    if payload.requested_start_local_date > payload.requested_through_local_date:
        raise HTTPException(status_code=422, detail={"code": "INVALID_DELTA_WINDOW"})
    if payload.registration_source_id == payload.update_source_id:
        raise HTTPException(
            status_code=422, detail={"code": "DELTA_SOURCES_MUST_BE_DISTINCT"}
        )
    route = "POST /api/v2/catalog/deltas"
    request_body = payload.model_dump(mode="json")
    replay = reserve_idempotency_key(
        connection,
        school_id=user.school_id,
        actor_id=user.id,
        route=route,
        key=idempotency_key,
        request_body=request_body,
    )
    if replay:
        connection.rollback()
        return replay.body
    window = DeltaWindow(
        payload.requested_start_local_date,
        payload.requested_through_local_date,
    )
    before = connection.execute(
        """
        SELECT active_version_id, watermark_local_date
        FROM catalog_source_state WHERE school_id = ?
        """,
        (user.school_id,),
    ).fetchone()
    try:
        registration = _delta_file_for_source(
            connection,
            school_id=user.school_id,
            source_id=payload.registration_source_id,
            expected_role=DocumentRole.CATALOG_DELTA_REGISTRATION,
            window=window,
        )
        update = _delta_file_for_source(
            connection,
            school_id=user.school_id,
            source_id=payload.update_source_id,
            expected_role=DocumentRole.CATALOG_DELTA_UPDATE,
            window=window,
        )
        applied = CatalogSyncService(connection).apply_delta(
            school_id=user.school_id,
            source_type=payload.source_type,
            registration_file=registration,
            update_file=update,
            through_date=payload.requested_through_local_date,
        )
    except FullSnapshotRequired as error:
        raise HTTPException(
            status_code=409,
            detail={"code": "FULL_CATALOG_SNAPSHOT_REQUIRED", "message": str(error)},
        ) from error
    except (CatalogValidationError, SourcePolicyError, ValueError) as error:
        raise HTTPException(
            status_code=422,
            detail={"code": "CATALOG_DELTA_VALIDATION_FAILED", "message": str(error)},
        ) from error
    result = {
        "applied": applied.applied,
        "status": applied.status,
        "catalog_version_id": applied.catalog_version_id,
        "watermark_local_date": (
            applied.watermark_local_date.isoformat()
            if applied.watermark_local_date
            else None
        ),
        "idempotent": applied.idempotent,
    }
    record_audit_event(
        connection,
        actor_id=user.id,
        school_id=user.school_id,
        action="CATALOG_DELTA_APPLIED",
        entity_type="catalog_version",
        entity_id=applied.catalog_version_id,
        before=dict(before) if before else None,
        after=result,
        request_id=request_id,
    )
    complete_idempotent_request(
        connection,
        school_id=user.school_id,
        actor_id=user.id,
        route=route,
        key=idempotency_key,
        status=200,
        body=result,
    )
    connection.commit()
    return result


def _job(connection, school_id: str, job_id: str):
    job = JobRepository(connection).get(job_id)
    return job if job is not None and job.school_id == school_id else None


def _public_job_error(
    error: object, *, fallback_code: str = "JOB_FAILED"
) -> dict[str, str] | None:
    return sanitize_public_job_error(error, fallback_code=fallback_code)


def _public_job_file_result(
    item: dict[str, Any], *, fallback_code: str
) -> dict[str, Any]:
    raw_error = item.get("error")
    raw_mapping = sanitize_public_mapping_required(
        raw_error,
        item.get("mapping_required"),
        item.get("_public_mapping_payload_version"),
    )
    mapping = None
    if raw_mapping is not None:
        try:
            mapping = MappingRequired.model_validate(raw_mapping).model_dump(
                mode="json"
            )
        except ValidationError:
            mapping = None
    public_item = {
        key: value
        for key, value in item.items()
        if key != "_public_mapping_payload_version"
    }
    return {
        **public_item,
        "error": _public_job_error(raw_error, fallback_code=fallback_code),
        "mapping_required": mapping,
    }


def _job_json(job) -> dict[str, Any]:
    return {
        "id": job.id,
        "workspace_id": job.workspace_id,
        "type": job.job_type,
        "status": job.status,
        "stage": job.stage,
        "progress_current": job.progress_current,
        "progress_total": job.progress_total,
        "error": _public_job_error(job.error),
        "retry_count": job.retry_count,
    }


def _job_with_items(connection: sqlite3.Connection, job) -> dict[str, Any]:
    result = _job_json(job)
    items = JobRepository(connection).file_results(job.id)
    fallback_code = (
        "COMPARISON_FILE_FAILED" if job.job_type == "COMPARE" else "PARSER_FAILURE"
    )
    items = [
        _public_job_file_result(item, fallback_code=fallback_code) for item in items
    ]
    result["items"] = items
    return result


@router.get(
    "/workspaces/{workspace_id}/jobs",
    summary="수서 작업의 자료 처리 이력 보기",
    operation_id="listWorkspaceJobs",
)
def list_workspace_jobs(
    workspace_id: str,
    user: AuthenticatedUser = Depends(current_user),
    connection: sqlite3.Connection = Depends(database_connection),
    job_type: str | None = Query(default=None, alias="type"),
    status: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
):
    if not _workspace_exists(connection, user.school_id, workspace_id):
        raise domain_not_found("WORKSPACE_NOT_FOUND")
    decoded = decode_cursor(cursor, 2)
    clauses = ["school_id = ?", "workspace_id = ?"]
    parameters: list[object] = [user.school_id, workspace_id]
    if job_type:
        clauses.append("job_type = ?")
        parameters.append(job_type)
    if status:
        clauses.append("status = ?")
        parameters.append(status)
    if decoded:
        clauses.append("(created_at < ? OR (created_at = ? AND id < ?))")
        parameters.extend((decoded[0], decoded[0], decoded[1]))
    rows = connection.execute(
        f"""
        SELECT * FROM durable_jobs WHERE {" AND ".join(clauses)}
        ORDER BY created_at DESC, id DESC LIMIT ?
        """,
        (*parameters, limit + 1),
    ).fetchall()
    repository = JobRepository(connection)
    jobs = [repository.get(row["id"]) for row in rows]
    created_at_by_id = {row["id"]: row["created_at"] for row in rows}
    items = [_job_with_items(connection, job) for job in jobs if job is not None]
    return page(
        items,
        limit=limit,
        cursor_values=lambda item: (created_at_by_id[item["id"]], item["id"]),
    )


@router.get("/jobs/{job_id}", summary="자료 처리 진행률 보기", operation_id="getJob")
def get_job(
    job_id: str,
    user: AuthenticatedUser = Depends(current_user),
    connection: sqlite3.Connection = Depends(database_connection),
):
    job = _job(connection, user.school_id, job_id)
    if job is None:
        raise domain_not_found("JOB_NOT_FOUND")
    return _job_with_items(connection, job)


def _refresh_compare_payload_for_retry(
    connection: sqlite3.Connection,
    *,
    job,
) -> None:
    """Take a fresh fenced comparison snapshot for an explicit user retry.

    Old queued comparisons had no snapshot, and a valid versioned comparison
    can also fail after the active catalog rotates.  A user retry is a new,
    auditable action, so it takes one fresh immutable snapshot while the
    workspace is still ANALYZING and source mutation is blocked.
    """
    if job.job_type != "COMPARE":
        return
    if job.workspace_id is None:
        raise HTTPException(status_code=409, detail={"code": "WORKSPACE_NOT_FOUND"})
    workspace = connection.execute(
        """
        SELECT status FROM acquisition_workspaces
        WHERE id = ? AND school_id = ?
        """,
        (job.workspace_id, job.school_id),
    ).fetchone()
    if workspace is None:
        raise domain_not_found("WORKSPACE_NOT_FOUND")
    if workspace["status"] != "ANALYZING":
        raise HTTPException(
            status_code=409, detail={"code": "SOURCE_COMPARISON_NOT_ACTIVE"}
        )
    unresolved_repairs = connection.execute(
        """
        SELECT 1 FROM upload_repair_obligations
        WHERE school_id = ? AND workspace_id = ? AND status != 'RESOLVED'
        LIMIT 1
        """,
        (job.school_id, job.workspace_id),
    ).fetchone()
    if unresolved_repairs is not None:
        raise HTTPException(status_code=409, detail={"code": "UPLOAD_REPAIR_REQUIRED"})

    source_snapshot = authoritative_comparison_sources(
        connection, school_id=job.school_id, workspace_id=job.workspace_id
    )
    if not source_snapshot or any(
        item["status"] not in {"SUCCESS", "ROW_ERROR"}
        or item["parsed_config_version"] != item["config_version"]
        for item in source_snapshot
    ):
        raise HTTPException(
            status_code=422, detail={"code": "COMPARISON_SOURCES_NOT_READY"}
        )
    document_ids = tuple(item["id"] for item in source_snapshot)
    source_ids_json = json.dumps(document_ids)
    active_processing = connection.execute(
        """
        SELECT 1
        FROM durable_jobs AS processing,
             json_each(processing.payload_json, '$.source_document_ids') AS source
        WHERE processing.school_id = ?
          AND processing.job_type IN ('INGEST', 'PARSE')
          AND processing.status IN ('QUEUED', 'RUNNING', 'CANCEL_REQUESTED')
          AND source.value IN (SELECT value FROM json_each(?))
        LIMIT 1
        """,
        (job.school_id, source_ids_json),
    ).fetchone()
    if active_processing is not None:
        raise HTTPException(
            status_code=409, detail={"code": "SOURCE_PROCESSING_IN_PROGRESS"}
        )
    active_catalog = connection.execute(
        """
        SELECT id FROM catalog_versions
        WHERE school_id = ? AND status = 'ACTIVE'
        """,
        (job.school_id,),
    ).fetchone()
    if active_catalog is None:
        raise HTTPException(status_code=409, detail={"code": "ACTIVE_CATALOG_REQUIRED"})

    upgraded = {
        "source_document_ids": list(document_ids),
        "catalog_version_id": active_catalog["id"],
        "source_snapshot_version": 1,
        "source_snapshot": source_snapshot,
    }
    immutable_downstream = connection.execute(
        """
        SELECT 1
        FROM approval_rows AS approval
        JOIN candidate_decisions AS candidate ON candidate.id = approval.candidate_id
        JOIN recommendations AS recommendation
          ON recommendation.id = candidate.recommendation_id
        WHERE approval.workspace_id = ?
          AND recommendation.source_document_id IN (
              SELECT value FROM json_each(?)
          )
        LIMIT 1
        """,
        (job.workspace_id, source_ids_json),
    ).fetchone()
    if immutable_downstream is not None:
        raise HTTPException(
            status_code=409, detail={"code": "SOURCE_COMPARISON_NOT_ACTIVE"}
        )

    # A pre-snapshot worker may have persisted some rows against an older
    # catalog before it failed.  The fresh snapshot must never reuse that
    # unfenced output or mix catalog generations.
    connection.execute(
        """
        DELETE FROM edit_locks
        WHERE school_id = ? AND entity_type = 'candidate_decision'
          AND entity_id IN (
              SELECT candidate.id
              FROM candidate_decisions AS candidate
              JOIN recommendations AS recommendation
                ON recommendation.id = candidate.recommendation_id
              WHERE candidate.workspace_id = ?
                AND recommendation.source_document_id IN (
                    SELECT value FROM json_each(?)
                )
          )
        """,
        (job.school_id, job.workspace_id, source_ids_json),
    )
    connection.execute(
        """
        DELETE FROM comparison_row_results
        WHERE workspace_id = ? AND source_document_id IN (
            SELECT value FROM json_each(?)
        )
        """,
        (job.workspace_id, source_ids_json),
    )
    connection.execute(
        """
        DELETE FROM candidate_decisions
        WHERE workspace_id = ? AND recommendation_id IN (
            SELECT id FROM recommendations
            WHERE workspace_id = ? AND source_document_id IN (
                SELECT value FROM json_each(?)
            )
        )
        """,
        (job.workspace_id, job.workspace_id, source_ids_json),
    )
    connection.execute(
        """
        DELETE FROM recommendations
        WHERE workspace_id = ? AND source_document_id IN (
            SELECT value FROM json_each(?)
        )
        """,
        (job.workspace_id, source_ids_json),
    )
    connection.execute(
        """
        DELETE FROM comparison_file_results
        WHERE workspace_id = ? AND source_document_id IN (
            SELECT value FROM json_each(?)
        )
        """,
        (job.workspace_id, source_ids_json),
    )
    connection.execute("DELETE FROM job_file_results WHERE job_id = ?", (job.id,))
    connection.execute(
        """
        UPDATE durable_jobs
        SET payload_json = ?, progress_current = 0, progress_total = ?
        WHERE id = ? AND status IN ('FAILED', 'PARTIAL', 'CANCELLED')
        """,
        (
            json.dumps(upgraded, ensure_ascii=False, sort_keys=True),
            sum(item["row_count"] for item in source_snapshot),
            job.id,
        ),
    )


def _validate_source_processing_retry(connection: sqlite3.Connection, *, job) -> None:
    """Fence source-processing retries to the mutable workspace generation."""
    if job.job_type not in {"INGEST", "PARSE"}:
        return
    if job.workspace_id is None:
        raise HTTPException(
            status_code=422, detail={"code": "COMPARISON_SOURCES_NOT_READY"}
        )
    raw_document_ids = job.payload.get("source_document_ids")
    if not isinstance(raw_document_ids, list) or not raw_document_ids:
        raise HTTPException(
            status_code=422, detail={"code": "COMPARISON_SOURCES_NOT_READY"}
        )
    document_ids = tuple(
        dict.fromkeys(item for item in raw_document_ids if isinstance(item, str))
    )
    if len(document_ids) != len(raw_document_ids):
        raise HTTPException(
            status_code=422, detail={"code": "COMPARISON_SOURCES_NOT_READY"}
        )
    placeholders = ",".join("?" for _ in document_ids)
    linked_count = connection.execute(
        f"""
        SELECT COUNT(*) AS count
        FROM workspace_sources
        WHERE workspace_id = ? AND school_id = ?
          AND source_document_id IN ({placeholders})
        """,
        (job.workspace_id, job.school_id, *document_ids),
    ).fetchone()["count"]
    if linked_count != len(document_ids):
        raise HTTPException(
            status_code=422, detail={"code": "COMPARISON_SOURCES_NOT_READY"}
        )
    comparison_fenced = connection.execute(
        f"""
        SELECT 1
        FROM source_documents document
        JOIN workspace_sources link ON link.source_document_id = document.id
        JOIN acquisition_workspaces workspace ON workspace.id = link.workspace_id
        LEFT JOIN source_configurations config
          ON config.source_document_id = document.id
        WHERE document.school_id = ?
          AND document.id IN ({placeholders})
          AND COALESCE(config.role, document.role) = 'PURCHASE_REQUEST'
          AND workspace.status != 'DRAFT'
        LIMIT 1
        """,
        (job.school_id, *document_ids),
    ).fetchone()
    if comparison_fenced is not None:
        raise HTTPException(
            status_code=409, detail={"code": "SOURCE_COMPARISON_IN_PROGRESS"}
        )


@router.post(
    "/jobs/{job_id}/retry",
    status_code=202,
    summary="실패한 자료 처리 다시 시도하기",
    operation_id="retryJob",
)
def retry_job(
    job_id: str,
    user: AuthenticatedUser = Depends(require_role("OPERATOR")),
    connection: sqlite3.Connection = Depends(database_connection),
    idempotency_key: str = Depends(require_idempotency_key),
    request_id: str = Depends(require_request_id),
):
    route = f"POST /api/v2/jobs/{job_id}/retry"
    replay = reserve_idempotency_key(
        connection,
        school_id=user.school_id,
        actor_id=user.id,
        route=route,
        key=idempotency_key,
        request_body={"job_id": job_id},
    )
    if replay is not None:
        connection.rollback()
        return replay.body
    job = _job(connection, user.school_id, job_id)
    if job is None:
        connection.rollback()
        raise domain_not_found("JOB_NOT_FOUND")
    if job.status in {"FAILED", "PARTIAL", "CANCELLED"}:
        _validate_source_processing_retry(connection, job=job)
        _refresh_compare_payload_for_retry(connection, job=job)
    result = JobRepository(connection).retry(job_id)
    body = _job_json(result)
    record_audit_event(
        connection,
        actor_id=user.id,
        school_id=user.school_id,
        action="JOB_RETRIED",
        entity_type="durable_job",
        entity_id=job_id,
        before=_job_json(job),
        after=body,
        request_id=request_id,
    )
    complete_idempotent_request(
        connection,
        school_id=user.school_id,
        actor_id=user.id,
        route=route,
        key=idempotency_key,
        status=202,
        body=body,
    )
    connection.commit()
    return body


@router.post(
    "/jobs/{job_id}/cancel", summary="자료 처리 취소 요청하기", operation_id="cancelJob"
)
def cancel_job(
    job_id: str,
    user: AuthenticatedUser = Depends(require_role("OPERATOR")),
    connection: sqlite3.Connection = Depends(database_connection),
    idempotency_key: str = Depends(require_idempotency_key),
    request_id: str = Depends(require_request_id),
):
    job = _job(connection, user.school_id, job_id)
    if job is None:
        raise domain_not_found("JOB_NOT_FOUND")
    route = f"POST /api/v2/jobs/{job_id}/cancel"
    replay = reserve_idempotency_key(
        connection,
        school_id=user.school_id,
        actor_id=user.id,
        route=route,
        key=idempotency_key,
        request_body={"job_id": job_id},
    )
    if replay is not None:
        connection.rollback()
        return replay.body
    result = JobRepository(connection).request_cancel(job_id)
    body = _job_json(result)
    record_audit_event(
        connection,
        actor_id=user.id,
        school_id=user.school_id,
        action="JOB_CANCEL_REQUESTED",
        entity_type="durable_job",
        entity_id=job_id,
        before=_job_json(job),
        after=body,
        request_id=request_id,
    )
    complete_idempotent_request(
        connection,
        school_id=user.school_id,
        actor_id=user.id,
        route=route,
        key=idempotency_key,
        status=200,
        body=body,
    )
    connection.commit()
    return body
