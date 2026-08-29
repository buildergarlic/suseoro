"""Common-input bridge from immutable parsed sources into procurement workflow."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Response

from suseoro.api.dependencies import (
    AuthenticatedUser,
    current_user,
    database_connection,
    require_idempotency_key,
    require_if_match,
    require_request_id,
)
from suseoro.api.errors import domain_not_found
from suseoro.api.schemas import (
    ProcurementImportComposeResponse,
    ProcurementImportPage,
)
from suseoro.workflow.procurement_imports import (
    ProcurementImportRuleError,
    ProcurementImportService,
)

router = APIRouter(prefix="/api/v2", tags=["procurement"])


@router.get(
    "/workspaces/{workspace_id}/procurement-imports",
    response_model=ProcurementImportPage,
    summary="견적·납품 파일 처리 상태 보기",
    operation_id="listProcurementImports",
)
def list_procurement_imports(
    workspace_id: str,
    user: AuthenticatedUser = Depends(current_user),
    connection: sqlite3.Connection = Depends(database_connection),
) -> dict[str, object]:
    workspace = connection.execute(
        "SELECT 1 FROM acquisition_workspaces WHERE id = ? AND school_id = ?",
        (workspace_id, user.school_id),
    ).fetchone()
    if workspace is None:
        raise domain_not_found("WORKSPACE_NOT_FOUND")
    return {
        "items": ProcurementImportService(connection).list(
            user.school_id, workspace_id
        ),
        "next_cursor": None,
    }


@router.post(
    "/procurement-imports/{import_id}/compose",
    status_code=201,
    response_model=ProcurementImportComposeResponse,
    summary="파싱된 파일을 견적·납품에 반영하기",
    operation_id="composeProcurementImport",
)
def compose_procurement_import(
    import_id: str,
    response: Response,
    user: AuthenticatedUser = Depends(current_user),
    connection: sqlite3.Connection = Depends(database_connection),
    workspace_version: int = Depends(require_if_match),
    idempotency_key: str = Depends(require_idempotency_key),
    request_id: str = Depends(require_request_id),
) -> dict[str, object]:
    try:
        result = ProcurementImportService(connection).compose(
            school_id=user.school_id,
            import_id=import_id,
            actor_id=user.id,
            actor_roles=user.roles,
            workspace_version=workspace_version,
            idempotency_key=idempotency_key,
            request_id=request_id,
        )
    except ProcurementImportRuleError as error:
        status_code = 404 if error.code.endswith("_NOT_FOUND") else 409
        raise HTTPException(
            status_code=status_code, detail={"code": error.code}
        ) from error
    response.headers["ETag"] = f'"{result["row_version"]}"'
    return result
