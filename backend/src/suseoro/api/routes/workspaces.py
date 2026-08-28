"""Acquisition workspace HTTP boundaries."""

from __future__ import annotations

import sqlite3
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
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
from suseoro.jobs.repository import JobRepository
from suseoro.jobs.source_snapshot import source_rows_snapshot
from suseoro.security.sessions import format_utc, utc_now
from suseoro.services.audit import record_audit_event
from suseoro.services.idempotency import (
    complete_idempotent_request,
    reserve_idempotency_key,
)
from suseoro.workflow.states import transition_workspace

router = APIRouter(prefix="/api/v2", tags=["workspaces"])


class WorkspaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class WorkspaceTransition(BaseModel):
    target: str
    reason: str = Field(min_length=1, max_length=500)


class ComparisonCreate(BaseModel):
    source_document_ids: Annotated[list[str], Field(min_length=1, max_length=100)]


def _serialized(row) -> dict[str, object]:
    return {
        "id": row["id"],
        "name": row["name"],
        "status": row["status"],
        "row_version": row["row_version"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


@router.post(
    "/workspaces",
    status_code=201,
    summary="수서 작업 만들기",
    operation_id="createWorkspace",
)
def create_workspace(
    payload: WorkspaceCreate,
    response: Response,
    user: AuthenticatedUser = Depends(require_role("OPERATOR")),
    connection: sqlite3.Connection = Depends(database_connection),
    idempotency_key: str = Depends(require_idempotency_key),
    request_id: str = Depends(require_request_id),
):
    body = payload.model_dump()
    replay = reserve_idempotency_key(
        connection,
        school_id=user.school_id,
        actor_id=user.id,
        route="POST /api/v2/workspaces",
        key=idempotency_key,
        request_body=body,
    )
    if replay is not None:
        connection.rollback()
        response.headers["ETag"] = f'"{replay.body["row_version"]}"'
        return replay.body
    name = payload.name.strip()
    if not name:
        from fastapi import HTTPException

        raise HTTPException(status_code=422, detail={"code": "VALIDATION_ERROR"})
    now = format_utc(utc_now())
    workspace_id = str(uuid.uuid4())
    connection.execute(
        """
        INSERT INTO acquisition_workspaces (
            id, school_id, name, status, created_by_user_id, created_at, updated_at
        ) VALUES (?, ?, ?, 'DRAFT', ?, ?, ?)
        """,
        (workspace_id, user.school_id, name, user.id, now, now),
    )
    row = connection.execute(
        "SELECT * FROM acquisition_workspaces WHERE id = ?", (workspace_id,)
    ).fetchone()
    result = _serialized(row)
    record_audit_event(
        connection,
        actor_id=user.id,
        school_id=user.school_id,
        action="WORKSPACE_CREATED",
        entity_type="acquisition_workspace",
        entity_id=workspace_id,
        before=None,
        after=result,
        request_id=request_id,
    )
    complete_idempotent_request(
        connection,
        school_id=user.school_id,
        actor_id=user.id,
        route="POST /api/v2/workspaces",
        key=idempotency_key,
        status=201,
        body=result,
    )
    connection.commit()
    response.headers["ETag"] = '"1"'
    return result


@router.get("/workspaces", summary="수서 작업 목록 보기", operation_id="listWorkspaces")
def list_workspaces(
    user: AuthenticatedUser = Depends(current_user),
    connection: sqlite3.Connection = Depends(database_connection),
    status: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
):
    decoded = decode_cursor(cursor, 2)
    conditions = ["school_id = ?"]
    parameters: list[object] = [user.school_id]
    if status:
        conditions.append("status = ?")
        parameters.append(status)
    if decoded:
        conditions.append("(created_at < ? OR (created_at = ? AND id < ?))")
        parameters.extend((decoded[0], decoded[0], decoded[1]))
    rows = connection.execute(
        f"""
        SELECT * FROM acquisition_workspaces
        WHERE {" AND ".join(conditions)}
        ORDER BY created_at DESC, id DESC LIMIT ?
        """,
        (*parameters, limit + 1),
    ).fetchall()
    items = [_serialized(row) for row in rows]
    return page(
        items, limit=limit, cursor_values=lambda item: (item["created_at"], item["id"])
    )


@router.get(
    "/workspaces/{workspace_id}",
    summary="수서 작업 자세히 보기",
    operation_id="getWorkspace",
)
def get_workspace(
    workspace_id: str,
    response: Response,
    user: AuthenticatedUser = Depends(current_user),
    connection: sqlite3.Connection = Depends(database_connection),
):
    row = connection.execute(
        "SELECT * FROM acquisition_workspaces WHERE id = ? AND school_id = ?",
        (workspace_id, user.school_id),
    ).fetchone()
    if row is None:
        raise domain_not_found("WORKSPACE_NOT_FOUND")
    response.headers["ETag"] = f'"{row["row_version"]}"'
    return _serialized(row)


@router.patch(
    "/workspaces/{workspace_id}/status",
    summary="수서 작업 단계 바꾸기",
    operation_id="transitionWorkspace",
)
def transition(
    workspace_id: str,
    payload: WorkspaceTransition,
    response: Response,
    user: AuthenticatedUser = Depends(current_user),
    connection: sqlite3.Connection = Depends(database_connection),
    submitted_version: int = Depends(require_if_match),
    idempotency_key: str = Depends(require_idempotency_key),
    request_id: str = Depends(require_request_id),
):
    result = transition_workspace(
        connection,
        school_id=user.school_id,
        workspace_id=workspace_id,
        actor_id=user.id,
        actor_roles=user.roles,
        target=payload.target,
        submitted_version=submitted_version,
        idempotency_key=idempotency_key,
        request_id=request_id,
        reason=payload.reason,
    )
    publish_event(
        connection,
        school_id=user.school_id,
        workspace_id=workspace_id,
        event_type="workspace.updated",
        data=result,
        deduplication_key=(
            f"{user.id}:PATCH:/workspaces/{workspace_id}/status:{idempotency_key}"
        ),
    )
    connection.commit()
    response.headers["ETag"] = f'"{result["row_version"]}"'
    return result


@router.post(
    "/workspaces/{workspace_id}/comparison-jobs",
    status_code=202,
    summary="장서와 추천 자료 비교 시작하기",
    operation_id="createComparisonJob",
)
def create_comparison_job(
    workspace_id: str,
    payload: ComparisonCreate,
    response: Response,
    user: AuthenticatedUser = Depends(require_role("OPERATOR")),
    connection: sqlite3.Connection = Depends(database_connection),
    submitted_version: int = Depends(require_if_match),
    idempotency_key: str = Depends(require_idempotency_key),
    request_id: str = Depends(require_request_id),
):
    route = f"POST /api/v2/workspaces/{workspace_id}/comparison-jobs"
    request_body = {
        **payload.model_dump(),
        "row_version": submitted_version,
    }
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
        response.headers["ETag"] = f'"{replay.body["row_version"]}"'
        return replay.body
    workspace = connection.execute(
        "SELECT * FROM acquisition_workspaces WHERE id = ? AND school_id = ?",
        (workspace_id, user.school_id),
    ).fetchone()
    if workspace is None:
        raise domain_not_found("WORKSPACE_NOT_FOUND")
    if workspace["status"] != "DRAFT":
        from fastapi import HTTPException

        raise HTTPException(status_code=409, detail={"code": "WORKSPACE_NOT_DRAFT"})
    if workspace["row_version"] != submitted_version:
        from suseoro.services.concurrency import VersionConflict

        raise VersionConflict(workspace["row_version"], submitted_version)
    active_catalog = connection.execute(
        "SELECT id FROM catalog_versions WHERE school_id = ? AND status = 'ACTIVE'",
        (user.school_id,),
    ).fetchone()
    if active_catalog is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=409, detail={"code": "ACTIVE_CATALOG_REQUIRED"})
    unresolved_repairs = connection.execute(
        """
        SELECT COUNT(*) FROM upload_repair_obligations
        WHERE school_id = ? AND workspace_id = ? AND status != 'RESOLVED'
        """,
        (user.school_id, workspace_id),
    ).fetchone()[0]
    if unresolved_repairs:
        from fastapi import HTTPException

        raise HTTPException(status_code=409, detail={"code": "UPLOAD_REPAIR_REQUIRED"})
    document_ids = tuple(dict.fromkeys(payload.source_document_ids))
    authoritative_documents = connection.execute(
        """
        SELECT document.id, document.status, document.parser_version,
               document.completed_at, file.sha256,
               COALESCE(config.role, document.role) AS role,
               COALESCE(config.row_version, 1) AS config_version,
               COALESCE(config.mapping_json, '{{}}') AS mapping_json
        FROM source_documents document
        JOIN source_files file ON file.id = document.source_file_id
        JOIN workspace_sources link ON link.source_document_id = document.id
        LEFT JOIN source_configurations config
          ON config.source_document_id = document.id
        WHERE link.workspace_id = ? AND link.school_id = ?
          AND COALESCE(config.role, document.role) = 'PURCHASE_REQUEST'
        """,
        (workspace_id, user.school_id),
    ).fetchall()
    documents_by_id = {row["id"]: row for row in authoritative_documents}
    if set(document_ids) != set(documents_by_id):
        from fastapi import HTTPException

        raise HTTPException(
            status_code=409, detail={"code": "COMPARISON_SOURCE_SET_CHANGED"}
        )
    documents = [documents_by_id[document_id] for document_id in document_ids]
    if any(row["status"] not in {"SUCCESS", "ROW_ERROR"} for row in documents):
        from fastapi import HTTPException

        raise HTTPException(
            status_code=422, detail={"code": "COMPARISON_SOURCES_NOT_READY"}
        )
    placeholders = ",".join("?" for _ in document_ids)
    active_processing = connection.execute(
        f"""
        SELECT 1
        FROM durable_jobs AS job, json_each(job.payload_json, '$.source_document_ids') AS source
        WHERE job.school_id = ? AND job.workspace_id = ?
          AND job.job_type IN ('INGEST', 'PARSE')
          AND job.status IN ('QUEUED', 'RUNNING', 'CANCEL_REQUESTED')
          AND source.value IN ({placeholders})
        LIMIT 1
        """,
        (user.school_id, workspace_id, *document_ids),
    ).fetchone()
    if active_processing is not None:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=409, detail={"code": "SOURCE_PROCESSING_IN_PROGRESS"}
        )
    source_snapshot = []
    for document in documents:
        row_count, row_digest = source_rows_snapshot(connection, document["id"])
        source_snapshot.append(
            {
                "id": document["id"],
                "role": document["role"],
                "status": document["status"],
                "sha256": document["sha256"],
                "config_version": document["config_version"],
                "mapping_json": document["mapping_json"],
                "parser_version": document["parser_version"],
                "completed_at": document["completed_at"],
                "row_count": row_count,
                "row_digest": row_digest,
            }
        )
    updated_at = format_utc(utc_now())
    updated = connection.execute(
        """
        UPDATE acquisition_workspaces
        SET status = 'ANALYZING', row_version = row_version + 1, updated_at = ?
        WHERE id = ? AND school_id = ? AND status = 'DRAFT' AND row_version = ?
        """,
        (updated_at, workspace_id, user.school_id, submitted_version),
    )
    if updated.rowcount != 1:
        from suseoro.services.concurrency import VersionConflict

        raise VersionConflict(workspace["row_version"], submitted_version)
    total = sum(int(item["row_count"]) for item in source_snapshot)
    job = JobRepository(connection).create(
        school_id=user.school_id,
        workspace_id=workspace_id,
        job_type="COMPARE",
        payload={
            "source_document_ids": list(document_ids),
            "catalog_version_id": active_catalog["id"],
            "source_snapshot_version": 1,
            "source_snapshot": source_snapshot,
        },
        progress_total=total,
    )
    result = {
        "job_id": job.id,
        "status": job.status,
        "workspace_status": "ANALYZING",
        "row_version": submitted_version + 1,
    }
    record_audit_event(
        connection,
        actor_id=user.id,
        school_id=user.school_id,
        action="COMPARISON_JOB_CREATED",
        entity_type="durable_job",
        entity_id=job.id,
        before={"workspace_status": "DRAFT", "row_version": submitted_version},
        after=result,
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
            "total": total,
        },
    )
    connection.commit()
    response.headers["ETag"] = f'"{submitted_version + 1}"'
    return result
