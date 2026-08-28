from __future__ import annotations

import json
import uuid

import pytest
from workflow_fixtures import make_workflow_fixture


def test_exact_state_table_allows_only_the_documented_edges() -> None:
    from suseoro.workflow.states import WorkflowState, is_transition_allowed

    allowed = {
        ("DATA_PREPARATION", "COMPARING"),
        ("COMPARING", "CANDIDATE_REVIEW"),
        ("CANDIDATE_REVIEW", "APPROVAL_PENDING"),
        ("APPROVAL_PENDING", "REVISION_REQUESTED"),
        ("REVISION_REQUESTED", "APPROVAL_PENDING"),
        ("APPROVAL_PENDING", "APPROVED"),
        ("APPROVED", "QUOTE_ADJUSTMENT"),
        ("QUOTE_ADJUSTMENT", "ORDER_READY"),
        ("ORDER_READY", "AWAITING_DELIVERY"),
        ("AWAITING_DELIVERY", "RECEIVING"),
        ("RECEIVING", "COMPLETE"),
    }
    states = [state.value for state in WorkflowState]
    assert states == [
        "DATA_PREPARATION",
        "COMPARING",
        "CANDIDATE_REVIEW",
        "APPROVAL_PENDING",
        "REVISION_REQUESTED",
        "APPROVED",
        "QUOTE_ADJUSTMENT",
        "ORDER_READY",
        "AWAITING_DELIVERY",
        "RECEIVING",
        "COMPLETE",
    ]
    for current in states:
        for target in states:
            assert is_transition_allowed(current, target) is (
                (current, target) in allowed
            )


def test_transition_uses_role_school_if_match_idempotency_and_audit(tmp_path) -> None:
    from suseoro.workflow.states import WorkflowRuleError, transition_workspace

    fixture = make_workflow_fixture(tmp_path, state="DATA_PREPARATION")
    request_id = str(uuid.uuid4())
    first = transition_workspace(
        fixture.connection,
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        target="COMPARING",
        submitted_version=1,
        idempotency_key="start-compare",
        request_id=request_id,
        reason="비교 시작",
    )
    replay = transition_workspace(
        fixture.connection,
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        target="COMPARING",
        submitted_version=1,
        idempotency_key="start-compare",
        request_id=request_id,
        reason="비교 시작",
    )
    assert replay == first
    assert first["state"] == "COMPARING"
    assert first["row_version"] == 2
    audit = fixture.connection.execute(
        "SELECT before_json, after_json FROM audit_events WHERE action = 'WORKSPACE_TRANSITION'"
    ).fetchall()
    assert len(audit) == 1
    assert json.loads(audit[0]["before_json"])["state"] == "DATA_PREPARATION"
    assert json.loads(audit[0]["after_json"])["reason"] == "비교 시작"

    with pytest.raises(WorkflowRuleError) as illegal:
        transition_workspace(
            fixture.connection,
            school_id=fixture.school_id,
            workspace_id=fixture.workspace_id,
            actor_id=fixture.operator_id,
            actor_roles=("OPERATOR",),
            target="APPROVED",
            submitted_version=2,
            idempotency_key="skip",
            request_id=str(uuid.uuid4()),
            reason="건너뛰기",
        )
    assert illegal.value.code == "ILLEGAL_STATE_TRANSITION"

    with pytest.raises(WorkflowRuleError) as wrong_school:
        transition_workspace(
            fixture.connection,
            school_id=fixture.other_school_id,
            workspace_id=fixture.workspace_id,
            actor_id=fixture.other_operator_id,
            actor_roles=("OPERATOR",),
            target="CANDIDATE_REVIEW",
            submitted_version=2,
            idempotency_key="wrong-school",
            request_id=str(uuid.uuid4()),
            reason="타교 변경",
        )
    assert wrong_school.value.code == "WORKSPACE_NOT_FOUND"


def test_candidate_autosave_and_bulk_return_final_outcomes_and_conflicts(
    tmp_path,
) -> None:
    from suseoro.workflow.candidates import CandidateService

    fixture = make_workflow_fixture(tmp_path)
    first_id = fixture.add_candidate(
        title="파친코", author="이민진", isbn="9788937464010", outcome="NEEDS_REVIEW"
    )
    second_id = fixture.add_candidate(
        title="아몬드", author="손원평", isbn="9788936434267", outcome="NEEDS_REVIEW"
    )
    service = CandidateService(fixture.connection)
    saved = service.autosave(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        candidate_id=first_id,
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        submitted_version=1,
        changes={"outcome": "CANDIDATE", "quantity": 2, "unit_price": 12_300},
        reason="복본 아님 확인",
        idempotency_key="autosave-first",
        request_id=str(uuid.uuid4()),
    )
    assert saved == {
        "id": first_id,
        "outcome": "CANDIDATE",
        "quantity": 2,
        "unit_price": 12_300,
        "row_version": 2,
    }
    results = service.bulk_decide(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        items=[
            {
                "id": first_id,
                "submitted_version": 1,
                "outcome": "EXCLUDED",
                "reason": "낡은 화면",
            },
            {
                "id": second_id,
                "submitted_version": 1,
                "outcome": "CANDIDATE",
                "reason": "복본 아님",
            },
        ],
        idempotency_key="bulk-1",
        request_id=str(uuid.uuid4()),
    )
    assert results == [
        {
            "id": first_id,
            "status": "CONFLICT",
            "outcome": "CANDIDATE",
            "row_version": 2,
        },
        {
            "id": second_id,
            "status": "APPLIED",
            "outcome": "CANDIDATE",
            "row_version": 2,
        },
    ]
    assert (
        service.bulk_decide(
            school_id=fixture.school_id,
            workspace_id=fixture.workspace_id,
            actor_id=fixture.operator_id,
            actor_roles=("OPERATOR",),
            items=[
                {
                    "id": first_id,
                    "submitted_version": 1,
                    "outcome": "EXCLUDED",
                    "reason": "낡은 화면",
                },
                {
                    "id": second_id,
                    "submitted_version": 1,
                    "outcome": "CANDIDATE",
                    "reason": "복본 아님",
                },
            ],
            idempotency_key="bulk-1",
            request_id=str(uuid.uuid4()),
        )
        == results
    )


def test_candidate_mutation_rejects_reviewer_and_cross_school_access(tmp_path) -> None:
    from suseoro.workflow.candidates import CandidateService, CandidateWorkflowError

    fixture = make_workflow_fixture(tmp_path)
    candidate_id = fixture.add_candidate(title="책", author="저자", isbn=None)
    service = CandidateService(fixture.connection)
    with pytest.raises(CandidateWorkflowError) as forbidden:
        service.autosave(
            school_id=fixture.school_id,
            workspace_id=fixture.workspace_id,
            candidate_id=candidate_id,
            actor_id=fixture.reviewer_id,
            actor_roles=("REVIEWER",),
            submitted_version=1,
            changes={"quantity": 2},
            reason="권한 없음",
            idempotency_key="reviewer-edit",
            request_id=str(uuid.uuid4()),
        )
    assert forbidden.value.code == "OPERATOR_ROLE_REQUIRED"
    with pytest.raises(CandidateWorkflowError) as hidden:
        service.autosave(
            school_id=fixture.other_school_id,
            workspace_id=fixture.workspace_id,
            candidate_id=candidate_id,
            actor_id=fixture.other_operator_id,
            actor_roles=("OPERATOR",),
            submitted_version=1,
            changes={"quantity": 2},
            reason="타교 변경",
            idempotency_key="cross-school-edit",
            request_id=str(uuid.uuid4()),
        )
    assert hidden.value.code == "CANDIDATE_NOT_FOUND"
    with pytest.raises(Exception, match="scope mismatch"):
        fixture.connection.execute(
            "UPDATE candidate_decisions SET modified_by_user_id = ? WHERE id = ?",
            (fixture.other_operator_id, candidate_id),
        )
    fixture.connection.rollback()


def test_bulk_candidate_decision_is_state_guarded_and_reports_missing_items(
    tmp_path,
) -> None:
    from suseoro.workflow.candidates import CandidateService, CandidateWorkflowError

    fixture = make_workflow_fixture(tmp_path)
    candidate_id = fixture.add_candidate(
        title="남은 책", author="저자", isbn="9788937464010", outcome="NEEDS_REVIEW"
    )
    service = CandidateService(fixture.connection)
    missing_id = str(uuid.uuid4())
    results = service.bulk_decide(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        items=[
            {
                "id": missing_id,
                "submitted_version": 1,
                "outcome": "CANDIDATE",
                "reason": "이미 삭제됨",
            },
            {
                "id": candidate_id,
                "submitted_version": 1,
                "outcome": "CANDIDATE",
                "reason": "복본 아님",
            },
        ],
        idempotency_key="bulk-missing",
        request_id=str(uuid.uuid4()),
    )
    assert results == [
        {"id": missing_id, "status": "NOT_FOUND", "outcome": None, "row_version": None},
        {
            "id": candidate_id,
            "status": "APPLIED",
            "outcome": "CANDIDATE",
            "row_version": 2,
        },
    ]

    fixture.set_state("APPROVED")
    with pytest.raises(CandidateWorkflowError) as state_error:
        service.bulk_decide(
            school_id=fixture.school_id,
            workspace_id=fixture.workspace_id,
            actor_id=fixture.operator_id,
            actor_roles=("OPERATOR",),
            items=[
                {
                    "id": candidate_id,
                    "submitted_version": 2,
                    "outcome": "EXCLUDED",
                    "reason": "승인 뒤 직접 변경",
                }
            ],
            idempotency_key="bulk-wrong-state",
            request_id=str(uuid.uuid4()),
        )
    assert state_error.value.code == "CANDIDATE_EDIT_STATE_INVALID"
