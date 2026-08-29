"""Vendor quote and immutable order artifact routes."""

from __future__ import annotations

import json
import sqlite3

from fastapi import APIRouter, Depends, Query, Request, Response
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
from suseoro.api.schemas import OrderResponse
from suseoro.config import Settings
from suseoro.workflow.orders import OrderService
from suseoro.workflow.quotes import QuoteService

router = APIRouter(prefix="/api/v2", tags=["procurement"])
ORDER_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class OrderDownloadResponse(BinaryResponse):
    media_type = ORDER_MEDIA_TYPE


def _order_service(request: Request, connection: sqlite3.Connection) -> OrderService:
    settings: Settings = request.app.state.settings
    return OrderService(connection, settings.exports_dir / "orders")


class QuoteRowInput(BaseModel):
    isbn: str | None = None
    title: str = ""
    author: str = ""
    publisher: str | None = None
    edition: str | None = None
    quantity: int = 1
    unit_price: int | None = None
    list_price: int | None = None
    out_of_stock: bool = False


class QuoteCreate(BaseModel):
    approval_revision_id: str
    vendor_name: str = Field(min_length=1, max_length=200)
    rows: list[QuoteRowInput] = Field(min_length=1, max_length=5000)
    reason: str = Field(min_length=1, max_length=500)


class QuoteMatch(BaseModel):
    workspace_id: str
    quote_row_id: str
    approval_row_id: str
    reason: str = Field(min_length=1, max_length=500)


class OrderAllocationInput(BaseModel):
    quote_id: str
    quote_row_id: str
    quantity: int


class OrderCreate(BaseModel):
    approval_revision_id: str
    quote_id: str
    reason: str = Field(min_length=1, max_length=500)
    allocations: list[OrderAllocationInput] | None = Field(
        default=None, max_length=5000
    )
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
            "approval_revision_id": row["approval_revision_id"],
            "vendor_name": row["vendor_name"],
            "total_won": row["total_won"],
            "list_total_won": row["list_total_won"],
            "discount_won": row["discount_won"],
            "budget_overrun_won": row["budget_overrun_won"],
            "out_of_stock_count": row["out_of_stock_count"],
            "missing_price_count": row["missing_price_count"],
            "list_mismatch_count": row["list_mismatch_count"],
            "needs_review_count": row["needs_review_count"],
            "unmatched_count": row["unmatched_count"],
            "requires_reapproval": bool(row["requires_reapproval"]),
            "reconciliation": json.loads(row["reconciliation_json"]),
            "created_at": row["created_at"],
        }
        for row in rows
    ]
    return page(
        items, limit=limit, cursor_values=lambda item: (item["created_at"], item["id"])
    )


def _quote_rows(connection: sqlite3.Connection, quote_id: str):
    return [
        {
            "quote_row_id": row["id"],
            "approval_row_id": row["approval_row_id"],
            "match_status": row["match_status"],
            "isbn13": row["isbn13"],
            "title": row["title"],
            "author": row["author"],
            "publisher": row["publisher"],
            "edition": row["edition"],
            "quantity": row["quantity"],
            "unit_price": row["unit_price"],
            "list_price": row["list_price"],
            "out_of_stock": bool(row["out_of_stock"]),
            "line_total_won": row["line_total_won"],
        }
        for row in connection.execute(
            "SELECT * FROM vendor_quote_rows WHERE quote_id = ? ORDER BY created_at, id",
            (quote_id,),
        ).fetchall()
    ]


@router.get(
    "/quotes/{quote_id}",
    summary="견적 자세히 보기",
    operation_id="getQuote",
)
def get_quote(
    quote_id: str,
    user: AuthenticatedUser = Depends(current_user),
    connection: sqlite3.Connection = Depends(database_connection),
):
    row = connection.execute(
        """
        SELECT quote.*, workspace.status AS state,
               workspace.row_version AS workspace_row_version
        FROM vendor_quotes AS quote
        JOIN acquisition_workspaces AS workspace
          ON workspace.id = quote.workspace_id
         AND workspace.school_id = quote.school_id
        WHERE quote.id = ? AND quote.school_id = ? AND quote.sealed_at IS NOT NULL
        """,
        (quote_id, user.school_id),
    ).fetchone()
    if row is None:
        raise domain_not_found("QUOTE_NOT_FOUND")
    return {
        "quote_id": row["id"],
        "revision_number": row["revision_number"],
        "approval_revision_id": row["approval_revision_id"],
        "vendor_name": row["vendor_name"],
        "created_at": row["created_at"],
        "state": row["state"],
        "row_version": row["workspace_row_version"],
        "total_won": row["total_won"],
        "list_total_won": row["list_total_won"],
        "discount_won": row["discount_won"],
        "budget_overrun_won": row["budget_overrun_won"],
        "out_of_stock_count": row["out_of_stock_count"],
        "missing_price_count": row["missing_price_count"],
        "list_mismatch_count": row["list_mismatch_count"],
        "needs_review_count": row["needs_review_count"],
        "unmatched_count": row["unmatched_count"],
        "requires_reapproval": bool(row["requires_reapproval"]),
        "reconciliation": json.loads(row["reconciliation_json"]),
        "rows": _quote_rows(connection, quote_id),
    }


@router.get(
    "/workspaces/{workspace_id}/orders/current",
    summary="현재 발주 버전 보기",
    operation_id="getCurrentOrder",
)
def get_current_order(
    workspace_id: str,
    user: AuthenticatedUser = Depends(current_user),
    connection: sqlite3.Connection = Depends(database_connection),
):
    order = connection.execute(
        """
        SELECT revision.*, current.transmission_id,
               workspace.status AS state,
               workspace.row_version AS workspace_row_version,
               approval.budget_won,
               quote.vendor_name
        FROM workspace_current_orders AS current
        JOIN order_revisions AS revision ON revision.id = current.order_revision_id
        JOIN acquisition_workspaces AS workspace
          ON workspace.id = current.workspace_id
         AND workspace.school_id = current.school_id
        JOIN approval_revisions AS approval
          ON approval.id = revision.approval_revision_id
        JOIN vendor_quotes AS quote ON quote.id = revision.quote_id
        WHERE current.workspace_id = ? AND current.school_id = ?
          AND revision.sealed_at IS NOT NULL
        """,
        (workspace_id, user.school_id),
    ).fetchone()
    if order is None:
        return {"order": None}
    rows = [
        dict(row)
        for row in connection.execute(
            """
            SELECT id, isbn13, title, author, publisher, edition,
                   quantity, unit_price, line_total_won
            FROM order_rows WHERE order_revision_id = ? ORDER BY created_at, id
            """,
            (order["id"],),
        ).fetchall()
    ]
    artifacts = [
        {
            "vendor_name": row["vendor_name"],
            "quote_id": row["quote_id"],
            "artifact_id": row["artifact_id"],
            "sha256": row["sha256"],
            "size_bytes": row["size_bytes"],
        }
        for row in connection.execute(
            """
            SELECT link.vendor_name, link.quote_id, link.artifact_id,
                   artifact.sha256, artifact.size_bytes
            FROM order_vendor_artifacts AS link
            JOIN generated_artifacts AS artifact ON artifact.id = link.artifact_id
            WHERE link.order_revision_id = ? ORDER BY link.vendor_name, link.id
            """,
            (order["id"],),
        ).fetchall()
    ]
    total = sum(int(row["line_total_won"]) for row in rows)
    return {
        "order": {
            "revision_id": order["id"],
            "revision_number": order["revision_number"],
            "approval_revision_id": order["approval_revision_id"],
            "quote_id": order["quote_id"],
            "vendor_name": order["vendor_name"],
            "budget_won": order["budget_won"],
            "total_won": total,
            "difference_won": int(order["budget_won"]) - total,
            "state": order["state"],
            "row_version": order["workspace_row_version"],
            "sent": order["transmission_id"] is not None,
            "artifacts": artifacts,
            "rows": rows,
        }
    }


def _public_order_result(result: dict[str, object]) -> dict[str, object]:
    public = {key: value for key, value in result.items() if key != "path"}
    artifacts = public.get("artifacts")
    if isinstance(artifacts, list):
        public["artifacts"] = [
            {key: value for key, value in item.items() if key != "path"}
            for item in artifacts
            if isinstance(item, dict)
        ]
    return public


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
        rows=[row.model_dump() for row in payload.rows],
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
    request: Request,
    response: Response,
    user: AuthenticatedUser = Depends(current_user),
    connection: sqlite3.Connection = Depends(database_connection),
    workspace_version: int = Depends(require_if_match),
    idempotency_key: str = Depends(require_idempotency_key),
    request_id: str = Depends(require_request_id),
):
    result = _order_service(request, connection).generate_revision(
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
        allocations=(
            [allocation.model_dump() for allocation in payload.allocations]
            if payload.allocations is not None
            else None
        ),
        advanced_split_enabled=payload.advanced_split_enabled,
    )
    response.headers["ETag"] = f'"{result["row_version"]}"'
    return OrderResponse.model_validate(_public_order_result(result)).model_dump(
        mode="json"
    )


@router.get(
    "/orders/{order_revision_id}/download",
    response_class=OrderDownloadResponse,
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
    return OrderDownloadResponse(
        content=row["content_bytes"],
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
    request: Request,
    response: Response,
    user: AuthenticatedUser = Depends(current_user),
    connection: sqlite3.Connection = Depends(database_connection),
    workspace_version: int = Depends(require_if_match),
    idempotency_key: str = Depends(require_idempotency_key),
    request_id: str = Depends(require_request_id),
):
    result = _order_service(request, connection).mark_sent(
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
