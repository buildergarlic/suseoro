"""Audit history and local operational administration routes."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
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
    RestoreReplayConflict,
    RestoreVerificationError,
)
from suseoro.catalog.contracts import CatalogRecord, SourceType
from suseoro.catalog.sync import CatalogSyncService
from suseoro.db.connection import connect
from suseoro.ingestion.contracts import DocumentRole, RowStatus
from suseoro.ingestion.parsers.tabular import parse_tabular
from suseoro.migration.v1 import inspect_v1, migrate_v1
from suseoro.security.sessions import format_utc, utc_now
from suseoro.services.audit import record_audit_event, record_system_audit_event
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
    kind: Literal["daily", "weekly", "monthly", "manual", "pre_upgrade"] = "daily"


class RestoreRequest(BaseModel):
    manifest_file: str


class V1CatalogActivationRequest(BaseModel):
    confirmed_row_count: int


def _local_admin_confirmed(request: Request, confirmation: str | None) -> bool:
    host = request.client.host if request.client else ""
    expected = request.app.state.local_admin_confirmation_token
    return bool(
        host in {"127.0.0.1", "::1", "localhost", "testclient"}
        and confirmation
        and expected
        and hmac.compare_digest(confirmation, expected)
    )


def _contained(path: Path, roots: tuple[Path, ...], *, strict: bool) -> Path:
    try:
        resolved = path.resolve(strict=strict)
    except OSError as error:
        raise HTTPException(status_code=400, detail={"code": "INVALID_PATH"}) from error
    if not any(resolved == root or resolved.is_relative_to(root) for root in roots):
        raise HTTPException(
            status_code=400, detail={"code": "PATH_OUTSIDE_CONFIGURED_ROOT"}
        )
    return resolved


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
    clauses = ["event.school_id = ?"]
    parameters: list[object] = [user.school_id]
    if entity_type:
        clauses.append("event.entity_type = ?")
        parameters.append(entity_type)
    if entity_id:
        clauses.append("event.entity_id = ?")
        parameters.append(entity_id)
    if decoded:
        clauses.append(
            "(event.occurred_at < ? OR (event.occurred_at = ? AND event.id < ?))"
        )
        parameters.extend((decoded[0], decoded[0], decoded[1]))
    rows = connection.execute(
        f"""
        SELECT event.*, user.display_name
        FROM audit_events event LEFT JOIN users user ON user.id = event.actor_id
        WHERE {" AND ".join(clauses)}
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


@router.get(
    "/v1/catalog-candidates",
    summary="이전 장서 활성화 후보 보기",
    operation_id="listV1CatalogCandidates",
)
def list_v1_catalog_candidates(
    user: AuthenticatedUser = Depends(current_user),
    connection: sqlite3.Connection = Depends(database_connection),
    status: Literal["PENDING_CONFIRMATION", "ACTIVATED", "REJECTED"] | None = Query(
        default=None
    ),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
):
    decoded = decode_cursor(cursor, 2)
    clauses = ["school_id = ?"]
    parameters: list[object] = [user.school_id]
    if status:
        clauses.append("status = ?")
        parameters.append(status)
    if decoded:
        clauses.append("(created_at < ? OR (created_at = ? AND id < ?))")
        parameters.extend((decoded[0], decoded[0], decoded[1]))
    rows = connection.execute(
        f"""
        SELECT id, source_copy_path, sha256, row_count, status,
               catalog_version_id, created_at, activated_at
        FROM v1_catalog_candidates
        WHERE {" AND ".join(clauses)}
        ORDER BY created_at DESC, id DESC LIMIT ?
        """,
        (*parameters, limit + 1),
    ).fetchall()
    items = [dict(row) for row in rows]
    return page(
        items,
        limit=limit,
        cursor_values=lambda item: (item["created_at"], item["id"]),
    )


@router.get(
    "/v1/legacy-workspaces",
    summary="읽기 전용 이전 작업 보기",
    operation_id="listV1LegacyWorkspaces",
)
def list_v1_legacy_workspaces(
    user: AuthenticatedUser = Depends(current_user),
    connection: sqlite3.Connection = Depends(database_connection),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
):
    decoded = decode_cursor(cursor, 2)
    parameters: list[object] = [user.school_id]
    cursor_clause = ""
    if decoded:
        cursor_clause = "AND (imported_at < ? OR (imported_at = ? AND id < ?))"
        parameters.extend((decoded[0], decoded[0], decoded[1]))
    rows = connection.execute(
        f"""
        SELECT id, display_name, source_copy_path, sha256,
               is_read_only, imported_at
        FROM legacy_v1_workspaces
        WHERE school_id = ? {cursor_clause}
        ORDER BY imported_at DESC, id DESC LIMIT ?
        """,
        (*parameters, limit + 1),
    ).fetchall()
    items = [
        {
            **dict(row),
            "is_read_only": bool(row["is_read_only"]),
        }
        for row in rows
    ]
    return page(
        items,
        limit=limit,
        cursor_values=lambda item: (item["imported_at"], item["id"]),
    )


@router.post(
    "/admin/v1-migration/inspect",
    summary="이전 버전 자료 점검하기",
    operation_id="inspectV1Migration",
)
def inspect_migration(
    payload: V1InspectRequest,
    request: Request,
    confirmation: str | None = Header(default=None, alias="X-Local-Admin-Confirmation"),
    user: AuthenticatedUser = Depends(require_role("OPERATOR")),
):
    if not _local_admin_confirmed(request, confirmation):
        raise HTTPException(
            status_code=403, detail={"code": "LOCAL_ADMIN_CONFIRMATION_REQUIRED"}
        )
    settings = request.app.state.settings
    source = _contained(
        Path(payload.source_path), settings.v1_import_roots, strict=True
    )
    return inspect_v1(source).to_dict()


@router.post(
    "/admin/v1-migration/run",
    summary="이전 버전 자료 복사하기",
    operation_id="runV1Migration",
)
def run_migration(
    payload: V1MigrationRequest,
    request: Request,
    confirmation: str | None = Header(default=None, alias="X-Local-Admin-Confirmation"),
    user: AuthenticatedUser = Depends(require_role("OPERATOR")),
    connection: sqlite3.Connection = Depends(database_connection),
    idempotency_key: str = Depends(require_idempotency_key),
    request_id: str = Depends(require_request_id),
):
    if not _local_admin_confirmed(request, confirmation):
        raise HTTPException(
            status_code=403, detail={"code": "LOCAL_ADMIN_CONFIRMATION_REQUIRED"}
        )
    settings = request.app.state.settings
    source = _contained(
        Path(payload.source_path), settings.v1_import_roots, strict=True
    )
    destination = _contained(
        Path(payload.destination_path),
        (settings.v1_destination_root.resolve(),),
        strict=False,
    )
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
        source,
        destination,
        database_path=settings.database_path,
        connection=connection,
        actor_id=user.id,
        actor_school_id=user.school_id,
        request_id=request_id,
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
    "/admin/v1-migration/catalog-candidates/{candidate_id}/activate",
    summary="이전 장서 후보 건수 확인 후 활성화하기",
    operation_id="activateV1CatalogCandidate",
)
def activate_v1_catalog_candidate(
    candidate_id: str,
    payload: V1CatalogActivationRequest,
    request: Request,
    confirmation: str | None = Header(default=None, alias="X-Local-Admin-Confirmation"),
    user: AuthenticatedUser = Depends(require_role("OPERATOR")),
    connection: sqlite3.Connection = Depends(database_connection),
    idempotency_key: str = Depends(require_idempotency_key),
    request_id: str = Depends(require_request_id),
):
    if not _local_admin_confirmed(request, confirmation):
        raise HTTPException(
            status_code=403, detail={"code": "LOCAL_ADMIN_CONFIRMATION_REQUIRED"}
        )
    route = (
        f"POST /api/v2/admin/v1-migration/catalog-candidates/{candidate_id}/activate"
    )
    replay = reserve_idempotency_key(
        connection,
        school_id=user.school_id,
        actor_id=user.id,
        route=route,
        key=idempotency_key,
        request_body=payload.model_dump(),
    )
    if replay:
        connection.rollback()
        return replay.body
    candidate = connection.execute(
        "SELECT * FROM v1_catalog_candidates WHERE id = ? AND school_id = ?",
        (candidate_id, user.school_id),
    ).fetchone()
    if candidate is None:
        raise domain_not_found("V1_CATALOG_CANDIDATE_NOT_FOUND")
    if candidate["status"] != "PENDING_CONFIRMATION":
        raise HTTPException(
            status_code=409, detail={"code": "V1_CATALOG_CANDIDATE_NOT_PENDING"}
        )
    if payload.confirmed_row_count != candidate["row_count"]:
        raise HTTPException(
            status_code=409, detail={"code": "V1_CATALOG_ROW_COUNT_MISMATCH"}
        )
    source_path = _contained(
        Path(candidate["source_copy_path"]),
        (request.app.state.settings.v1_destination_root.resolve(),),
        strict=True,
    )
    parsed = parse_tabular(
        source_path,
        role=DocumentRole.CATALOG_FULL,
        sha256=candidate["sha256"],
    )
    records = []
    for index, row in enumerate(parsed.rows, start=1):
        if row.status != RowStatus.SUCCESS:
            raise HTTPException(
                status_code=422, detail={"code": "V1_CATALOG_PARSE_FAILED"}
            )
        fields = {name: field.value for name, field in row.fields.items()}
        registration = fields.get("registration_number")
        records.append(
            CatalogRecord(
                source_item_id=str(registration or f"v1-{index}"),
                title=str(fields.get("title") or ""),
                isbn=fields.get("isbn"),
                authors=tuple(
                    part.strip()
                    for part in str(fields.get("author") or "").split(";")
                    if part.strip()
                ),
                publisher=fields.get("publisher"),
                registration_number=str(registration) if registration else None,
                call_number=fields.get("call_number"),
                raw_fields=row.raw_values,
            )
        )
    if len(records) != payload.confirmed_row_count:
        raise HTTPException(
            status_code=409, detail={"code": "V1_CATALOG_ROW_COUNT_MISMATCH"}
        )
    version = CatalogSyncService(
        connection, _allow_unbound_sources=True
    ).import_full_snapshot(
        school_id=candidate["school_id"],
        source_type=SourceType.DLS_EXCEL,
        records=records,
        confirm_anomaly=True,
        _allow_unbound_source=True,
    )
    now = format_utc(utc_now())
    connection.execute(
        """
        UPDATE v1_catalog_candidates
        SET status = 'ACTIVATED', catalog_version_id = ?, activated_at = ?
        WHERE id = ? AND school_id = ? AND status = 'PENDING_CONFIRMATION'
        """,
        (version.id, now, candidate_id, user.school_id),
    )
    result = {
        "id": candidate_id,
        "status": "ACTIVATED",
        "catalog_version_id": version.id,
        "confirmed_row_count": payload.confirmed_row_count,
    }
    record_audit_event(
        connection,
        actor_id=user.id,
        school_id=user.school_id,
        action="V1_CATALOG_CANDIDATE_ACTIVATED",
        entity_type="v1_catalog_candidate",
        entity_id=candidate_id,
        before={"status": candidate["status"], "row_count": candidate["row_count"]},
        after=result,
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
    record_audit_event(
        connection,
        actor_id=user.id,
        school_id=user.school_id,
        action="DATABASE_BACKUP_CREATED",
        entity_type="backup_manifest",
        entity_id=manifest.id,
        before={"exists": False},
        after=result,
        request_id=request_id,
    )
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
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
):
    settings = request.app.state.settings
    decoded = decode_cursor(cursor, 2)
    items = [
        manifest.to_dict()
        for manifest in BackupService(
            settings.database_path, settings.backups_dir
        ).list()
    ]
    if decoded:
        items = [
            item
            for item in items
            if (item["created_at"], item["id"]) < (decoded[0], decoded[1])
        ]
    return page(
        items,
        limit=limit,
        cursor_values=lambda item: (item["created_at"], item["id"]),
    )


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
    request_body = payload.model_dump()
    operation_key = hashlib.sha256(
        json.dumps(
            {
                "school_id": user.school_id,
                "route": "POST /api/v2/admin/restores",
                "idempotency_key": idempotency_key,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    request_fingerprint = hashlib.sha256(
        json.dumps(
            request_body,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    try:
        result = service.restore(
            manifest_path,
            confirmation_token=confirmation,
            expected_confirmation_token=request.app.state.local_admin_confirmation_token,
            local_request=local_request,
            operation_key=operation_key,
            request_fingerprint=request_fingerprint,
        )
    except LocalAdminConfirmationRequired as error:
        raise HTTPException(
            status_code=403, detail={"code": "LOCAL_ADMIN_CONFIRMATION_REQUIRED"}
        ) from error
    except (RestoreVerificationError, FileNotFoundError) as error:
        raise HTTPException(
            status_code=400, detail={"code": "RESTORE_VERIFICATION_FAILED"}
        ) from error
    except RestoreReplayConflict as error:
        raise HTTPException(
            status_code=409, detail={"code": "IDEMPOTENCY_KEY_REUSED"}
        ) from error
    body = {
        "restored": result.restored_manifest.to_dict(),
        "pre_restore_backup": result.pre_restore_backup.to_dict(),
    }
    if not result.replayed:
        restored_connection = connect(settings.database_path)
        try:
            school = restored_connection.execute(
                "SELECT id FROM schools WHERE id = ?", (user.school_id,)
            ).fetchone()
            if school is not None:
                record_system_audit_event(
                    restored_connection,
                    school_id=user.school_id,
                    action="DATABASE_RESTORED",
                    entity_type="backup_manifest",
                    entity_id=result.restored_manifest.id,
                    before={"pre_restore_backup_id": result.pre_restore_backup.id},
                    after={
                        "restored_backup_id": result.restored_manifest.id,
                        "requested_by_actor_id": user.id,
                    },
                    request_id=request_id,
                )
                restored_connection.commit()
        finally:
            restored_connection.close()
    return body
