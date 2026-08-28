"""School-scoped, audited production entrypoint for immutable export artifacts."""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path
from typing import Any

from suseoro.exports.dls import store_dls_isbn_artifact, store_dls_title_artifact
from suseoro.security.sessions import format_utc, utc_now
from suseoro.services.audit import record_audit_event
from suseoro.services.concurrency import update_with_version
from suseoro.workflow._common import (
    WorkflowDomainError,
    idempotent_mutation,
    require_role,
)


class ExportArtifactRuleError(WorkflowDomainError):
    pass


_DLS_EXPORT_STATES = {"CANDIDATE_REVIEW", "CHANGES_REQUESTED"}


class ExportArtifactService:
    def __init__(self, connection: sqlite3.Connection, artifact_root: Path) -> None:
        self.connection = connection
        self.artifact_root = artifact_root

    def create_dls_title(
        self,
        *,
        school_id: str,
        workspace_id: str,
        actor_id: str,
        actor_roles: tuple[str, ...],
        workspace_version: int,
        rows: list[dict[str, Any]],
        reason: str,
        idempotency_key: str,
        request_id: str,
    ) -> dict[str, object]:
        return self._create(
            school_id=school_id,
            workspace_id=workspace_id,
            actor_id=actor_id,
            actor_roles=actor_roles,
            workspace_version=workspace_version,
            payload=rows,
            reason=reason,
            idempotency_key=idempotency_key,
            request_id=request_id,
            route="exports.dls-title.create",
            artifact_type="DLS_TITLE_XLSX",
            store=lambda root: store_dls_title_artifact(root, rows),
        )

    def create_dls_isbn(
        self,
        *,
        school_id: str,
        workspace_id: str,
        actor_id: str,
        actor_roles: tuple[str, ...],
        workspace_version: int,
        values: list[object],
        reason: str,
        idempotency_key: str,
        request_id: str,
    ) -> dict[str, object]:
        return self._create(
            school_id=school_id,
            workspace_id=workspace_id,
            actor_id=actor_id,
            actor_roles=actor_roles,
            workspace_version=workspace_version,
            payload=values,
            reason=reason,
            idempotency_key=idempotency_key,
            request_id=request_id,
            route="exports.dls-isbn.create",
            artifact_type="DLS_ISBN_TXT",
            store=lambda root: store_dls_isbn_artifact(root, values),
        )

    def _create(
        self,
        *,
        school_id: str,
        workspace_id: str,
        actor_id: str,
        actor_roles: tuple[str, ...],
        workspace_version: int,
        payload: object,
        reason: str,
        idempotency_key: str,
        request_id: str,
        route: str,
        artifact_type: str,
        store: Any,
    ) -> dict[str, object]:
        body = {
            "workspace_id": workspace_id,
            "workspace_version": workspace_version,
            "payload": payload,
            "reason": reason,
        }
        created_path: Path | None = None

        def mutate() -> dict[str, object]:
            nonlocal created_path
            require_role(
                self.connection,
                school_id=school_id,
                actor_id=actor_id,
                actor_roles=actor_roles,
                role="OPERATOR",
                error_type=ExportArtifactRuleError,
            )
            if not reason.strip():
                raise ExportArtifactRuleError("MODIFICATION_REASON_REQUIRED")
            workspace = self.connection.execute(
                "SELECT * FROM acquisition_workspaces WHERE id = ? AND school_id = ?",
                (workspace_id, school_id),
            ).fetchone()
            if workspace is None:
                raise ExportArtifactRuleError("WORKSPACE_NOT_FOUND")
            if workspace["status"] not in _DLS_EXPORT_STATES:
                raise ExportArtifactRuleError("DLS_EXPORT_STATE_INVALID")
            stored = store(self.artifact_root / school_id / workspace_id)
            created_path = stored.path
            now = format_utc(utc_now())
            artifact_id = str(uuid.uuid4())
            self.connection.execute(
                """
                INSERT INTO generated_artifacts (
                    id, school_id, workspace_id, artifact_type, storage_path,
                    sha256, size_bytes, content_bytes, created_by_user_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    school_id,
                    workspace_id,
                    artifact_type,
                    str(stored.path),
                    stored.sha256,
                    stored.size_bytes,
                    stored.content,
                    actor_id,
                    now,
                ),
            )
            updated = update_with_version(
                self.connection,
                table="acquisition_workspaces",
                school_id=school_id,
                entity_id=workspace_id,
                submitted_version=workspace_version,
                changes={"updated_at": now},
            )
            result = {
                "artifact_id": artifact_id,
                "artifact_type": artifact_type,
                "path": str(stored.path),
                "sha256": stored.sha256,
                "size_bytes": stored.size_bytes,
                "state": updated["status"],
                "row_version": updated["row_version"],
            }
            record_audit_event(
                self.connection,
                actor_id=actor_id,
                school_id=school_id,
                action="EXPORT_ARTIFACT_CREATED",
                entity_type="generated_artifact",
                entity_id=artifact_id,
                before={
                    "state": workspace["status"],
                    "row_version": workspace["row_version"],
                },
                after={**result, "reason": reason.strip()},
                request_id=request_id,
            )
            return result

        try:
            result = idempotent_mutation(
                self.connection,
                school_id=school_id,
                actor_id=actor_id,
                route=route,
                key=idempotency_key,
                request_body=body,
                operation=mutate,
            )
        except BaseException:
            if created_path is not None:
                created_path.unlink(missing_ok=True)
            raise
        return {**result, "path": Path(str(result["path"]))}
