from __future__ import annotations

import hashlib
import sqlite3
import uuid

import pytest
from workflow_fixtures import make_workflow_fixture


def _approved(fixture):
    from suseoro.workflow.approvals import ApprovalService

    service = ApprovalService(fixture.connection)
    requested = service.request_approval(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        workspace_version=fixture.workspace_version(),
        budget_won=40_000,
        reason="승인 요청",
        idempotency_key="request",
        request_id=str(uuid.uuid4()),
    )
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
    return requested


def test_quote_ingest_isbn_first_and_reports_every_mismatch(tmp_path) -> None:
    from suseoro.workflow.quotes import QuoteService

    fixture = make_workflow_fixture(tmp_path)
    fixture.add_candidate(
        title="파친코",
        author="이민진",
        isbn="9788937464010",
        quantity=2,
        unit_price=12_000,
    )
    fixture.add_candidate(
        title="무ISBN 책", author="홍길동", isbn=None, unit_price=8_000
    )
    approved = _approved(fixture)
    result = QuoteService(fixture.connection).ingest(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        approval_revision_id=approved["revision_id"],
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        workspace_version=fixture.workspace_version(),
        vendor_name="좋은책 업체",
        rows=[
            {
                "isbn": "9788937464010",
                "title": "다른 제목",
                "author": "아무개",
                "quantity": 2,
                "unit_price": 11_000,
                "list_price": 12_000,
            },
            {
                "isbn": None,
                "title": "무ISBN 책",
                "author": "홍길동",
                "quantity": 1,
                "unit_price": 7_000,
                "list_price": 8_000,
            },
            {
                "isbn": "9788936434267",
                "title": "미승인 책",
                "author": "저자",
                "quantity": 1,
                "unit_price": None,
                "out_of_stock": True,
            },
        ],
        reason="견적 등록",
        idempotency_key="quote-1",
        request_id=str(uuid.uuid4()),
    )
    assert result["total_won"] == 29_000
    assert result["list_total_won"] == 32_000
    assert result["discount_won"] == 3_000
    assert result["out_of_stock_count"] == 1
    assert result["missing_price_count"] == 1
    assert result["needs_review_count"] == 1
    assert result["unmatched_count"] == 1
    assert result["rows"][0]["match_status"] == "MATCHED_ISBN"
    assert result["rows"][1]["match_status"] == "NEEDS_REVIEW"
    assert result["rows"][2]["match_status"] == "UNMATCHED"


def test_budget_overrun_never_auto_removes_books(tmp_path) -> None:
    from suseoro.workflow.quotes import QuoteService

    fixture = make_workflow_fixture(tmp_path)
    fixture.add_candidate(
        title="책1", author="저자", isbn="9788937464010", unit_price=10_000
    )
    fixture.add_candidate(
        title="책2", author="저자", isbn="9788936434267", unit_price=10_000
    )
    approved = _approved(fixture)
    result = QuoteService(fixture.connection).ingest(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        approval_revision_id=approved["revision_id"],
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        workspace_version=fixture.workspace_version(),
        vendor_name="비싼 업체",
        rows=[
            {
                "isbn": "9788937464010",
                "title": "책1",
                "author": "저자",
                "quantity": 1,
                "unit_price": 30_000,
            },
            {
                "isbn": "9788936434267",
                "title": "책2",
                "author": "저자",
                "quantity": 1,
                "unit_price": 30_000,
            },
        ],
        reason="초과 견적",
        idempotency_key="quote-over",
        request_id=str(uuid.uuid4()),
    )
    assert result["budget_overrun_won"] == 20_000
    assert len(result["rows"]) == 2
    assert result["requires_reapproval"] is True


def test_single_vendor_is_default_and_split_requires_advanced_flag(tmp_path) -> None:
    from suseoro.workflow.quotes import QuoteRuleError, select_vendor_plan

    rows = [
        {"vendor": "A", "approval_row_id": "1", "unit_price": 9_000},
        {"vendor": "B", "approval_row_id": "2", "unit_price": 8_000},
    ]
    assert select_vendor_plan(
        rows, split_order=False, advanced_split_enabled=False
    ) == ["A"]
    with pytest.raises(QuoteRuleError) as disabled:
        select_vendor_plan(rows, split_order=True, advanced_split_enabled=False)
    assert disabled.value.code == "ADVANCED_SPLIT_ORDER_DISABLED"
    assert select_vendor_plan(rows, split_order=True, advanced_split_enabled=True) == [
        "A",
        "B",
    ]


def test_quote_rejects_a_superseded_approval_scope(tmp_path) -> None:
    from suseoro.workflow.approvals import ApprovalService
    from suseoro.workflow.quotes import QuoteRuleError, QuoteService

    fixture = make_workflow_fixture(tmp_path)
    candidate_id = fixture.add_candidate(
        title="책", author="저자", isbn="9788937464010", quantity=1, unit_price=10_000
    )
    first = _approved(fixture)
    approval = ApprovalService(fixture.connection)
    adjusted = approval.adjust_approved_candidate(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        candidate_id=candidate_id,
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        candidate_version=1,
        workspace_version=fixture.workspace_version(),
        changes={"quantity": 2},
        budget_won=40_000,
        reason="두 권으로 늘림",
        idempotency_key="adjust-up",
        request_id=str(uuid.uuid4()),
    )
    approval.approve(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        revision_id=adjusted["revision_id"],
        actor_id=fixture.reviewer_id,
        actor_roles=("REVIEWER",),
        workspace_version=fixture.workspace_version(),
        reason="수정본 승인",
        idempotency_key="approve-revision-2",
        request_id=str(uuid.uuid4()),
    )
    with pytest.raises(QuoteRuleError) as old_scope:
        QuoteService(fixture.connection).ingest(
            school_id=fixture.school_id,
            workspace_id=fixture.workspace_id,
            approval_revision_id=first["revision_id"],
            actor_id=fixture.operator_id,
            actor_roles=("OPERATOR",),
            workspace_version=fixture.workspace_version(),
            vendor_name="과거 승인 업체",
            rows=[
                {
                    "isbn": "9788937464010",
                    "title": "책",
                    "author": "저자",
                    "quantity": 1,
                    "unit_price": 9_000,
                }
            ],
            reason="과거 승인본 사용 시도",
            idempotency_key="quote-old-scope",
            request_id=str(uuid.uuid4()),
        )
    assert old_scope.value.code == "APPROVAL_REVISION_NOT_CURRENT"


def test_order_revision_revalidates_scope_and_never_overwrites_artifact(
    tmp_path,
) -> None:
    from suseoro.workflow.orders import OrderRuleError, OrderService
    from suseoro.workflow.quotes import QuoteService

    fixture = make_workflow_fixture(tmp_path)
    fixture.add_candidate(
        title="책", author="저자", isbn="9788937464010", quantity=1, unit_price=10_000
    )
    approved = _approved(fixture)
    quote = QuoteService(fixture.connection).ingest(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        approval_revision_id=approved["revision_id"],
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
        reason="견적",
        idempotency_key="quote",
        request_id=str(uuid.uuid4()),
    )
    service = OrderService(fixture.connection, tmp_path / "artifacts")
    first = service.generate_revision(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        approval_revision_id=approved["revision_id"],
        quote_id=quote["quote_id"],
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        workspace_version=fixture.workspace_version(),
        reason="발주 생성",
        idempotency_key="order-1",
        request_id=str(uuid.uuid4()),
    )
    first_bytes = first["path"].read_bytes()
    assert hashlib.sha256(first_bytes).hexdigest() == first["sha256"]
    service.mark_sent(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        order_revision_id=first["revision_id"],
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        workspace_version=fixture.workspace_version(),
        reason="업체 전달 확인",
        idempotency_key="sent",
        request_id=str(uuid.uuid4()),
    )
    second = service.generate_revision(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        approval_revision_id=approved["revision_id"],
        quote_id=quote["quote_id"],
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        workspace_version=fixture.workspace_version(),
        reason="전달 뒤 수정본",
        idempotency_key="order-2",
        request_id=str(uuid.uuid4()),
    )
    assert second["revision_id"] != first["revision_id"]
    assert second["path"] != first["path"]
    assert first["path"].read_bytes() == first_bytes
    assert first["path"].exists() and second["path"].exists()

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        fixture.connection.execute(
            "UPDATE vendor_quote_rows SET quantity = 2 WHERE quote_id = ?",
            (quote["quote_id"],),
        )
    fixture.connection.rollback()

    over_fixture = make_workflow_fixture(tmp_path / "scope-overrun")
    over_fixture.add_candidate(
        title="책", author="저자", isbn="9788937464010", quantity=1, unit_price=10_000
    )
    over_approved = _approved(over_fixture)
    over_quote = QuoteService(over_fixture.connection).ingest(
        school_id=over_fixture.school_id,
        workspace_id=over_fixture.workspace_id,
        approval_revision_id=over_approved["revision_id"],
        actor_id=over_fixture.operator_id,
        actor_roles=("OPERATOR",),
        workspace_version=over_fixture.workspace_version(),
        vendor_name="범위 초과 업체",
        rows=[
            {
                "isbn": "9788937464010",
                "title": "책",
                "author": "저자",
                "quantity": 2,
                "unit_price": 9_000,
            }
        ],
        reason="범위 초과 견적",
        idempotency_key="quote-over-scope",
        request_id=str(uuid.uuid4()),
    )
    over_service = OrderService(over_fixture.connection, tmp_path / "over-artifacts")
    with pytest.raises(OrderRuleError) as scope:
        over_service.generate_revision(
            school_id=over_fixture.school_id,
            workspace_id=over_fixture.workspace_id,
            approval_revision_id=over_approved["revision_id"],
            quote_id=over_quote["quote_id"],
            actor_id=over_fixture.operator_id,
            actor_roles=("OPERATOR",),
            workspace_version=over_fixture.workspace_version(),
            reason="범위 초과",
            idempotency_key="order-over",
            request_id=str(uuid.uuid4()),
        )
    assert scope.value.code == "APPROVAL_SCOPE_EXCEEDED"


def test_order_file_is_removed_when_idempotency_completion_rolls_back(
    tmp_path, monkeypatch
) -> None:
    from suseoro.workflow.orders import OrderService
    from suseoro.workflow.quotes import QuoteService

    fixture = make_workflow_fixture(tmp_path)
    fixture.add_candidate(
        title="책", author="저자", isbn="9788937464010", quantity=1, unit_price=10_000
    )
    approved = _approved(fixture)
    quote = QuoteService(fixture.connection).ingest(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        approval_revision_id=approved["revision_id"],
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
        reason="견적",
        idempotency_key="quote-rollback",
        request_id=str(uuid.uuid4()),
    )

    def fail_completion(*args, **kwargs):
        raise RuntimeError("idempotency completion failed")

    monkeypatch.setattr(
        "suseoro.workflow._common.complete_idempotent_request", fail_completion
    )
    artifact_root = tmp_path / "rollback-artifacts"
    with pytest.raises(RuntimeError, match="idempotency completion failed"):
        OrderService(fixture.connection, artifact_root).generate_revision(
            school_id=fixture.school_id,
            workspace_id=fixture.workspace_id,
            approval_revision_id=approved["revision_id"],
            quote_id=quote["quote_id"],
            actor_id=fixture.operator_id,
            actor_roles=("OPERATOR",),
            workspace_version=fixture.workspace_version(),
            reason="발주 rollback",
            idempotency_key="order-rollback",
            request_id=str(uuid.uuid4()),
        )

    assert list(artifact_root.rglob("*.xlsx")) == []
    assert (
        fixture.connection.execute("SELECT COUNT(*) FROM order_revisions").fetchone()[0]
        == 0
    )


def test_order_rejects_artifacts_from_an_older_approval_after_reapproval(
    tmp_path,
) -> None:
    from suseoro.workflow.approvals import ApprovalService
    from suseoro.workflow.orders import OrderRuleError, OrderService
    from suseoro.workflow.quotes import QuoteService

    fixture = make_workflow_fixture(tmp_path)
    candidate_id = fixture.add_candidate(
        title="책", author="저자", isbn="9788937464010", quantity=1, unit_price=10_000
    )
    first = _approved(fixture)
    quotes = QuoteService(fixture.connection)
    old_quote = quotes.ingest(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        approval_revision_id=first["revision_id"],
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        workspace_version=fixture.workspace_version(),
        vendor_name="이전 업체",
        rows=[
            {
                "isbn": "9788937464010",
                "title": "책",
                "author": "저자",
                "quantity": 1,
                "unit_price": 9_000,
            }
        ],
        reason="첫 견적",
        idempotency_key="old-quote",
        request_id=str(uuid.uuid4()),
    )
    approvals = ApprovalService(fixture.connection)
    second = approvals.adjust_approved_candidate(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        candidate_id=candidate_id,
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        candidate_version=1,
        workspace_version=fixture.workspace_version(),
        changes={"quantity": 2},
        budget_won=40_000,
        reason="수량 증가",
        idempotency_key="increase-after-quote",
        request_id=str(uuid.uuid4()),
    )
    approvals.approve(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        revision_id=second["revision_id"],
        actor_id=fixture.reviewer_id,
        actor_roles=("REVIEWER",),
        workspace_version=fixture.workspace_version(),
        reason="재승인",
        idempotency_key="approve-second",
        request_id=str(uuid.uuid4()),
    )
    quotes.ingest(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        approval_revision_id=second["revision_id"],
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        workspace_version=fixture.workspace_version(),
        vendor_name="현재 업체",
        rows=[
            {
                "isbn": "9788937464010",
                "title": "책",
                "author": "저자",
                "quantity": 2,
                "unit_price": 9_000,
            }
        ],
        reason="현재 견적",
        idempotency_key="current-quote",
        request_id=str(uuid.uuid4()),
    )
    with pytest.raises(OrderRuleError) as stale:
        OrderService(fixture.connection, tmp_path / "artifacts").generate_revision(
            school_id=fixture.school_id,
            workspace_id=fixture.workspace_id,
            approval_revision_id=first["revision_id"],
            quote_id=old_quote["quote_id"],
            actor_id=fixture.operator_id,
            actor_roles=("OPERATOR",),
            workspace_version=fixture.workspace_version(),
            reason="과거 견적으로 발주 시도",
            idempotency_key="stale-order",
            request_id=str(uuid.uuid4()),
        )
    assert stale.value.code == "APPROVAL_REVISION_NOT_CURRENT"


def test_order_honors_current_downward_scope_after_approval(tmp_path) -> None:
    from suseoro.workflow.approvals import ApprovalService
    from suseoro.workflow.orders import OrderRuleError, OrderService
    from suseoro.workflow.quotes import QuoteService

    fixture = make_workflow_fixture(tmp_path)
    candidate_id = fixture.add_candidate(
        title="제외할 책",
        author="저자",
        isbn="9788937464010",
        quantity=1,
        unit_price=10_000,
    )
    approved = _approved(fixture)
    quote = QuoteService(fixture.connection).ingest(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        approval_revision_id=approved["revision_id"],
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        workspace_version=fixture.workspace_version(),
        vendor_name="업체",
        rows=[
            {
                "isbn": "9788937464010",
                "title": "제외할 책",
                "author": "저자",
                "quantity": 1,
                "unit_price": 9_000,
            }
        ],
        reason="견적",
        idempotency_key="quote-before-exclusion",
        request_id=str(uuid.uuid4()),
    )
    ApprovalService(fixture.connection).adjust_approved_candidate(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        candidate_id=candidate_id,
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        candidate_version=1,
        workspace_version=fixture.workspace_version(),
        changes={"outcome": "EXCLUDED"},
        budget_won=40_000,
        reason="최종 제외",
        idempotency_key="exclude-after-quote",
        request_id=str(uuid.uuid4()),
    )
    with pytest.raises(OrderRuleError) as current_scope:
        OrderService(fixture.connection, tmp_path / "artifacts").generate_revision(
            school_id=fixture.school_id,
            workspace_id=fixture.workspace_id,
            approval_revision_id=approved["revision_id"],
            quote_id=quote["quote_id"],
            actor_id=fixture.operator_id,
            actor_roles=("OPERATOR",),
            workspace_version=fixture.workspace_version(),
            reason="제외 전 견적으로 발주 시도",
            idempotency_key="order-after-exclusion",
            request_id=str(uuid.uuid4()),
        )
    assert current_scope.value.code == "APPROVAL_SCOPE_EXCEEDED"
