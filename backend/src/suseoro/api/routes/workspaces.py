"""Acquisition workspace HTTP boundaries."""

from __future__ import annotations

import sqlite3
import uuid

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
