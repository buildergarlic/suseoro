"""Candidate review, autosave, bulk decision, and edit-lock routes."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

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
from suseoro.services.audit import record_audit_event
from suseoro.services.concurrency import acquire_edit_lock
from suseoro.services.idempotency import (
    complete_idempotent_request,
    reserve_idempotency_key,
)
from suseoro.workflow.candidates import CandidateService

router = APIRouter(prefix="/api/v2", tags=["candidates"])


class CandidateUpdate(BaseModel):
    workspace_id: str
    changes: dict[str, Any]
    reason: str = Field(min_length=1, max_length=500)


class CandidateBulkUpdate(BaseModel):
    items: list[Any] = Field(min_length=1, max_length=1000)


class CandidateLock(BaseModel):
    workspace_id: str


def _candidate(row) -> dict[str, Any]:
    authors = json.loads(row["original_authors_json"])
    return {
        "id": row["id"],
        "workspace_id": row["workspace_id"],
        "title": row["original_title"],
        "authors": authors,
        "isbn13": row["isbn13"],
        "edition": row["original_edition"],
        "outcome": row["outcome"],
        "reason": row["reason"],
        "quantity": row["quantity"],
        "unit_price": row["unit_price"],
        "row_version": row["row_version"],
        "updated_at": row["updated_at"],
    }


@router.get(
    "/workspaces/{workspace_id}/candidates",
    summary="수서 후보 목록 보기",
    operation_id="listCandidates",
)
def list_candidates(
    workspace_id: str,
    user: AuthenticatedUser = Depends(current_user),
    connection: sqlite3.Connection = Depends(database_connection),
    outcome: str | None = Query(default=None),
    search: str | None = Query(default=None, max_length=200),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
):
    decoded = decode_cursor(cursor, 1)
    clauses = ["candidate.school_id = ?", "candidate.workspace_id = ?"]
    parameters: list[object] = [user.school_id, workspace_id]
    if outcome:
        clauses.append("candidate.outcome = ?")
        parameters.append(outcome)
    if search:
        clauses.append("recommendation.original_title LIKE ?")
        parameters.append(f"%{search}%")
    if decoded:
        clauses.append("candidate.id > ?")
        parameters.append(decoded[0])
    rows = connection.execute(
        f"""
        SELECT candidate.*, recommendation.original_title,
               recommendation.original_authors_json, recommendation.isbn13,
               recommendation.original_edition
        FROM candidate_decisions candidate
        JOIN recommendations recommendation ON recommendation.id = candidate.recommendation_id
        WHERE {" AND ".join(clauses)}
        ORDER BY candidate.id ASC LIMIT ?
        """,
        (*parameters, limit + 1),
    ).fetchall()
    items = [_candidate(row) for row in rows]
    return page(items, limit=limit, cursor_values=lambda item: (item["id"],))


@router.patch(
    "/candidates/{candidate_id}",
    summary="수서 후보 자동 저장하기",
    operation_id="updateCandidate",
)
def update_candidate(
    candidate_id: str,
    payload: CandidateUpdate,
    response: Response,
    user: AuthenticatedUser = Depends(require_role("OPERATOR")),
    connection: sqlite3.Connection = Depends(database_connection),
    submitted_version: int = Depends(require_if_match),
    idempotency_key: str = Depends(require_idempotency_key),
    request_id: str = Depends(require_request_id),
):
    result = CandidateService(connection).autosave(
        school_id=user.school_id,
        workspace_id=payload.workspace_id,
        candidate_id=candidate_id,
        actor_id=user.id,
        actor_roles=user.roles,
        submitted_version=submitted_version,
        changes=payload.changes,
        reason=payload.reason,
        idempotency_key=idempotency_key,
        request_id=request_id,
    )
    response.headers["ETag"] = f'"{result["row_version"]}"'
    return result


@router.post(
    "/workspaces/{workspace_id}/candidates/bulk-decision",
    summary="여러 수서 후보 한꺼번에 결정하기",
    operation_id="bulkDecideCandidates",
)
def bulk_decide(
    workspace_id: str,
    payload: CandidateBulkUpdate,
    user: AuthenticatedUser = Depends(require_role("OPERATOR")),
    connection: sqlite3.Connection = Depends(database_connection),
    idempotency_key: str = Depends(require_idempotency_key),
    request_id: str = Depends(require_request_id),
):
    return {
        "items": CandidateService(connection).bulk_decide(
            school_id=user.school_id,
            workspace_id=workspace_id,
            actor_id=user.id,
            actor_roles=user.roles,
            items=payload.items,
            idempotency_key=idempotency_key,
            request_id=request_id,
        )
    }


@router.post(
    "/candidates/{candidate_id}/lock",
    summary="수서 후보 편집 잠금 잡기",
    operation_id="lockCandidate",
)
def lock_candidate(
    candidate_id: str,
    payload: CandidateLock,
    user: AuthenticatedUser = Depends(require_role("OPERATOR")),
    connection: sqlite3.Connection = Depends(database_connection),
    idempotency_key: str = Depends(require_idempotency_key),
    request_id: str = Depends(require_request_id),
):
    exists = connection.execute(
        """
        SELECT row_version FROM candidate_decisions
        WHERE id = ? AND workspace_id = ? AND school_id = ?
        """,
        (candidate_id, payload.workspace_id, user.school_id),
    ).fetchone()
    if exists is None:
        raise domain_not_found("CANDIDATE_NOT_FOUND")
    replay = reserve_idempotency_key(
        connection,
        school_id=user.school_id,
        actor_id=user.id,
        route="POST /api/v2/candidates/lock",
        key=idempotency_key,
        request_body={
            "candidate_id": candidate_id,
            "workspace_id": payload.workspace_id,
        },
    )
    if replay:
        connection.rollback()
        return replay.body
    lock = acquire_edit_lock(
        connection,
        school_id=user.school_id,
        entity_type="candidate_decision",
        entity_id=candidate_id,
        actor_id=user.id,
    )
    result = {
        "candidate_id": candidate_id,
        "actor_id": lock.actor_id,
        "expires_at": lock.expires_at.isoformat().replace("+00:00", "Z"),
    }
    record_audit_event(
        connection,
        actor_id=user.id,
        school_id=user.school_id,
        action="CANDIDATE_LOCK_ACQUIRED",
        entity_type="candidate_decision",
        entity_id=candidate_id,
        before={"locked": False, "row_version": exists["row_version"]},
        after={**result, "row_version": exists["row_version"]},
        request_id=request_id,
    )
    publish_event(
        connection,
        school_id=user.school_id,
        workspace_id=payload.workspace_id,
        event_type="candidate.locked",
        data=result,
    )
    complete_idempotent_request(
        connection,
        school_id=user.school_id,
        actor_id=user.id,
        route="POST /api/v2/candidates/lock",
        key=idempotency_key,
        status=200,
        body=result,
    )
    connection.commit()
    return result
