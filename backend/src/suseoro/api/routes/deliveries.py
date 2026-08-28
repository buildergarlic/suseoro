"""Delivery reconciliation and barcode scan routes."""

from __future__ import annotations

import json
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
from suseoro.workflow.receiving import ReceivingService

router = APIRouter(prefix="/api/v2", tags=["deliveries"])


class DeliveryRowInput(BaseModel):
    isbn: str | None = None
    title: str = ""
    author: str | None = None
    edition: str | None = None
    quantity: int = 1
    unit_price: int | None = None


class DeliveryCreate(BaseModel):
    order_revision_id: str
    rows: list[DeliveryRowInput] = Field(min_length=1, max_length=5000)
    reason: str = Field(min_length=1, max_length=500)


class ScanStart(BaseModel):
    order_revision_id: str
    reason: str = Field(min_length=1, max_length=500)


class ScanRecord(BaseModel):
    workspace_id: str
    isbn: str
    expected_order_row_id: str | None = None


class DifferenceDisposition(BaseModel):
    workspace_id: str
    disposition: str
    reason: str = Field(min_length=1, max_length=500)


class ReceivingComplete(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


@router.get(
    "/workspaces/{workspace_id}/deliveries",
    summary="납품과 차이 목록 보기",
    operation_id="listDeliveries",
)
def list_deliveries(
    workspace_id: str,
    user: AuthenticatedUser = Depends(current_user),
    connection: sqlite3.Connection = Depends(database_connection),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
):
    decoded = decode_cursor(cursor, 2)
    clauses = ["school_id = ?", "workspace_id = ?"]
    parameters: list[object] = [user.school_id, workspace_id]
    if decoded:
        clauses.append("(created_at < ? OR (created_at = ? AND id < ?))")
        parameters.extend((decoded[0], decoded[0], decoded[1]))
    rows = connection.execute(
        f"""
        SELECT * FROM delivery_batches WHERE {" AND ".join(clauses)}
        ORDER BY created_at DESC, id DESC LIMIT ?
        """,
        (*parameters, limit + 1),
    ).fetchall()
    items = [
        {
            "id": row["id"],
            "order_revision_id": row["order_revision_id"],
            "delivery_number": row["delivery_number"],
            "created_at": row["created_at"],
            "sealed_at": row["sealed_at"],
        }
        for row in rows
    ]
    result = page(
        items, limit=limit, cursor_values=lambda item: (item["created_at"], item["id"])
    )
    differences = connection.execute(
        """
        SELECT id, kind, reference_key, disposition, row_version, details_json
        FROM receiving_differences
        WHERE school_id = ? AND workspace_id = ? AND active = 1
        ORDER BY created_at, id LIMIT 101
        """,
        (user.school_id, workspace_id),
    ).fetchall()
    result["differences"] = [dict(row) for row in differences[:100]]
    result["differences_truncated"] = len(differences) > 100
    return result


@router.get(
    "/workspaces/{workspace_id}/receiving/differences",
    summary="납품 차이 목록을 조건별로 보기",
    operation_id="listReceivingDifferences",
)
def list_receiving_differences(
    workspace_id: str,
    user: AuthenticatedUser = Depends(current_user),
    connection: sqlite3.Connection = Depends(database_connection),
    kind: str | None = Query(default=None),
    disposition: str | None = Query(default=None),
    active: bool | None = Query(default=True),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
):
    decoded = decode_cursor(cursor, 2)
    clauses = ["school_id = ?", "workspace_id = ?"]
    parameters: list[object] = [user.school_id, workspace_id]
    if kind is not None:
        clauses.append("kind = ?")
        parameters.append(kind)
    if disposition is not None:
        clauses.append("disposition = ?")
        parameters.append(disposition)
    if active is not None:
        clauses.append("active = ?")
        parameters.append(int(active))
    if decoded:
        clauses.append("(created_at < ? OR (created_at = ? AND id < ?))")
        parameters.extend((decoded[0], decoded[0], decoded[1]))
    rows = connection.execute(
        f"""
        SELECT id, kind, reference_key, disposition, active, row_version,
               details_json, created_at, updated_at
        FROM receiving_differences
        WHERE {" AND ".join(clauses)}
        ORDER BY created_at DESC, id DESC LIMIT ?
        """,
        (*parameters, limit + 1),
    ).fetchall()
    items = [
        {
            **dict(row),
            "details": json.loads(row["details_json"]),
        }
        for row in rows
    ]
    for item in items:
        item.pop("details_json", None)
    return page(
        items,
        limit=limit,
        cursor_values=lambda item: (item["created_at"], item["id"]),
    )


@router.post(
    "/workspaces/{workspace_id}/deliveries",
    summary="납품명세서 비교하기",
    operation_id="createDelivery",
)
def create_delivery(
    workspace_id: str,
    payload: DeliveryCreate,
    response: Response,
    user: AuthenticatedUser = Depends(current_user),
    connection: sqlite3.Connection = Depends(database_connection),
    workspace_version: int = Depends(require_if_match),
    idempotency_key: str = Depends(require_idempotency_key),
    request_id: str = Depends(require_request_id),
):
    result = ReceivingService(connection).record_delivery(
        school_id=user.school_id,
        workspace_id=workspace_id,
        order_revision_id=payload.order_revision_id,
        actor_id=user.id,
        actor_roles=user.roles,
        workspace_version=workspace_version,
        rows=[row.model_dump() for row in payload.rows],
        reason=payload.reason,
        idempotency_key=idempotency_key,
        request_id=request_id,
    )
    response.headers["ETag"] = f'"{result["row_version"]}"'
    return result


@router.post(
    "/workspaces/{workspace_id}/scans/sessions",
    summary="바코드 검수 시작하기",
    operation_id="startScanSession",
)
def start_scan(
    workspace_id: str,
    payload: ScanStart,
    response: Response,
    user: AuthenticatedUser = Depends(current_user),
    connection: sqlite3.Connection = Depends(database_connection),
    workspace_version: int = Depends(require_if_match),
    idempotency_key: str = Depends(require_idempotency_key),
    request_id: str = Depends(require_request_id),
):
    result = ReceivingService(connection).start_scan_session(
        school_id=user.school_id,
        workspace_id=workspace_id,
        order_revision_id=payload.order_revision_id,
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
    "/scans/sessions/{session_id}/events",
    summary="ISBN 바코드 검수 기록하기",
    operation_id="recordScan",
)
def record_scan(
    session_id: str,
    payload: ScanRecord,
    user: AuthenticatedUser = Depends(current_user),
    connection: sqlite3.Connection = Depends(database_connection),
    idempotency_key: str = Depends(require_idempotency_key),
    request_id: str = Depends(require_request_id),
):
    result = ReceivingService(connection).scan(
        school_id=user.school_id,
        workspace_id=payload.workspace_id,
        session_id=session_id,
        actor_id=user.id,
        actor_roles=user.roles,
        isbn=payload.isbn,
        idempotency_key=idempotency_key,
        request_id=request_id,
        expected_order_row_id=payload.expected_order_row_id,
    )
    publish_event(
        connection,
        school_id=user.school_id,
        workspace_id=payload.workspace_id,
        event_type="scan.result",
        data=result,
        deduplication_key=(
            f"{user.id}:POST:/scans/sessions/{session_id}/events:{idempotency_key}"
        ),
    )
    connection.commit()
    return result


@router.patch(
    "/receiving/differences/{difference_id}",
    summary="납품 차이 처리 방침 정하기",
    operation_id="setReceivingDisposition",
)
def set_disposition(
    difference_id: str,
    payload: DifferenceDisposition,
    response: Response,
    user: AuthenticatedUser = Depends(current_user),
    connection: sqlite3.Connection = Depends(database_connection),
    submitted_version: int = Depends(require_if_match),
    idempotency_key: str = Depends(require_idempotency_key),
    request_id: str = Depends(require_request_id),
):
    result = ReceivingService(connection).set_disposition(
        school_id=user.school_id,
        workspace_id=payload.workspace_id,
        difference_id=difference_id,
        actor_id=user.id,
        actor_roles=user.roles,
        submitted_version=submitted_version,
        disposition=payload.disposition,
        reason=payload.reason,
        idempotency_key=idempotency_key,
        request_id=request_id,
    )
    response.headers["ETag"] = f'"{result["row_version"]}"'
    return result


@router.post(
    "/workspaces/{workspace_id}/receiving/complete",
    summary="납품 검수 완료하기",
    operation_id="completeReceiving",
)
def complete_receiving(
    workspace_id: str,
    payload: ReceivingComplete,
    response: Response,
    user: AuthenticatedUser = Depends(current_user),
    connection: sqlite3.Connection = Depends(database_connection),
    workspace_version: int = Depends(require_if_match),
    idempotency_key: str = Depends(require_idempotency_key),
    request_id: str = Depends(require_request_id),
):
    result = ReceivingService(connection).complete(
        school_id=user.school_id,
        workspace_id=workspace_id,
        actor_id=user.id,
        actor_roles=user.roles,
        workspace_version=workspace_version,
        reason=payload.reason,
        idempotency_key=idempotency_key,
        request_id=request_id,
    )
    response.headers["ETag"] = f'"{result["row_version"]}"'
    return result
