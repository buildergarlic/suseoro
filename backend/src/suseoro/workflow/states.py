"""Exact acquisition state machine and audited transition command."""

from __future__ import annotations

import sqlite3
from enum import Enum

from suseoro.security.sessions import format_utc, utc_now
from suseoro.services.audit import record_audit_event
from suseoro.services.concurrency import update_with_version
from suseoro.workflow._common import (
    WorkflowDomainError,
    idempotent_mutation,
    require_role,
)


class WorkflowState(str, Enum):
    DATA_PREPARATION = "DATA_PREPARATION"
    COMPARING = "COMPARING"
    CANDIDATE_REVIEW = "CANDIDATE_REVIEW"
    APPROVAL_PENDING = "APPROVAL_PENDING"
    REVISION_REQUESTED = "REVISION_REQUESTED"
    APPROVED = "APPROVED"
    QUOTE_ADJUSTMENT = "QUOTE_ADJUSTMENT"
    ORDER_READY = "ORDER_READY"
    AWAITING_DELIVERY = "AWAITING_DELIVERY"
    RECEIVING = "RECEIVING"
    COMPLETE = "COMPLETE"


_ALLOWED = frozenset(
    {
        (WorkflowState.DATA_PREPARATION, WorkflowState.COMPARING),
        (WorkflowState.COMPARING, WorkflowState.CANDIDATE_REVIEW),
        (WorkflowState.CANDIDATE_REVIEW, WorkflowState.APPROVAL_PENDING),
        (WorkflowState.APPROVAL_PENDING, WorkflowState.REVISION_REQUESTED),
        (WorkflowState.REVISION_REQUESTED, WorkflowState.APPROVAL_PENDING),
        (WorkflowState.APPROVAL_PENDING, WorkflowState.APPROVED),
        (WorkflowState.APPROVED, WorkflowState.QUOTE_ADJUSTMENT),
        (WorkflowState.QUOTE_ADJUSTMENT, WorkflowState.ORDER_READY),
        (WorkflowState.ORDER_READY, WorkflowState.AWAITING_DELIVERY),
        (WorkflowState.AWAITING_DELIVERY, WorkflowState.RECEIVING),
        (WorkflowState.RECEIVING, WorkflowState.COMPLETE),
    }
)


class WorkflowRuleError(WorkflowDomainError):
    pass


def is_transition_allowed(
    current: str | WorkflowState, target: str | WorkflowState
) -> bool:
    try:
        edge = (WorkflowState(current), WorkflowState(target))
    except ValueError:
        return False
    return edge in _ALLOWED


def _role_for_transition(target: WorkflowState) -> str:
    if target in {WorkflowState.APPROVED, WorkflowState.REVISION_REQUESTED}:
        return "REVIEWER"
    return "OPERATOR"


def transition_workspace(
    connection: sqlite3.Connection,
    *,
    school_id: str,
    workspace_id: str,
    actor_id: str,
    actor_roles: tuple[str, ...],
    target: str | WorkflowState,
    submitted_version: int,
    idempotency_key: str,
    request_id: str,
    reason: str,
) -> dict[str, object]:
    target_state = WorkflowState(target)
    request_body = {
        "workspace_id": workspace_id,
        "target": target_state.value,
        "submitted_version": submitted_version,
        "reason": reason,
    }

    def mutate() -> dict[str, object]:
        workspace = connection.execute(
            "SELECT * FROM acquisition_workspaces WHERE id = ? AND school_id = ?",
            (workspace_id, school_id),
        ).fetchone()
        if workspace is None:
            raise WorkflowRuleError("WORKSPACE_NOT_FOUND")
        if not reason.strip():
            raise WorkflowRuleError("MODIFICATION_REASON_REQUIRED")
        if not is_transition_allowed(workspace["status"], target_state):
            raise WorkflowRuleError("ILLEGAL_STATE_TRANSITION")
        require_role(
            connection,
            school_id=school_id,
            actor_id=actor_id,
            actor_roles=actor_roles,
            role=_role_for_transition(target_state),
            error_type=WorkflowRuleError,
        )
        updated = update_with_version(
            connection,
            table="acquisition_workspaces",
            school_id=school_id,
            entity_id=workspace_id,
            submitted_version=submitted_version,
            changes={
                "status": target_state.value,
                "updated_at": format_utc(utc_now()),
            },
        )
        before = {"state": workspace["status"], "row_version": workspace["row_version"]}
        after = {
            "state": updated["status"],
            "row_version": updated["row_version"],
            "reason": reason.strip(),
        }
        record_audit_event(
            connection,
            actor_id=actor_id,
            school_id=school_id,
            action="WORKSPACE_TRANSITION",
            entity_type="acquisition_workspace",
            entity_id=workspace_id,
            before=before,
            after=after,
            request_id=request_id,
        )
        return after

    return idempotent_mutation(
        connection,
        school_id=school_id,
        actor_id=actor_id,
        route="workflow.transition",
        key=idempotency_key,
        request_body=request_body,
        operation=mutate,
    )
