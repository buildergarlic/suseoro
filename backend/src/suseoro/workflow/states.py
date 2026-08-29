"""Exact acquisition state machine and audited transition command."""

from __future__ import annotations

import sqlite3
from enum import Enum

from suseoro.security.sessions import format_utc, utc_now
from suseoro.services.audit import record_audit_event, record_system_audit_event
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
        result = {
            "state": updated["status"],
            "row_version": updated["row_version"],
        }
        record_audit_event(
            connection,
            actor_id=actor_id,
            school_id=school_id,
            action="WORKSPACE_TRANSITION",
            entity_type="acquisition_workspace",
            entity_id=workspace_id,
            before=before,
            after={**result, "reason": reason.strip()},
            request_id=request_id,
        )
        return result

    return idempotent_mutation(
        connection,
        school_id=school_id,
        actor_id=actor_id,
        route="workflow.transition",
        key=idempotency_key,
        request_body=request_body,
        operation=mutate,
    )


def complete_analysis_for_succeeded_job(
    connection: sqlite3.Connection, *, job_id: str
) -> bool:
    """Advance an analysis only when the completed COMPARE job has durable results."""
    completed = connection.execute(
        """
        SELECT j.school_id, j.workspace_id, j.updated_at,
               w.status, w.row_version
        FROM durable_jobs j
        JOIN acquisition_workspaces w
          ON w.id = j.workspace_id AND w.school_id = j.school_id
        WHERE j.id = ?
          AND j.job_type = 'COMPARE'
          AND j.status = 'SUCCEEDED'
          AND j.stage = 'COMPLETED'
          AND j.progress_total IS NOT NULL
          AND j.progress_current = j.progress_total
          AND w.status = 'ANALYZING'
          AND json_type(j.payload_json, '$.source_document_ids') = 'array'
          AND json_array_length(j.payload_json, '$.source_document_ids') > 0
          AND NOT EXISTS (
              SELECT 1
              FROM json_each(j.payload_json, '$.source_document_ids') source
              WHERE NOT EXISTS (
                  SELECT 1
                  FROM comparison_file_results result
                  WHERE result.school_id = j.school_id
                    AND result.workspace_id = j.workspace_id
                    AND result.source_document_id = source.value
                    AND result.status IN ('SUCCESS', 'PARTIAL')
              )
          )
        """,
        (job_id,),
    ).fetchone()
    if completed is None:
        return False
    updated = connection.execute(
        """
        UPDATE acquisition_workspaces
        SET status = 'CANDIDATE_REVIEW', row_version = row_version + 1,
            updated_at = ?
        WHERE id = ? AND school_id = ? AND status = 'ANALYZING'
          AND row_version = ?
        """,
        (
            completed["updated_at"],
            completed["workspace_id"],
            completed["school_id"],
            completed["row_version"],
        ),
    )
    if updated.rowcount != 1:
        return False
    before = {
        "state": completed["status"],
        "row_version": completed["row_version"],
    }
    after = {
        "state": WorkflowState.CANDIDATE_REVIEW.value,
        "row_version": completed["row_version"] + 1,
        "reason": "comparison job completed",
    }
    record_system_audit_event(
        connection,
        school_id=completed["school_id"],
        action="ANALYSIS_COMPLETED",
        entity_type="acquisition_workspace",
        entity_id=completed["workspace_id"],
        before=before,
        after=after,
        request_id=job_id,
    )
    return True
