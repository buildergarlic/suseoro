from __future__ import annotations

import json
import sqlite3
import uuid

import pytest
from workflow_fixtures import NOW, make_workflow_fixture


def test_exact_state_table_allows_only_the_documented_edges() -> None:
    from suseoro.workflow.states import WorkflowState, is_transition_allowed

    allowed = {
        ("DRAFT", "ANALYZING"),
        ("ANALYZING", "CANDIDATE_REVIEW"),
        ("CANDIDATE_REVIEW", "APPROVAL_PENDING"),
        ("APPROVAL_PENDING", "CANDIDATE_REVIEW"),
        ("APPROVAL_PENDING", "CHANGES_REQUESTED"),
        ("CHANGES_REQUESTED", "APPROVAL_PENDING"),
        ("APPROVAL_PENDING", "APPROVED"),
        ("APPROVED", "QUOTE_REVIEW"),
        ("QUOTE_REVIEW", "APPROVAL_PENDING"),
        ("QUOTE_REVIEW", "ORDER_READY"),
        ("ORDER_READY", "APPROVAL_PENDING"),
        ("ORDER_READY", "ORDER_SENT"),
        ("ORDER_SENT", "APPROVAL_PENDING"),
        ("ORDER_SENT", "ORDER_READY"),
        ("ORDER_SENT", "RECEIVING"),
        ("RECEIVING", "APPROVAL_PENDING"),
        ("RECEIVING", "ORDER_READY"),
        ("RECEIVING", "COMPLETED"),
    }
    states = [state.value for state in WorkflowState]
    assert states == [
        "DRAFT",
        "ANALYZING",
        "CANDIDATE_REVIEW",
        "APPROVAL_PENDING",
        "CHANGES_REQUESTED",
        "APPROVED",
        "QUOTE_REVIEW",
        "ORDER_READY",
        "ORDER_SENT",
        "RECEIVING",
        "COMPLETED",
    ]
    for current in states:
        for target in states:
            assert is_transition_allowed(current, target) is (
                (current, target) in allowed
            )


def test_transition_uses_role_school_if_match_idempotency_and_audit(tmp_path) -> None:
    from suseoro.workflow.states import WorkflowRuleError, transition_workspace

    fixture = make_workflow_fixture(tmp_path, state="DRAFT")
    request_id = str(uuid.uuid4())
    first = transition_workspace(
        fixture.connection,
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        target="ANALYZING",
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
        target="ANALYZING",
        submitted_version=1,
        idempotency_key="start-compare",
        request_id=request_id,
        reason="비교 시작",
    )
    assert replay == first
    assert first["state"] == "ANALYZING"
    assert first["row_version"] == 2
    audit = fixture.connection.execute(
        "SELECT before_json, after_json FROM audit_events WHERE action = 'WORKSPACE_TRANSITION'"
    ).fetchall()
    assert len(audit) == 1
    assert json.loads(audit[0]["before_json"])["state"] == "DRAFT"
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


def test_operator_cannot_finish_system_owned_analysis(tmp_path) -> None:
    from suseoro.workflow.states import WorkflowRuleError, transition_workspace

    fixture = make_workflow_fixture(tmp_path, state="ANALYZING")
    with pytest.raises(WorkflowRuleError) as denied:
        transition_workspace(
            fixture.connection,
            school_id=fixture.school_id,
            workspace_id=fixture.workspace_id,
            actor_id=fixture.operator_id,
            actor_roles=("OPERATOR",),
            target="CANDIDATE_REVIEW",
            submitted_version=1,
            idempotency_key="operator-finish-analysis",
            request_id=str(uuid.uuid4()),
            reason="분석 완료 처리",
        )
    assert denied.value.code == "DOMAIN_TRANSITION_REQUIRED"
    assert (
        fixture.connection.execute(
            "SELECT status FROM acquisition_workspaces WHERE id = ?",
            (fixture.workspace_id,),
        ).fetchone()["status"]
        == "ANALYZING"
    )
    assert fixture.workspace_version() == 1


def test_database_requires_successful_comparison_before_candidate_review(
    tmp_path,
) -> None:
    fixture = make_workflow_fixture(tmp_path, state="ANALYZING")
    with pytest.raises(sqlite3.IntegrityError, match="analysis completion required"):
        fixture.connection.execute(
            "UPDATE acquisition_workspaces SET status = 'CANDIDATE_REVIEW' WHERE id = ?",
            (fixture.workspace_id,),
        )
    fixture.connection.rollback()


def test_generic_transition_cannot_walk_business_stages_without_domain_records(
    tmp_path,
) -> None:
    from suseoro.workflow.states import WorkflowRuleError, transition_workspace

    fixture = make_workflow_fixture(tmp_path)
    with pytest.raises(WorkflowRuleError) as bypass:
        transition_workspace(
            fixture.connection,
            school_id=fixture.school_id,
            workspace_id=fixture.workspace_id,
            actor_id=fixture.operator_id,
            actor_roles=("OPERATOR",),
            target="APPROVAL_PENDING",
            submitted_version=fixture.workspace_version(),
            idempotency_key="generic-approval-bypass",
            request_id=str(uuid.uuid4()),
            reason="artifact 없이 이동",
        )
    assert bypass.value.code == "DOMAIN_TRANSITION_REQUIRED"
    assert (
        fixture.connection.execute(
            "SELECT COUNT(*) FROM approval_revisions WHERE workspace_id = ?",
            (fixture.workspace_id,),
        ).fetchone()[0]
        == 0
    )
    with pytest.raises(Exception, match="workflow transition guard"):
        fixture.connection.execute(
            "UPDATE acquisition_workspaces SET status = 'APPROVAL_PENDING' WHERE id = ?",
            (fixture.workspace_id,),
        )
    fixture.connection.rollback()


def test_database_rejects_unknown_workspace_state_code_on_insert(tmp_path) -> None:
    fixture = make_workflow_fixture(tmp_path)
    with pytest.raises(Exception, match="workflow state code"):
        fixture.connection.execute(
            """
            INSERT INTO acquisition_workspaces (
                id, school_id, name, status, created_by_user_id, created_at, updated_at
            ) VALUES (?, ?, 'invalid state', 'REVIEWING', ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                fixture.school_id,
                fixture.operator_id,
                NOW,
                NOW,
            ),
        )
    fixture.connection.rollback()


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
    from suseoro.workflow.approvals import ApprovalService
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

    approvals = ApprovalService(fixture.connection)
    requested = approvals.request_approval(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        workspace_version=fixture.workspace_version(),
        budget_won=50_000,
        reason="상태 검증용 승인 요청",
        idempotency_key="state-guard-request",
        request_id=str(uuid.uuid4()),
    )
    approvals.approve(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        revision_id=requested["revision_id"],
        actor_id=fixture.reviewer_id,
        actor_roles=("REVIEWER",),
        workspace_version=fixture.workspace_version(),
        reason="상태 검증용 승인",
        idempotency_key="state-guard-approve",
        request_id=str(uuid.uuid4()),
    )
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


def test_bulk_candidate_decision_keeps_valid_items_when_other_targets_are_invalid(
    tmp_path,
) -> None:
    from suseoro.workflow.candidates import CandidateService

    fixture = make_workflow_fixture(tmp_path)
    valid_id = fixture.add_candidate(
        title="적용", author="저자", isbn="9788937464010", outcome="NEEDS_REVIEW"
    )
    invalid_outcome_id = fixture.add_candidate(
        title="잘못된 결과", author="저자", isbn=None, outcome="NEEDS_REVIEW"
    )
    invalid_reason_id = fixture.add_candidate(
        title="사유 없음", author="저자", isbn=None, outcome="NEEDS_REVIEW"
    )
    service = CandidateService(fixture.connection)
    items = [
        {
            "id": invalid_outcome_id,
            "submitted_version": 1,
            "outcome": "NOT_A_DECISION",
            "reason": "형식 오류",
        },
        {
            "id": valid_id,
            "submitted_version": 1,
            "outcome": "CANDIDATE",
            "reason": "적용 가능",
        },
        {
            "id": invalid_reason_id,
            "submitted_version": 1,
            "outcome": "EXCLUDED",
            "reason": " ",
        },
    ]
    result = service.bulk_decide(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        items=items,
        idempotency_key="bulk-partial-invalid",
        request_id=str(uuid.uuid4()),
    )
    assert result == [
        {
            "id": invalid_outcome_id,
            "status": "INVALID",
            "code": "INVALID_CANDIDATE_OUTCOME",
            "outcome": "NEEDS_REVIEW",
            "row_version": 1,
        },
        {
            "id": valid_id,
            "status": "APPLIED",
            "outcome": "CANDIDATE",
            "row_version": 2,
        },
        {
            "id": invalid_reason_id,
            "status": "INVALID",
            "code": "MODIFICATION_REASON_REQUIRED",
            "outcome": "NEEDS_REVIEW",
            "row_version": 1,
        },
    ]
    assert (
        service.bulk_decide(
            school_id=fixture.school_id,
            workspace_id=fixture.workspace_id,
            actor_id=fixture.operator_id,
            actor_roles=("OPERATOR",),
            items=items,
            idempotency_key="bulk-partial-invalid",
            request_id=str(uuid.uuid4()),
        )
        == result
    )


def test_bulk_reports_malformed_items_then_applies_later_valid_target(tmp_path) -> None:
    from suseoro.workflow.candidates import CandidateService

    fixture = make_workflow_fixture(tmp_path)
    candidate_id = fixture.add_candidate(
        title="정상 대상",
        author="저자",
        isbn="9788937464010",
        outcome="NEEDS_REVIEW",
    )
    service = CandidateService(fixture.connection)
    items = [
        None,
        {"outcome": "CANDIDATE", "submitted_version": 1, "reason": "식별자 없음"},
        {
            "id": candidate_id,
            "outcome": "CANDIDATE",
            "submitted_version": 1,
            "reason": "복본 아님",
        },
    ]
    result = service.bulk_decide(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        items=items,
        idempotency_key="bulk-malformed-isolation",
        request_id=str(uuid.uuid4()),
    )
    assert result == [
        {
            "id": None,
            "status": "INVALID",
            "code": "INVALID_BULK_ITEM",
            "outcome": None,
            "row_version": None,
        },
        {
            "id": None,
            "status": "INVALID",
            "code": "CANDIDATE_ID_REQUIRED",
            "outcome": None,
            "row_version": None,
        },
        {
            "id": candidate_id,
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
            items=items,
            idempotency_key="bulk-malformed-isolation",
            request_id=str(uuid.uuid4()),
        )
        == result
    )
    assert (
        fixture.connection.execute(
            "SELECT COUNT(*) FROM audit_events WHERE action = 'CANDIDATE_BULK_DECISION'"
        ).fetchone()[0]
        == 1
    )
