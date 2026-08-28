"""Streaming source uploads and durable ingestion job routes."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import date
from pathlib import Path
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
from pydantic import BaseModel, Field

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
from suseoro.api.errors import domain_not_found
from suseoro.api.routes.events import publish_event
from suseoro.api.schemas import ApiErrorResponse, UploadResponse
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
    UnsupportedFileType,
)
from suseoro.jobs.handlers import parser_version_for_format
from suseoro.jobs.repository import JobRepository
from suseoro.security.sessions import format_utc, utc_now
from suseoro.services.audit import record_audit_event
from suseoro.services.idempotency import (
    complete_idempotent_request,
    reserve_idempotency_key,
)

router = APIRouter(prefix="/api/v2", tags=["sources"])


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


def _source_row(connection, school_id: str, source_id: str):
    return connection.execute(
        """
        SELECT document.*, file.sha256, file.size_bytes, file.original_filename,
               COALESCE(config.role, document.role) AS configured_role,
               COALESCE(config.row_version, 1) AS row_version,
               COALESCE(config.mapping_json, '{}') AS mapping_json
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
        "row_version": row["row_version"],
        "created_at": row["created_at"],
        "completed_at": row["completed_at"],
    }


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
    vendor_scope: str = Form("*"),
    requested_start_local_date: str | None = Form(None),
    requested_through_local_date: str | None = Form(None),
    user: AuthenticatedUser = Depends(require_role("OPERATOR")),
    connection: sqlite3.Connection = Depends(database_connection),
    idempotency_key: str = Depends(require_idempotency_key),
    request_id: str = Depends(require_request_id),
):
    if not _workspace_exists(connection, user.school_id, workspace_id):
        raise domain_not_found("WORKSPACE_NOT_FOUND")
    settings = request.app.state.settings
    if len(files) > settings.upload_max_files:
        raise HTTPException(
            status_code=413, detail={"code": "UPLOAD_BATCH_LIMIT_EXCEEDED"}
        )
    delta_roles = {
        DocumentRole.CATALOG_DELTA_REGISTRATION,
        DocumentRole.CATALOG_DELTA_UPDATE,
    }
    requested_start = requested_through = None
    if role in delta_roles:
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
    elif requested_start_local_date or requested_through_local_date:
        raise HTTPException(status_code=422, detail={"code": "INVALID_DELTA_WINDOW"})
    store = ImmutableFileStore(
        settings.sources_dir, max_bytes=settings.upload_max_file_bytes
    )
    now = format_utc(utc_now())
    items: list[dict[str, Any]] = []
    accepted_ids: list[str] = []
    fingerprints: list[dict[str, Any]] = []
    newly_published: list[Path] = []
    aggregate_bytes = 0

    def counted_chunks(upload: UploadFile):
        nonlocal aggregate_bytes
        while True:
            remaining = settings.upload_max_batch_bytes - aggregate_bytes
            chunk = upload.file.read(min(64 * 1024, max(1, remaining + 1)))
            if not chunk:
                return
            aggregate_bytes += len(chunk)
            if aggregate_bytes > settings.upload_max_batch_bytes:
                raise HTTPException(
                    status_code=413,
                    detail={"code": "UPLOAD_BATCH_LIMIT_EXCEEDED"},
                )
            yield chunk

    for upload in files:
        filename = upload.filename or "unnamed"
        try:
            stored = store.store(
                counted_chunks(upload),
                filename=filename,
            )
            if stored.created:
                newly_published.append(stored.path)
            fingerprints.append({"filename": filename, "sha256": stored.sha256})
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
                        filename,
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
                    vendor_scope, updated_at
                ) VALUES (?, ?, ?, '{}', ?, ?)
                """,
                (source_id, user.school_id, role, vendor_scope.strip() or "*", now),
            )
            accepted_ids.append(source_id)
            items.append(
                {
                    "filename": filename,
                    "status": "ACCEPTED",
                    "source_id": source_id,
                    "error": None,
                }
            )
        except HTTPException:
            connection.rollback()
            for path in newly_published:
                path.unlink(missing_ok=True)
            raise
        except (FileTooLarge, UnsupportedFileType, ValueError) as error:
            if isinstance(error, FileTooLarge):
                try:
                    for _ in counted_chunks(upload):
                        pass
                except HTTPException:
                    connection.rollback()
                    for path in newly_published:
                        path.unlink(missing_ok=True)
                    raise
            code = (
                "FILE_TOO_LARGE"
                if isinstance(error, FileTooLarge)
                else "UNSUPPORTED_FILE_TYPE"
            )
            fingerprints.append({"filename": filename, "error": code})
            items.append(
                {
                    "filename": filename,
                    "status": "FAILED",
                    "source_id": None,
                    "error": {"code": code, "message": str(error)},
                }
            )
        finally:
            upload.file.close()
    request_body = {
        "workspace_id": workspace_id,
        "role": role,
        "vendor_scope": vendor_scope.strip() or "*",
        "requested_start_local_date": requested_start_local_date,
        "requested_through_local_date": requested_through_local_date,
        "files": fingerprints,
    }
    replay = reserve_idempotency_key(
        connection,
        school_id=user.school_id,
        actor_id=user.id,
        route=f"POST /api/v2/workspaces/{workspace_id}/sources",
        key=idempotency_key,
        request_body=request_body,
    )
    if replay is not None:
        connection.rollback()
        for path in newly_published:
            with connect(request.app.state.settings.database_path) as check:
                exists = check.execute(
                    "SELECT 1 FROM source_files WHERE storage_path = ?", (str(path),)
                ).fetchone()
            if exists is None:
                path.unlink(missing_ok=True)
        response.status_code = replay.status
        return replay.body
    job = None
    if accepted_ids:
        job = JobRepository(connection).create(
            school_id=user.school_id,
            workspace_id=workspace_id,
            job_type="INGEST",
            payload={"source_document_ids": accepted_ids, "role": role},
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
        after={"accepted": len(accepted_ids), "failed": len(items) - len(accepted_ids)},
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
        route=f"POST /api/v2/workspaces/{workspace_id}/sources",
        key=idempotency_key,
        status=status_code,
        body=result,
    )
    connection.commit()
    response.status_code = status_code
    return result


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
               COALESCE(config.mapping_json, '{{}}') AS mapping_json
        FROM workspace_sources link
        JOIN source_documents document ON document.id = link.source_document_id
        JOIN source_files file ON file.id = document.source_file_id
        LEFT JOIN source_configurations config ON config.source_document_id = document.id
        WHERE {" AND ".join(clauses)}
        ORDER BY link.created_at DESC, document.id DESC LIMIT ?
        """,
        (*parameters, limit + 1),
    ).fetchall()
    items = [_source(row) for row in rows]
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
    return _source(row)


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
    row = connection.execute(
        """
        SELECT link.workspace_id FROM source_documents document
        JOIN workspace_sources link ON link.source_document_id = document.id
        WHERE document.id = ? AND document.school_id = ?
        """,
        (source_id, user.school_id),
    ).fetchone()
    if row is None:
        raise domain_not_found("SOURCE_NOT_FOUND")
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
    job = JobRepository(connection).create(
        school_id=user.school_id,
        workspace_id=row["workspace_id"],
        job_type="PARSE",
        payload={"source_document_ids": [source_id]},
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


def _job_json(job) -> dict[str, Any]:
    return {
        "id": job.id,
        "workspace_id": job.workspace_id,
        "type": job.job_type,
        "status": job.status,
        "stage": job.stage,
        "progress_current": job.progress_current,
        "progress_total": job.progress_total,
        "error": job.error,
        "retry_count": job.retry_count,
    }


@router.get("/jobs/{job_id}", summary="자료 처리 진행률 보기", operation_id="getJob")
def get_job(
    job_id: str,
    user: AuthenticatedUser = Depends(current_user),
    connection: sqlite3.Connection = Depends(database_connection),
):
    job = _job(connection, user.school_id, job_id)
    if job is None:
        raise domain_not_found("JOB_NOT_FOUND")
    result = _job_json(job)
    result["items"] = JobRepository(connection).file_results(job.id)
    return result


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
    job = _job(connection, user.school_id, job_id)
    if job is None:
        raise domain_not_found("JOB_NOT_FOUND")
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
