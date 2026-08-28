"""Audit history and local operational administration routes."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import BaseModel

from suseoro.api.common import decode_cursor, page
from suseoro.api.dependencies import (
    AuthenticatedUser,
    current_user,
    database_connection,
    enforce_role,
    require_idempotency_key,
    require_request_id,
    require_role,
)
from suseoro.api.errors import domain_not_found
from suseoro.backup.service import (
    BackupService,
    LocalAdminConfirmationRequired,
    RestoreVerificationError,
)
from suseoro.db.connection import connect
from suseoro.migration.v1 import inspect_v1, migrate_v1
from suseoro.services.audit import record_audit_event
from suseoro.services.idempotency import (
    complete_idempotent_request,
    reserve_idempotency_key,
)

router = APIRouter(prefix="/api/v2", tags=["operations"])


class V1InspectRequest(BaseModel):
    source_path: str


class V1MigrationRequest(V1InspectRequest):
    destination_path: str


class BackupCreateRequest(BaseModel):
    kind: str = "daily"


class RestoreRequest(BaseModel):
    manifest_file: str


@router.get("/audit", summary="변경 이력 보기", operation_id="listAuditEvents")
def list_audit_events(
    user: AuthenticatedUser = Depends(current_user),
    connection: sqlite3.Connection = Depends(database_connection),
    entity_type: str | None = Query(default=None),
    entity_id: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
):
    decoded = decode_cursor(cursor, 2)
    clauses = ["school_id = ?"]
    parameters: list[object] = [user.school_id]
    if entity_type:
        clauses.append("entity_type = ?")
        parameters.append(entity_type)
    if entity_id:
        clauses.append("entity_id = ?")
        parameters.append(entity_id)
    if decoded:
        clauses.append("(occurred_at < ? OR (occurred_at = ? AND id < ?))")
        parameters.extend((decoded[0], decoded[0], decoded[1]))
    rows = connection.execute(
        f"""
        SELECT event.*, user.display_name
        FROM audit_events event LEFT JOIN users user ON user.id = event.actor_id
        WHERE {" AND ".join("event." + clause if index == 0 else clause for index, clause in enumerate(clauses))}
        ORDER BY event.occurred_at DESC, event.id DESC LIMIT ?
        """,
        (*parameters, limit + 1),
    ).fetchall()
    items = [
        {
            "id": row["id"],
            "actor_id": row["actor_id"],
            "actor_name": row["display_name"],
            "action": row["action"],
            "entity_type": row["entity_type"],
            "entity_id": row["entity_id"],
            "before": json.loads(row["before_json"]) if row["before_json"] else None,
            "after": json.loads(row["after_json"]) if row["after_json"] else None,
            "request_id": row["request_id"],
            "occurred_at": row["occurred_at"],
        }
        for row in rows
    ]
    return page(
        items, limit=limit, cursor_values=lambda item: (item["occurred_at"], item["id"])
    )


@router.post(
    "/admin/v1-migration/inspect",
    summary="이전 버전 자료 점검하기",
    operation_id="inspectV1Migration",
)
def inspect_migration(
    payload: V1InspectRequest,
    user: AuthenticatedUser = Depends(require_role("OPERATOR")),
):
    return inspect_v1(Path(payload.source_path)).to_dict()


@router.post(
    "/admin/v1-migration/run",
    summary="이전 버전 자료 복사하기",
    operation_id="runV1Migration",
)
def run_migration(
    payload: V1MigrationRequest,
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
        route="POST /api/v2/admin/v1-migration/run",
        key=idempotency_key,
        request_body=body,
    )
    if replay:
        connection.rollback()
        return replay.body
    result = migrate_v1(
        Path(payload.source_path), Path(payload.destination_path)
    ).to_dict()
    complete_idempotent_request(
        connection,
        school_id=user.school_id,
        actor_id=user.id,
        route="POST /api/v2/admin/v1-migration/run",
        key=idempotency_key,
        status=200,
        body=result,
    )
    connection.commit()
    return result


@router.post(
    "/admin/backups",
    status_code=201,
    summary="데이터베이스 백업 만들기",
    operation_id="createBackup",
)
def create_backup(
    payload: BackupCreateRequest,
    request: Request,
    user: AuthenticatedUser = Depends(require_role("OPERATOR")),
    connection: sqlite3.Connection = Depends(database_connection),
    idempotency_key: str = Depends(require_idempotency_key),
    request_id: str = Depends(require_request_id),
):
    replay = reserve_idempotency_key(
        connection,
        school_id=user.school_id,
        actor_id=user.id,
        route="POST /api/v2/admin/backups",
        key=idempotency_key,
        request_body=payload.model_dump(),
    )
    if replay:
        connection.rollback()
        return replay.body
    settings = request.app.state.settings
    manifest = BackupService(settings.database_path, settings.backups_dir).create(
        kind=payload.kind
    )
    result = manifest.to_dict()
    complete_idempotent_request(
        connection,
        school_id=user.school_id,
        actor_id=user.id,
        route="POST /api/v2/admin/backups",
        key=idempotency_key,
        status=201,
        body=result,
    )
    connection.commit()
    return result


@router.get(
    "/admin/backups", summary="데이터베이스 백업 목록 보기", operation_id="listBackups"
)
def list_backups(
    request: Request,
    user: AuthenticatedUser = Depends(require_role("OPERATOR")),
):
    settings = request.app.state.settings
    return {
        "items": [
            manifest.to_dict()
            for manifest in BackupService(
                settings.database_path, settings.backups_dir
            ).list()
        ]
    }


@router.post(
    "/admin/restores",
    summary="검증된 데이터베이스 백업 복원하기",
    operation_id="restoreBackup",
)
def restore_backup(
    payload: RestoreRequest,
    request: Request,
    confirmation: str | None = Header(default=None, alias="X-Local-Admin-Confirmation"),
    idempotency_key: str = Depends(require_idempotency_key),
    request_id: str = Depends(require_request_id),
):
    user = getattr(request.state, "authenticated_user", None)
    if user is None:
        raise domain_not_found("AUTHENTICATION_REQUIRED")
    enforce_role(user, "OPERATOR")
    host = request.client.host if request.client else ""
    local_request = host in {"127.0.0.1", "::1", "localhost", "testclient"}
    settings = request.app.state.settings
    service = BackupService(settings.database_path, settings.backups_dir)
    manifest_path = settings.backups_dir / Path(payload.manifest_file).name
    route = "POST /api/v2/admin/restores"
    request_body = payload.model_dump()
    idempotency_connection = connect(settings.database_path)
    try:
        replay = reserve_idempotency_key(
            idempotency_connection,
            school_id=user.school_id,
            actor_id=user.id,
            route=route,
            key=idempotency_key,
            request_body=request_body,
        )
        if replay is not None:
            idempotency_connection.rollback()
            return replay.body
        # The restore atomically replaces this database, so the completed
        # reservation is recorded against the restored database below.
        idempotency_connection.rollback()
    finally:
        idempotency_connection.close()
    try:
        result = service.restore(
            manifest_path,
            confirmation_token=confirmation,
            expected_confirmation_token=request.app.state.local_admin_confirmation_token,
            local_request=local_request,
        )
    except LocalAdminConfirmationRequired as error:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=403, detail={"code": "LOCAL_ADMIN_CONFIRMATION_REQUIRED"}
        ) from error
    except (RestoreVerificationError, FileNotFoundError) as error:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=400, detail={"code": "RESTORE_VERIFICATION_FAILED"}
        ) from error
    body = {
        "restored": result.restored_manifest.to_dict(),
        "pre_restore_backup": result.pre_restore_backup.to_dict(),
    }
    idempotency_connection = connect(settings.database_path)
    try:
        replay = reserve_idempotency_key(
            idempotency_connection,
            school_id=user.school_id,
            actor_id=user.id,
            route=route,
            key=idempotency_key,
            request_body=request_body,
        )
        if replay is not None:
            idempotency_connection.rollback()
            return replay.body
        record_audit_event(
            idempotency_connection,
            actor_id=user.id,
            school_id=user.school_id,
            action="DATABASE_RESTORED",
            entity_type="backup_manifest",
            entity_id=result.restored_manifest.id,
            before={"pre_restore_backup_id": result.pre_restore_backup.id},
            after={"restored_backup_id": result.restored_manifest.id},
            request_id=request_id,
        )
        complete_idempotent_request(
            idempotency_connection,
            school_id=user.school_id,
            actor_id=user.id,
            route=route,
            key=idempotency_key,
            status=200,
            body=body,
        )
        idempotency_connection.commit()
    finally:
        idempotency_connection.close()
    return body
