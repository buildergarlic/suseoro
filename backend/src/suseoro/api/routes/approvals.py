"""Immutable approval revision routes."""

from __future__ import annotations

import sqlite3

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
)
from suseoro.api.routes.events import publish_event
from suseoro.workflow.approvals import ApprovalService

router = APIRouter(prefix="/api/v2", tags=["approvals"])


class ApprovalRequest(BaseModel):
    budget_won: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=500)
    candidate_collection_revision: int = Field(ge=0)


class ApprovalAction(BaseModel):
    workspace_id: str
    reason: str = Field(min_length=1, max_length=500)


class ApprovalComment(ApprovalAction):
    content: str = Field(min_length=1, max_length=4000)


def _event(connection, user, workspace_id, event_type, result, idempotency_key):
    publish_event(
        connection,
        school_id=user.school_id,
        workspace_id=workspace_id,
        event_type=event_type,
        data=result,
        deduplication_key=(f"{user.id}:{workspace_id}:{event_type}:{idempotency_key}"),
    )
    connection.commit()


@router.get(
    "/workspaces/{workspace_id}/approvals",
    summary="승인 버전 목록 보기",
    operation_id="listApprovals",
)
def list_approvals(
    workspace_id: str,
    user: AuthenticatedUser = Depends(current_user),
    connection: sqlite3.Connection = Depends(database_connection),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
):
    decoded = decode_cursor(cursor, 2)
    clauses = ["school_id = ?", "workspace_id = ?", "sealed_at IS NOT NULL"]
    parameters: list[object] = [user.school_id, workspace_id]
    if decoded:
        clauses.append("(revision_number < ? OR (revision_number = ? AND id < ?))")
        parameters.extend((decoded[0], decoded[0], decoded[1]))
    rows = connection.execute(
        f"""
        SELECT * FROM approval_revisions WHERE {" AND ".join(clauses)}
        ORDER BY revision_number DESC, id DESC LIMIT ?
        """,
        (*parameters, limit + 1),
    ).fetchall()
    items = [
        {
            "id": row["id"],
            "revision_number": row["revision_number"],
            "budget_won": row["budget_won"],
            "expected_total_won": row["expected_total_won"],
            "sha256": row["sha256"],
            "candidate_collection_revision": row["candidate_collection_revision"],
            "candidate_count": connection.execute(
                "SELECT COUNT(*) FROM approval_rows WHERE approval_revision_id = ?",
                (row["id"],),
            ).fetchone()[0],
            "decision": connection.execute(
                "SELECT decision FROM approval_decisions WHERE approval_revision_id = ?",
                (row["id"],),
            ).fetchone(),
            "request_reason": row["reason"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]
    for item in items:
        decision = item["decision"]
        item["decision"] = decision["decision"] if decision is not None else None
    return page(
        items,
        limit=limit,
        cursor_values=lambda item: (item["revision_number"], item["id"]),
    )


@router.get(
    "/approvals/{revision_id}",
    summary="승인 버전 자세히 보기",
    operation_id="getApproval",
)
def get_approval(
    revision_id: str,
    user: AuthenticatedUser = Depends(current_user),
    connection: sqlite3.Connection = Depends(database_connection),
):
    return ApprovalService(connection).view_revision(
        school_id=user.school_id,
        revision_id=revision_id,
        actor_id=user.id,
        actor_roles=user.roles,
    )


@router.post(
    "/workspaces/{workspace_id}/approvals/requests",
    summary="후보 목록 승인 요청하기",
    operation_id="requestApproval",
)
def request_approval(
    workspace_id: str,
    payload: ApprovalRequest,
    response: Response,
    user: AuthenticatedUser = Depends(current_user),
    connection: sqlite3.Connection = Depends(database_connection),
    workspace_version: int = Depends(require_if_match),
    idempotency_key: str = Depends(require_idempotency_key),
    request_id: str = Depends(require_request_id),
):
    result = ApprovalService(connection).request_approval(
        school_id=user.school_id,
        workspace_id=workspace_id,
        actor_id=user.id,
        actor_roles=user.roles,
        workspace_version=workspace_version,
        candidate_collection_revision=payload.candidate_collection_revision,
        budget_won=payload.budget_won,
        reason=payload.reason,
        idempotency_key=idempotency_key,
        request_id=request_id,
    )
    _event(
        connection,
        user,
        workspace_id,
        "approval.requested",
        result,
        idempotency_key,
    )
    response.headers["ETag"] = f'"{result["row_version"]}"'
    return result


@router.post(
    "/approvals/{revision_id}/cancel",
    summary="승인 요청 취소하기",
    operation_id="cancelApproval",
)
def cancel_approval(
    revision_id: str,
    payload: ApprovalAction,
    response: Response,
    user: AuthenticatedUser = Depends(current_user),
    connection: sqlite3.Connection = Depends(database_connection),
    workspace_version: int = Depends(require_if_match),
    idempotency_key: str = Depends(require_idempotency_key),
    request_id: str = Depends(require_request_id),
):
    result = ApprovalService(connection).cancel_request(
        school_id=user.school_id,
        workspace_id=payload.workspace_id,
        revision_id=revision_id,
        actor_id=user.id,
        actor_roles=user.roles,
        workspace_version=workspace_version,
        reason=payload.reason,
        idempotency_key=idempotency_key,
        request_id=request_id,
    )
    _event(
        connection,
        user,
        payload.workspace_id,
        "approval.cancelled",
        result,
        idempotency_key,
    )
    response.headers["ETag"] = f'"{result["row_version"]}"'
    return result


def _decide(
    *,
    revision_id,
    payload,
    response,
    user,
    connection,
    workspace_version,
    idempotency_key,
    request_id,
    approved,
):
    method = (
        ApprovalService(connection).approve
        if approved
        else ApprovalService(connection).reject
    )
    result = method(
        school_id=user.school_id,
        workspace_id=payload.workspace_id,
        revision_id=revision_id,
        actor_id=user.id,
        actor_roles=user.roles,
        workspace_version=workspace_version,
        reason=payload.reason,
        idempotency_key=idempotency_key,
        request_id=request_id,
    )
    _event(
        connection,
        user,
        payload.workspace_id,
        "approval.approved" if approved else "approval.changes_requested",
        result,
        idempotency_key,
    )
    response.headers["ETag"] = f'"{result["row_version"]}"'
    return result


@router.post(
    "/approvals/{revision_id}/approve",
    summary="승인 요청 승인하기",
    operation_id="approveApproval",
)
def approve(
    revision_id: str,
    payload: ApprovalAction,
    response: Response,
    user: AuthenticatedUser = Depends(current_user),
    connection: sqlite3.Connection = Depends(database_connection),
    workspace_version: int = Depends(require_if_match),
    idempotency_key: str = Depends(require_idempotency_key),
    request_id: str = Depends(require_request_id),
):
    return _decide(
        revision_id=revision_id,
        payload=payload,
        response=response,
        user=user,
        connection=connection,
        workspace_version=workspace_version,
        idempotency_key=idempotency_key,
        request_id=request_id,
        approved=True,
    )


@router.post(
    "/approvals/{revision_id}/changes-request",
    summary="승인 요청에 수정 요청 보내기",
    operation_id="requestApprovalChanges",
)
def request_changes(
    revision_id: str,
    payload: ApprovalAction,
    response: Response,
    user: AuthenticatedUser = Depends(current_user),
    connection: sqlite3.Connection = Depends(database_connection),
    workspace_version: int = Depends(require_if_match),
    idempotency_key: str = Depends(require_idempotency_key),
    request_id: str = Depends(require_request_id),
):
    return _decide(
        revision_id=revision_id,
        payload=payload,
        response=response,
        user=user,
        connection=connection,
        workspace_version=workspace_version,
        idempotency_key=idempotency_key,
        request_id=request_id,
        approved=False,
    )


@router.post(
    "/approvals/{revision_id}/comments",
    summary="승인 버전에 의견 남기기",
    operation_id="commentApproval",
)
def comment(
    revision_id: str,
    payload: ApprovalComment,
    response: Response,
    user: AuthenticatedUser = Depends(current_user),
    connection: sqlite3.Connection = Depends(database_connection),
    workspace_version: int = Depends(require_if_match),
    idempotency_key: str = Depends(require_idempotency_key),
    request_id: str = Depends(require_request_id),
):
    result = ApprovalService(connection).comment(
        school_id=user.school_id,
        workspace_id=payload.workspace_id,
        revision_id=revision_id,
        actor_id=user.id,
        actor_roles=user.roles,
        workspace_version=workspace_version,
        content=payload.content,
        reason=payload.reason,
        idempotency_key=idempotency_key,
        request_id=request_id,
    )
    _event(
        connection,
        user,
        payload.workspace_id,
        "approval.commented",
        result,
        idempotency_key,
    )
    response.headers["ETag"] = f'"{result["row_version"]}"'
    return result
