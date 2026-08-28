"""Streaming source uploads and durable ingestion job routes."""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from fastapi import APIRouter, Depends, File, Form, Query, Request, Response, UploadFile
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
    summary="원본 자료 올리기",
    operation_id="uploadSources",
)
def upload_sources(
    workspace_id: str,
    request: Request,
    response: Response,
    files: list[UploadFile] = File(...),
    role: DocumentRole = Form(DocumentRole.UNKNOWN),
    user: AuthenticatedUser = Depends(require_role("OPERATOR")),
    connection: sqlite3.Connection = Depends(database_connection),
    idempotency_key: str = Depends(require_idempotency_key),
    request_id: str = Depends(require_request_id),
):
    if not _workspace_exists(connection, user.school_id, workspace_id):
        raise domain_not_found("WORKSPACE_NOT_FOUND")
    store = ImmutableFileStore(request.app.state.settings.sources_dir)
    now = format_utc(utc_now())
    items: list[dict[str, Any]] = []
    accepted_ids: list[str] = []
    fingerprints: list[dict[str, Any]] = []
    for upload in files:
        filename = upload.filename or "unnamed"
        try:
            stored = store.store(
                iter(lambda upload=upload: upload.file.read(64 * 1024), b""),
                filename=filename,
            )
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
                    status, detected_format, created_at
                ) VALUES (?, ?, ?, ?, ?, 'PENDING', ?, ?)
                """,
                (
                    source_id,
                    source_file_id,
                    user.school_id,
                    role,
                    parser_version_for_format(stored.detected_format),
                    stored.detected_format,
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
                    source_document_id, school_id, role, mapping_json, updated_at
                ) VALUES (?, ?, ?, '{}', ?)
                """,
                (source_id, user.school_id, role, now),
            )
            accepted_ids.append(source_id)
            items.append(
                {"filename": filename, "status": "ACCEPTED", "source_id": source_id}
            )
        except (FileTooLarge, UnsupportedFileType, ValueError) as error:
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
                    "error": {"code": code, "message": str(error)},
                }
            )
        finally:
            upload.file.close()
    request_body = {"workspace_id": workspace_id, "role": role, "files": fingerprints}
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
            row_version = row_version + 1, updated_at = ?
        WHERE source_document_id = ? AND school_id = ? AND row_version = ?
        """,
        (
            payload.role,
            json.dumps(payload.mapping, ensure_ascii=False, sort_keys=True),
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
    return _job_json(job)


@router.post(
    "/jobs/{job_id}/retry",
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
        status=200,
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
