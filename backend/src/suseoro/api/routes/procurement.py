"""Vendor quote and immutable order artifact routes."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, Query, Response
from fastapi.responses import Response as BinaryResponse
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
from suseoro.api.errors import domain_not_found
from suseoro.workflow.orders import OrderService
from suseoro.workflow.quotes import QuoteService

router = APIRouter(prefix="/api/v2", tags=["procurement"])


class QuoteCreate(BaseModel):
    approval_revision_id: str
    vendor_name: str = Field(min_length=1, max_length=200)
    rows: list[dict[str, Any]]
    reason: str = Field(min_length=1, max_length=500)


class QuoteMatch(BaseModel):
    workspace_id: str
    quote_row_id: str
    approval_row_id: str
    reason: str = Field(min_length=1, max_length=500)


class OrderCreate(BaseModel):
    approval_revision_id: str
    quote_id: str
    reason: str = Field(min_length=1, max_length=500)
    allocations: list[dict[str, Any]] | None = None
    advanced_split_enabled: bool = False


class OrderSent(BaseModel):
    workspace_id: str
    reason: str = Field(min_length=1, max_length=500)


@router.get(
    "/workspaces/{workspace_id}/quotes",
    summary="견적 비교 목록 보기",
    operation_id="listQuotes",
)
def list_quotes(
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
        clauses.append("(created_at < ? OR (created_at = ? AND id < ?))")
        parameters.extend((decoded[0], decoded[0], decoded[1]))
    rows = connection.execute(
        f"""
        SELECT * FROM vendor_quotes WHERE {" AND ".join(clauses)}
        ORDER BY created_at DESC, id DESC LIMIT ?
        """,
        (*parameters, limit + 1),
    ).fetchall()
    items = [
        {
            "id": row["id"],
            "vendor_name": row["vendor_name"],
            "total_won": row["total_won"],
            "budget_overrun_won": row["budget_overrun_won"],
            "requires_reapproval": bool(row["requires_reapproval"]),
            "reconciliation": json.loads(row["reconciliation_json"]),
            "created_at": row["created_at"],
        }
        for row in rows
    ]
    return page(
        items, limit=limit, cursor_values=lambda item: (item["created_at"], item["id"])
    )


@router.post(
    "/workspaces/{workspace_id}/quotes",
    summary="업체 견적 비교하기",
    operation_id="createQuote",
)
def create_quote(
    workspace_id: str,
    payload: QuoteCreate,
    response: Response,
    user: AuthenticatedUser = Depends(current_user),
    connection: sqlite3.Connection = Depends(database_connection),
    workspace_version: int = Depends(require_if_match),
    idempotency_key: str = Depends(require_idempotency_key),
    request_id: str = Depends(require_request_id),
):
    result = QuoteService(connection).ingest(
        school_id=user.school_id,
        workspace_id=workspace_id,
        approval_revision_id=payload.approval_revision_id,
        actor_id=user.id,
        actor_roles=user.roles,
        workspace_version=workspace_version,
        vendor_name=payload.vendor_name,
        rows=payload.rows,
        reason=payload.reason,
        idempotency_key=idempotency_key,
        request_id=request_id,
    )
    response.headers["ETag"] = f'"{result["row_version"]}"'
    return result


@router.post(
    "/quotes/{quote_id}/matches",
    summary="견적 행 직접 연결하기",
    operation_id="matchQuoteRow",
)
def match_quote_row(
    quote_id: str,
    payload: QuoteMatch,
    response: Response,
    user: AuthenticatedUser = Depends(current_user),
    connection: sqlite3.Connection = Depends(database_connection),
    workspace_version: int = Depends(require_if_match),
    idempotency_key: str = Depends(require_idempotency_key),
    request_id: str = Depends(require_request_id),
):
    result = QuoteService(connection).confirm_manual_match(
        school_id=user.school_id,
        workspace_id=payload.workspace_id,
        quote_id=quote_id,
        quote_row_id=payload.quote_row_id,
        approval_row_id=payload.approval_row_id,
        actor_id=user.id,
        actor_roles=user.roles,
        workspace_version=workspace_version,
        reason=payload.reason,
        idempotency_key=idempotency_key,
        request_id=request_id,
    )
    response.headers["ETag"] = f'"{result["row_version"]}"'
    return result


@router.post(
    "/workspaces/{workspace_id}/orders",
    summary="발주 파일 만들기",
    operation_id="createOrder",
)
def create_order(
    workspace_id: str,
    payload: OrderCreate,
    response: Response,
    user: AuthenticatedUser = Depends(current_user),
    connection: sqlite3.Connection = Depends(database_connection),
    workspace_version: int = Depends(require_if_match),
    idempotency_key: str = Depends(require_idempotency_key),
    request_id: str = Depends(require_request_id),
):
    result = OrderService(connection).generate_revision(
        school_id=user.school_id,
        workspace_id=workspace_id,
        approval_revision_id=payload.approval_revision_id,
        quote_id=payload.quote_id,
        actor_id=user.id,
        actor_roles=user.roles,
        workspace_version=workspace_version,
        reason=payload.reason,
        idempotency_key=idempotency_key,
        request_id=request_id,
        allocations=payload.allocations,
        advanced_split_enabled=payload.advanced_split_enabled,
    )
    response.headers["ETag"] = f'"{result["row_version"]}"'
    return result


@router.get(
    "/orders/{order_revision_id}/download",
    summary="발주 파일 내려받기",
    operation_id="downloadOrder",
)
def download_order(
    order_revision_id: str,
    user: AuthenticatedUser = Depends(current_user),
    connection: sqlite3.Connection = Depends(database_connection),
) -> BinaryResponse:
    row = connection.execute(
        """
        SELECT artifact.content_bytes, artifact.sha256
        FROM order_revisions revision
        JOIN generated_artifacts artifact ON artifact.id = revision.artifact_id
        WHERE revision.id = ? AND revision.school_id = ? AND revision.sealed_at IS NOT NULL
        """,
        (order_revision_id, user.school_id),
    ).fetchone()
    if row is None:
        raise domain_not_found("ORDER_NOT_FOUND")
    return BinaryResponse(
        content=row["content_bytes"],
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="order-{order_revision_id}.xlsx"',
            "ETag": f'"{row["sha256"]}"',
        },
    )


@router.post(
    "/orders/{order_revision_id}/sent",
    summary="발주 파일 전달 확인하기",
    operation_id="markOrderSent",
)
def mark_order_sent(
    order_revision_id: str,
    payload: OrderSent,
    response: Response,
    user: AuthenticatedUser = Depends(current_user),
    connection: sqlite3.Connection = Depends(database_connection),
    workspace_version: int = Depends(require_if_match),
    idempotency_key: str = Depends(require_idempotency_key),
    request_id: str = Depends(require_request_id),
):
    result = OrderService(connection).mark_sent(
        school_id=user.school_id,
        workspace_id=payload.workspace_id,
        order_revision_id=order_revision_id,
        actor_id=user.id,
        actor_roles=user.roles,
        workspace_version=workspace_version,
        reason=payload.reason,
        idempotency_key=idempotency_key,
        request_id=request_id,
    )
    response.headers["ETag"] = f'"{result["row_version"]}"'
    return result
