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
    DRAFT = "DRAFT"
    ANALYZING = "ANALYZING"
    CANDIDATE_REVIEW = "CANDIDATE_REVIEW"
    APPROVAL_PENDING = "APPROVAL_PENDING"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    APPROVED = "APPROVED"
    QUOTE_REVIEW = "QUOTE_REVIEW"
    ORDER_READY = "ORDER_READY"
    ORDER_SENT = "ORDER_SENT"
    RECEIVING = "RECEIVING"
    COMPLETED = "COMPLETED"


_ALLOWED = frozenset(
    {
        (WorkflowState.DRAFT, WorkflowState.ANALYZING),
        (WorkflowState.ANALYZING, WorkflowState.CANDIDATE_REVIEW),
        (WorkflowState.CANDIDATE_REVIEW, WorkflowState.APPROVAL_PENDING),
        (WorkflowState.APPROVAL_PENDING, WorkflowState.CANDIDATE_REVIEW),
        (WorkflowState.APPROVAL_PENDING, WorkflowState.CHANGES_REQUESTED),
        (WorkflowState.CHANGES_REQUESTED, WorkflowState.APPROVAL_PENDING),
        (WorkflowState.APPROVAL_PENDING, WorkflowState.APPROVED),
        (WorkflowState.APPROVED, WorkflowState.QUOTE_REVIEW),
        (WorkflowState.QUOTE_REVIEW, WorkflowState.APPROVAL_PENDING),
        (WorkflowState.QUOTE_REVIEW, WorkflowState.ORDER_READY),
        (WorkflowState.ORDER_READY, WorkflowState.APPROVAL_PENDING),
        (WorkflowState.ORDER_READY, WorkflowState.ORDER_SENT),
        (WorkflowState.ORDER_SENT, WorkflowState.APPROVAL_PENDING),
        (WorkflowState.ORDER_SENT, WorkflowState.ORDER_READY),
        (WorkflowState.ORDER_SENT, WorkflowState.RECEIVING),
        (WorkflowState.RECEIVING, WorkflowState.APPROVAL_PENDING),
        (WorkflowState.RECEIVING, WorkflowState.ORDER_READY),
        (WorkflowState.RECEIVING, WorkflowState.COMPLETED),
    }
)

_BOOTSTRAP_EDGES = frozenset(
    {
        (WorkflowState.DRAFT, WorkflowState.ANALYZING),
        (WorkflowState.ANALYZING, WorkflowState.CANDIDATE_REVIEW),
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
    try:
        target_state = WorkflowState(target)
    except ValueError as error:
        raise WorkflowRuleError("ILLEGAL_STATE_TRANSITION") from error
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
        try:
            edge = (WorkflowState(workspace["status"]), target_state)
        except ValueError as error:
            raise WorkflowRuleError("ILLEGAL_STATE_TRANSITION") from error
        if edge not in _ALLOWED:
            raise WorkflowRuleError("ILLEGAL_STATE_TRANSITION")
        if edge not in _BOOTSTRAP_EDGES:
            raise WorkflowRuleError("DOMAIN_TRANSITION_REQUIRED")
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
