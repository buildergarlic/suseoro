from __future__ import annotations

import hashlib
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from workflow_fixtures import make_workflow_fixture

from suseoro.db.connection import connect


def _request(fixture, *, actor_id=None, key="approval-request", reason="최초 요청"):
    from suseoro.workflow.approvals import ApprovalService

    return ApprovalService(fixture.connection).request_approval(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        actor_id=actor_id or fixture.operator_id,
        actor_roles=("OPERATOR",),
        workspace_version=fixture.workspace_version(),
        candidate_collection_revision=fixture.candidate_collection_revision(),
        budget_won=50_000,
        reason=reason,
        idempotency_key=key,
        request_id=str(uuid.uuid4()),
    )


def test_unresolved_needs_review_blocks_approval_request(tmp_path) -> None:
    from suseoro.workflow.approvals import ApprovalError

    fixture = make_workflow_fixture(tmp_path)
    fixture.add_candidate(
        title="확인 책", author="저자", isbn=None, outcome="NEEDS_REVIEW"
    )
    with pytest.raises(ApprovalError) as blocked:
        _request(fixture)
    assert blocked.value.code == "UNRESOLVED_CANDIDATES"


def test_approval_revision_is_canonical_hashed_and_immutable(tmp_path) -> None:
    fixture = make_workflow_fixture(tmp_path)
    candidate_id = fixture.add_candidate(
        title="파친코",
        author="이민진",
        isbn="9788937464010",
        quantity=2,
        unit_price=12_000,
    )
    requested = _request(fixture)
    assert requested["state"] == "APPROVAL_PENDING"
    assert requested["expected_total_won"] == 24_000
    revision = fixture.connection.execute(
        "SELECT * FROM approval_revisions WHERE id = ?", (requested["revision_id"],)
    ).fetchone()
    assert (
        hashlib.sha256(revision["canonical_json"].encode("utf-8")).hexdigest()
        == revision["sha256"]
    )
    payload = json.loads(revision["canonical_json"])
    assert payload == {
        "budget_won": 50_000,
        "candidate_collection_revision": fixture.candidate_collection_revision(),
        "candidates": [
            {
                "author": "이민진",
                "candidate_id": candidate_id,
                "edition": None,
                "isbn13": "9788937464010",
                "quantity": 2,
                "title": "파친코",
                "unit_price": 12_000,
            }
        ],
        "expected_total_won": 24_000,
        "review_snapshot": {
            "auto_excluded_count": 0,
            "auto_exclusions": [],
            "catalog_as_of_local_date": None,
            "source_counts_verified": True,
            "unresolved_complete": True,
        },
    }
    with pytest.raises(Exception, match="scope mismatch"):
        fixture.connection.execute(
            """
            INSERT INTO approval_comments (
                id, approval_revision_id, school_id, actor_id, content, created_at
            ) VALUES (?, ?, ?, ?, 'cross-school', '2026-08-28T00:00:00.000000Z')
            """,
            (
                str(uuid.uuid4()),
                requested["revision_id"],
                fixture.other_school_id,
                fixture.other_operator_id,
            ),
        )
    fixture.connection.rollback()
    with pytest.raises(Exception, match="immutable"):
        fixture.connection.execute(
            "UPDATE approval_revisions SET budget_won = 1 WHERE id = ?",
            (requested["revision_id"],),
        )
    fixture.connection.rollback()
    with pytest.raises(Exception, match="immutable"):
        fixture.connection.execute(
            "DELETE FROM approval_rows WHERE approval_revision_id = ?",
            (requested["revision_id"],),
        )


def test_reviewer_view_comment_and_decision_enforce_self_approval_policy(
    tmp_path,
) -> None:
    from suseoro.workflow.approvals import ApprovalError, ApprovalService

    fixture = make_workflow_fixture(tmp_path)
    fixture.add_candidate(title="책", author="저자", isbn="9788937464010")
    requested = _request(fixture, actor_id=fixture.dual_role_id)
    service = ApprovalService(fixture.connection)
    view = service.view_revision(
        school_id=fixture.school_id,
        revision_id=requested["revision_id"],
        actor_id=fixture.reviewer_id,
        actor_roles=("REVIEWER",),
    )
    assert view["sha256"] == requested["sha256"]
    comment = service.comment(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        revision_id=requested["revision_id"],
        actor_id=fixture.reviewer_id,
        actor_roles=("REVIEWER",),
        workspace_version=fixture.workspace_version(),
        content="예산 근거 확인",
        reason="승인 검토 의견",
        idempotency_key="comment-1",
        request_id=str(uuid.uuid4()),
    )
    assert comment["content"] == "예산 근거 확인"
    assert comment["row_version"] == 3
    with pytest.raises(ApprovalError) as denied:
        service.approve(
            school_id=fixture.school_id,
            workspace_id=fixture.workspace_id,
            revision_id=requested["revision_id"],
            actor_id=fixture.dual_role_id,
            actor_roles=("REVIEWER",),
            workspace_version=fixture.workspace_version(),
            reason="본인 승인",
            idempotency_key="self-approve",
            request_id=str(uuid.uuid4()),
        )
    assert denied.value.code == "SELF_APPROVAL_FORBIDDEN"
    rejected = service.reject(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        revision_id=requested["revision_id"],
        actor_id=fixture.dual_role_id,
        actor_roles=("REVIEWER",),
        workspace_version=fixture.workspace_version(),
        reason="본인 요청을 스스로 철회",
        idempotency_key="self-reject",
        request_id=str(uuid.uuid4()),
    )
    assert rejected["state"] == "CHANGES_REQUESTED"


def test_pending_request_can_be_cancelled_and_comments_require_current_version(
    tmp_path,
) -> None:
    from suseoro.services.concurrency import VersionConflict
    from suseoro.workflow.approvals import ApprovalError, ApprovalService

    fixture = make_workflow_fixture(tmp_path)
    fixture.add_candidate(title="책", author="저자", isbn="9788937464010")
    requested = _request(fixture)
    service = ApprovalService(fixture.connection)
    comment = service.comment(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        revision_id=requested["revision_id"],
        actor_id=fixture.reviewer_id,
        actor_roles=("REVIEWER",),
        workspace_version=requested["row_version"],
        content="현재 승인본 의견",
        reason="검토 의견",
        idempotency_key="versioned-comment",
        request_id=str(uuid.uuid4()),
    )
    assert comment["row_version"] == requested["row_version"] + 1
    with pytest.raises(VersionConflict):
        service.comment(
            school_id=fixture.school_id,
            workspace_id=fixture.workspace_id,
            revision_id=requested["revision_id"],
            actor_id=fixture.reviewer_id,
            actor_roles=("REVIEWER",),
            workspace_version=requested["row_version"],
            content="stale 의견",
            reason="stale",
            idempotency_key="stale-comment",
            request_id=str(uuid.uuid4()),
        )
    cancelled = service.cancel_request(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        revision_id=requested["revision_id"],
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        workspace_version=fixture.workspace_version(),
        reason="목록을 다시 수정",
        idempotency_key="cancel-approval",
        request_id=str(uuid.uuid4()),
    )
    assert cancelled["state"] == "CANDIDATE_REVIEW"
    assert (
        fixture.connection.execute(
            "SELECT COUNT(*) FROM approval_cancellations WHERE approval_revision_id = ?",
            (requested["revision_id"],),
        ).fetchone()[0]
        == 1
    )
    with pytest.raises(ApprovalError) as not_pending:
        service.comment(
            school_id=fixture.school_id,
            workspace_id=fixture.workspace_id,
            revision_id=requested["revision_id"],
            actor_id=fixture.reviewer_id,
            actor_roles=("REVIEWER",),
            workspace_version=fixture.workspace_version(),
            content="취소 후 의견",
            reason="취소됨",
            idempotency_key="comment-cancelled",
            request_id=str(uuid.uuid4()),
        )
    assert not_pending.value.code == "APPROVAL_COMMENT_STATE_INVALID"


def test_single_operator_mode_allows_self_approval_and_first_decision_wins(
    tmp_path,
) -> None:
    from suseoro.workflow.approvals import ApprovalError, ApprovalService

    fixture = make_workflow_fixture(tmp_path, single_operator_mode=True)
    fixture.add_candidate(title="책", author="저자", isbn="9788937464010")
    requested = _request(fixture, actor_id=fixture.dual_role_id)
    service = ApprovalService(fixture.connection)
    approved = service.approve(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        revision_id=requested["revision_id"],
        actor_id=fixture.dual_role_id,
        actor_roles=("REVIEWER",),
        workspace_version=fixture.workspace_version(),
        reason="1인 운영 승인",
        idempotency_key="approve-once",
        request_id=str(uuid.uuid4()),
    )
    assert approved["state"] == "APPROVED"
    with pytest.raises(ApprovalError) as second:
        service.reject(
            school_id=fixture.school_id,
            workspace_id=fixture.workspace_id,
            revision_id=requested["revision_id"],
            actor_id=fixture.reviewer_id,
            actor_roles=("REVIEWER",),
            workspace_version=fixture.workspace_version(),
            reason="뒤늦은 반려",
            idempotency_key="reject-late",
            request_id=str(uuid.uuid4()),
        )
    assert second.value.code == "APPROVAL_ALREADY_DECIDED"


def test_concurrent_approval_and_rejection_commit_exactly_one_decision(
    tmp_path,
) -> None:
    from suseoro.workflow.approvals import ApprovalError, ApprovalService

    fixture = make_workflow_fixture(tmp_path)
    fixture.add_candidate(title="책", author="저자", isbn="9788937464010")
    requested = _request(fixture)
    database_path = Path(
        fixture.connection.execute("PRAGMA database_list").fetchone()["file"]
    )
    barrier = Barrier(2)

    def decide(actor_id: str, action: str) -> str:
        connection = connect(database_path)
        try:
            barrier.wait(timeout=5)
            service = ApprovalService(connection)
            try:
                getattr(service, action)(
                    school_id=fixture.school_id,
                    workspace_id=fixture.workspace_id,
                    revision_id=requested["revision_id"],
                    actor_id=actor_id,
                    actor_roles=("REVIEWER",),
                    workspace_version=requested["row_version"],
                    reason=f"동시 {action}",
                    idempotency_key=f"race-{action}",
                    request_id=str(uuid.uuid4()),
                )
            except ApprovalError as error:
                return error.code
            return "SUCCESS"
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda item: decide(*item),
                (
                    (fixture.reviewer_id, "approve"),
                    (fixture.dual_role_id, "reject"),
                ),
            )
        )
    assert sorted(results) == ["APPROVAL_ALREADY_DECIDED", "SUCCESS"]
    assert (
        fixture.connection.execute(
            "SELECT COUNT(*) AS n FROM approval_decisions WHERE approval_revision_id = ?",
            (requested["revision_id"],),
        ).fetchone()["n"]
        == 1
    )


def test_rejection_requires_reason_and_re_request_creates_revision(tmp_path) -> None:
    from suseoro.workflow.approvals import ApprovalError, ApprovalService

    fixture = make_workflow_fixture(tmp_path)
    fixture.add_candidate(title="책", author="저자", isbn="9788937464010")
    first = _request(fixture)
    service = ApprovalService(fixture.connection)
    with pytest.raises(ApprovalError) as missing:
        service.reject(
            school_id=fixture.school_id,
            workspace_id=fixture.workspace_id,
            revision_id=first["revision_id"],
            actor_id=fixture.reviewer_id,
            actor_roles=("REVIEWER",),
            workspace_version=fixture.workspace_version(),
            reason="",
            idempotency_key="reject-empty",
            request_id=str(uuid.uuid4()),
        )
    assert missing.value.code == "MODIFICATION_REASON_REQUIRED"
    rejected = service.reject(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        revision_id=first["revision_id"],
        actor_id=fixture.reviewer_id,
        actor_roles=("REVIEWER",),
        workspace_version=fixture.workspace_version(),
        reason="수량 근거 보완",
        idempotency_key="reject-valid",
        request_id=str(uuid.uuid4()),
    )
    assert rejected["state"] == "CHANGES_REQUESTED"
    second = _request(fixture, key="approval-rerequest", reason="수량 근거를 보완함")
    assert second["revision_number"] == 2
    assert second["revision_id"] != first["revision_id"]


@pytest.mark.parametrize(
    ("changes", "budget_won", "requires_reapproval"),
    [
        ({"quantity": 2}, 50_000, True),
        ({"unit_price": 60_000}, 50_000, True),
        ({"outcome": "EXCLUDED"}, 50_000, False),
        ({"quantity": 0}, 50_000, False),
        ({"unit_price": 9_000}, 50_000, False),
        ({"quantity": 1}, 60_000, True),
    ],
)
def test_post_approval_scope_changes_trigger_only_required_reapproval(
    tmp_path, changes, budget_won, requires_reapproval
) -> None:
    from suseoro.workflow.approvals import ApprovalService

    fixture = make_workflow_fixture(tmp_path)
    candidate_id = fixture.add_candidate(
        title="책", author="저자", isbn="9788937464010", quantity=1, unit_price=10_000
    )
    requested = _request(fixture)
    service = ApprovalService(fixture.connection)
    service.approve(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        revision_id=requested["revision_id"],
        actor_id=fixture.reviewer_id,
        actor_roles=("REVIEWER",),
        workspace_version=fixture.workspace_version(),
        reason="승인",
        idempotency_key="approve",
        request_id=str(uuid.uuid4()),
    )
    result = service.adjust_approved_candidate(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        candidate_id=candidate_id,
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        candidate_version=1,
        workspace_version=fixture.workspace_version(),
        changes=changes,
        budget_won=budget_won,
        reason="승인 뒤 조정",
        idempotency_key="adjust",
        request_id=str(uuid.uuid4()),
    )
    assert result["reapproval_required"] is requires_reapproval
    assert result["state"] == (
        "APPROVAL_PENDING" if requires_reapproval else "APPROVED"
    )
    if not requires_reapproval:
        assert (
            fixture.connection.execute(
                "SELECT COUNT(*) AS n FROM approval_revisions WHERE workspace_id = ?",
                (fixture.workspace_id,),
            ).fetchone()["n"]
            == 1
        )


def test_downward_adjustment_during_quote_review_keeps_the_current_stage(
    tmp_path,
) -> None:
    from suseoro.workflow.approvals import ApprovalService
    from suseoro.workflow.quotes import QuoteService

    fixture = make_workflow_fixture(tmp_path)
    candidate_id = fixture.add_candidate(
        title="책", author="저자", isbn="9788937464010", quantity=1, unit_price=10_000
    )
    requested = _request(fixture)
    approval = ApprovalService(fixture.connection)
    approval.approve(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        revision_id=requested["revision_id"],
        actor_id=fixture.reviewer_id,
        actor_roles=("REVIEWER",),
        workspace_version=fixture.workspace_version(),
        reason="승인",
        idempotency_key="approve",
        request_id=str(uuid.uuid4()),
    )
    QuoteService(fixture.connection).ingest(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        approval_revision_id=requested["revision_id"],
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        workspace_version=fixture.workspace_version(),
        vendor_name="업체",
        rows=[
            {
                "isbn": "9788937464010",
                "title": "책",
                "author": "저자",
                "quantity": 1,
                "unit_price": 9_000,
            }
        ],
        reason="견적 등록",
        idempotency_key="quote",
        request_id=str(uuid.uuid4()),
    )
    adjusted = approval.adjust_approved_candidate(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        candidate_id=candidate_id,
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        candidate_version=1,
        workspace_version=fixture.workspace_version(),
        changes={"unit_price": 9_000},
        budget_won=50_000,
        reason="견적에 맞춘 하향 조정",
        idempotency_key="adjust-down-in-quote",
        request_id=str(uuid.uuid4()),
    )
    assert adjusted["reapproval_required"] is False
    assert adjusted["state"] == "QUOTE_REVIEW"


def test_post_approval_adjustment_rejects_float_money(tmp_path) -> None:
    from suseoro.workflow.approvals import ApprovalError, ApprovalService

    fixture = make_workflow_fixture(tmp_path)
    candidate_id = fixture.add_candidate(
        title="책", author="저자", isbn="9788937464010", quantity=1, unit_price=10_000
    )
    requested = _request(fixture)
    service = ApprovalService(fixture.connection)
    service.approve(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        revision_id=requested["revision_id"],
        actor_id=fixture.reviewer_id,
        actor_roles=("REVIEWER",),
        workspace_version=fixture.workspace_version(),
        reason="승인",
        idempotency_key="approve",
        request_id=str(uuid.uuid4()),
    )
    with pytest.raises(ApprovalError) as invalid:
        service.adjust_approved_candidate(
            school_id=fixture.school_id,
            workspace_id=fixture.workspace_id,
            candidate_id=candidate_id,
            actor_id=fixture.operator_id,
            actor_roles=("OPERATOR",),
            candidate_version=1,
            workspace_version=fixture.workspace_version(),
            changes={"unit_price": 9_999.5},
            budget_won=50_000,
            reason="부동소수 금액",
            idempotency_key="float-money",
            request_id=str(uuid.uuid4()),
        )
    assert invalid.value.code == "INVALID_UNIT_PRICE"
    stored = fixture.connection.execute(
        "SELECT unit_price, row_version FROM candidate_decisions WHERE id = ?",
        (candidate_id,),
    ).fetchone()
    assert (stored["unit_price"], stored["row_version"]) == (10_000, 1)
