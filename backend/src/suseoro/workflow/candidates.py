"""Small autosave and conflict-preserving bulk candidate commands."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Any

from suseoro.security.sessions import format_utc, utc_now
from suseoro.services.audit import record_audit_event
from suseoro.services.concurrency import (
    VersionConflict,
    require_edit_lock,
    update_with_version,
)
from suseoro.workflow._common import (
    WorkflowDomainError,
    idempotent_mutation,
    require_role,
)

_OUTCOMES = {"CANDIDATE", "NEEDS_REVIEW", "EXCLUDED"}


class CandidateWorkflowError(WorkflowDomainError):
    pass


def _validated_changes(changes: dict[str, Any]) -> dict[str, Any]:
    allowed = {"outcome", "quantity", "unit_price"}
    if not changes or set(changes) - allowed:
        raise CandidateWorkflowError("INVALID_CANDIDATE_CHANGE")
    normalized = dict(changes)
    if "outcome" in normalized and normalized["outcome"] not in _OUTCOMES:
        raise CandidateWorkflowError("INVALID_CANDIDATE_OUTCOME")
    for name in ("quantity", "unit_price"):
        if name in normalized:
            value = normalized[name]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise CandidateWorkflowError(f"INVALID_{name.upper()}")
    return normalized


class CandidateService:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def _authorize(
        self, school_id: str, actor_id: str, actor_roles: tuple[str, ...]
    ) -> None:
        require_role(
            self.connection,
            school_id=school_id,
            actor_id=actor_id,
            actor_roles=actor_roles,
            role="OPERATOR",
            error_type=CandidateWorkflowError,
        )

    def _candidate(self, school_id: str, workspace_id: str, candidate_id: str):
        row = self.connection.execute(
            """
            SELECT * FROM candidate_decisions
            WHERE id = ? AND school_id = ? AND workspace_id = ?
            """,
            (candidate_id, school_id, workspace_id),
        ).fetchone()
        if row is None:
            raise CandidateWorkflowError("CANDIDATE_NOT_FOUND")
        return row

    @staticmethod
    def _result(row) -> dict[str, object]:
        return {
            "id": row["id"],
            "outcome": row["outcome"],
            "quantity": row["quantity"],
            "unit_price": row["unit_price"],
            "row_version": row["row_version"],
        }

    def autosave(
        self,
        *,
        school_id: str,
        workspace_id: str,
        candidate_id: str,
        actor_id: str,
        actor_roles: tuple[str, ...],
        submitted_version: int,
        changes: dict[str, Any],
        reason: str,
        idempotency_key: str,
        request_id: str,
    ) -> dict[str, object]:
        normalized = _validated_changes(changes)
        body = {
            "workspace_id": workspace_id,
            "candidate_id": candidate_id,
            "submitted_version": submitted_version,
            "changes": normalized,
            "reason": reason,
        }

        def mutate() -> dict[str, object]:
            self._authorize(school_id, actor_id, actor_roles)
            if not reason.strip():
                raise CandidateWorkflowError("MODIFICATION_REASON_REQUIRED")
            before = self._candidate(school_id, workspace_id, candidate_id)
            require_edit_lock(
                self.connection,
                school_id=school_id,
                entity_type="candidate_decision",
                entity_id=candidate_id,
                actor_id=actor_id,
            )
            workspace = self.connection.execute(
                "SELECT status FROM acquisition_workspaces WHERE id = ? AND school_id = ?",
                (workspace_id, school_id),
            ).fetchone()
            if workspace is None or workspace["status"] not in {
                "CANDIDATE_REVIEW",
                "CHANGES_REQUESTED",
            }:
                raise CandidateWorkflowError("CANDIDATE_EDIT_STATE_INVALID")
            updated = update_with_version(
                self.connection,
                table="candidate_decisions",
                school_id=school_id,
                entity_id=candidate_id,
                submitted_version=submitted_version,
                changes={
                    **normalized,
                    "reason": reason.strip(),
                    "modified_by_user_id": actor_id,
                    "updated_at": format_utc(utc_now()),
                },
            )
            result = self._result(updated)
            record_audit_event(
                self.connection,
                actor_id=actor_id,
                school_id=school_id,
                action="CANDIDATE_AUTOSAVE",
                entity_type="candidate_decision",
                entity_id=candidate_id,
                before=self._result(before),
                after={**result, "reason": reason.strip()},
                request_id=request_id,
            )
            return result

        return idempotent_mutation(
            self.connection,
            school_id=school_id,
            actor_id=actor_id,
            route="candidates.autosave",
            key=idempotency_key,
            request_body=body,
            operation=mutate,
        )

    def bulk_decide(
        self,
        *,
        school_id: str,
        workspace_id: str,
        actor_id: str,
        actor_roles: tuple[str, ...],
        items: list[object],
        idempotency_key: str,
        request_id: str,
    ) -> list[dict[str, object]]:
        body = {"workspace_id": workspace_id, "items": items}

        def mutate() -> list[dict[str, object]]:
            self._authorize(school_id, actor_id, actor_roles)
            workspace = self.connection.execute(
                """
                SELECT status FROM acquisition_workspaces
                WHERE id = ? AND school_id = ?
                """,
                (workspace_id, school_id),
            ).fetchone()
            if workspace is None:
                raise CandidateWorkflowError("WORKSPACE_NOT_FOUND")
            if workspace["status"] not in {"CANDIDATE_REVIEW", "CHANGES_REQUESTED"}:
                raise CandidateWorkflowError("CANDIDATE_EDIT_STATE_INVALID")
            results: list[dict[str, object]] = []
            for item in items:
                if not isinstance(item, Mapping):
                    results.append(
                        {
                            "id": None,
                            "status": "INVALID",
                            "code": "INVALID_BULK_ITEM",
                            "outcome": None,
                            "row_version": None,
                        }
                    )
                    continue
                raw_candidate_id = item.get("id")
                candidate_id = (
                    str(raw_candidate_id).strip()
                    if raw_candidate_id is not None
                    else ""
                )
                if not candidate_id:
                    results.append(
                        {
                            "id": None,
                            "status": "INVALID",
                            "code": "CANDIDATE_ID_REQUIRED",
                            "outcome": None,
                            "row_version": None,
                        }
                    )
                    continue
                before = self.connection.execute(
                    """
                    SELECT * FROM candidate_decisions
                    WHERE id = ? AND school_id = ? AND workspace_id = ?
                    """,
                    (candidate_id, school_id, workspace_id),
                ).fetchone()
                if before is None:
                    results.append(
                        {
                            "id": candidate_id,
                            "status": "NOT_FOUND",
                            "outcome": None,
                            "row_version": None,
                        }
                    )
                    continue
                require_edit_lock(
                    self.connection,
                    school_id=school_id,
                    entity_type="candidate_decision",
                    entity_id=candidate_id,
                    actor_id=actor_id,
                )
                try:
                    reason = str(item.get("reason") or "").strip()
                    if not reason:
                        raise CandidateWorkflowError("MODIFICATION_REASON_REQUIRED")
                    changes = _validated_changes({"outcome": item.get("outcome")})
                    submitted_version = int(item["submitted_version"])
                except (
                    CandidateWorkflowError,
                    KeyError,
                    TypeError,
                    ValueError,
                ) as error:
                    code = (
                        error.code
                        if isinstance(error, CandidateWorkflowError)
                        else "INVALID_ROW_VERSION"
                    )
                    results.append(
                        {
                            "id": candidate_id,
                            "status": "INVALID",
                            "code": code,
                            "outcome": before["outcome"],
                            "row_version": before["row_version"],
                        }
                    )
                    continue
                try:
                    updated = update_with_version(
                        self.connection,
                        table="candidate_decisions",
                        school_id=school_id,
                        entity_id=candidate_id,
                        submitted_version=submitted_version,
                        changes={
                            **changes,
                            "reason": reason,
                            "modified_by_user_id": actor_id,
                            "updated_at": format_utc(utc_now()),
                        },
                    )
                except VersionConflict:
                    current = self._candidate(school_id, workspace_id, candidate_id)
                    results.append(
                        {
                            "id": candidate_id,
                            "status": "CONFLICT",
                            "outcome": current["outcome"],
                            "row_version": current["row_version"],
                        }
                    )
                    continue
                result = {
                    "id": candidate_id,
                    "status": "APPLIED",
                    "outcome": updated["outcome"],
                    "row_version": updated["row_version"],
                }
                results.append(result)
                record_audit_event(
                    self.connection,
                    actor_id=actor_id,
                    school_id=school_id,
                    action="CANDIDATE_BULK_DECISION",
                    entity_type="candidate_decision",
                    entity_id=candidate_id,
                    before=self._result(before),
                    after={**result, "reason": reason},
                    request_id=request_id,
                )
            return results

        return idempotent_mutation(
            self.connection,
            school_id=school_id,
            actor_id=actor_id,
            route="candidates.bulk",
            key=idempotency_key,
            request_body=body,
            operation=mutate,
        )
