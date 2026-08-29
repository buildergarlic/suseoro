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
        candidate_collection_revision=fixture.candidate_collection_revision(),
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


def test_quote_reconciliation_rejects_zero_quantity_and_reports_duplicates(
    tmp_path,
) -> None:
    from suseoro.workflow.quotes import QuoteRuleError, QuoteService

    fixture = make_workflow_fixture(tmp_path)
    fixture.add_candidate(
        title="책", author="저자", isbn="9788937464010", quantity=2, unit_price=12_000
    )
    approved = _approved(fixture)
    service = QuoteService(fixture.connection)
    with pytest.raises(QuoteRuleError) as zero:
        service.ingest(
            school_id=fixture.school_id,
            workspace_id=fixture.workspace_id,
            approval_revision_id=approved["revision_id"],
            actor_id=fixture.operator_id,
            actor_roles=("OPERATOR",),
            workspace_version=fixture.workspace_version(),
            vendor_name="0권 업체",
            rows=[
                {
                    "isbn": "9788937464010",
                    "title": "책",
                    "author": "저자",
                    "quantity": 0,
                    "unit_price": 9_000,
                }
            ],
            reason="수량 검증",
            idempotency_key="zero-quantity",
            request_id=str(uuid.uuid4()),
        )
    assert zero.value.code == "INVALID_QUOTE_QUANTITY"
    duplicate = service.ingest(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        approval_revision_id=approved["revision_id"],
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        workspace_version=fixture.workspace_version(),
        vendor_name="중복 업체",
        rows=[
            {
                "isbn": "9788937464010",
                "title": "책",
                "author": "저자",
                "quantity": 1,
                "unit_price": 9_000,
            },
            {
                "isbn": "9788937464010",
                "title": "책",
                "author": "저자",
                "quantity": 1,
                "unit_price": 8_500,
            },
        ],
        reason="중복 진단",
        idempotency_key="duplicate-quote",
        request_id=str(uuid.uuid4()),
    )
    assert duplicate["reconciliation"] == {
        "duplicate_isbns": ["9788937464010"],
        "price_conflicts": ["9788937464010"],
    }


def test_manual_no_isbn_confirmation_creates_new_immutable_quote_revision(
    tmp_path,
) -> None:
    from suseoro.workflow.quotes import QuoteService

    fixture = make_workflow_fixture(tmp_path)
    fixture.add_candidate(title="무ISBN", author="저자", isbn=None, unit_price=10_000)
    approved = _approved(fixture)
    service = QuoteService(fixture.connection)
    original = service.ingest(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        approval_revision_id=approved["revision_id"],
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        workspace_version=fixture.workspace_version(),
        vendor_name="무ISBN 업체",
        rows=[
            {
                "isbn": None,
                "title": "무ISBN",
                "author": "저자",
                "quantity": 1,
                "unit_price": 9_000,
            }
        ],
        reason="견적 등록",
        idempotency_key="manual-source",
        request_id=str(uuid.uuid4()),
    )
    approval_row_id = fixture.connection.execute(
        "SELECT id FROM approval_rows WHERE approval_revision_id = ?",
        (approved["revision_id"],),
    ).fetchone()["id"]
    confirmed = service.confirm_manual_match(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        quote_id=original["quote_id"],
        quote_row_id=original["rows"][0]["quote_row_id"],
        approval_row_id=approval_row_id,
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        workspace_version=fixture.workspace_version(),
        reason="제목과 저자 확인",
        idempotency_key="manual-confirm",
        request_id=str(uuid.uuid4()),
    )
    assert confirmed["quote_id"] != original["quote_id"]
    assert confirmed["revision_number"] == 2
    assert confirmed["rows"][0]["match_status"] == "MATCHED_MANUAL"
    assert confirmed["rows"][0]["approval_row_id"] == approval_row_id
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        fixture.connection.execute(
            "UPDATE vendor_quote_rows SET title = '변조' WHERE quote_id = ?",
            (original["quote_id"],),
        )
    fixture.connection.rollback()


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


def test_order_validation_persists_missing_duplicate_and_overallocation_diagnostics(
    tmp_path,
) -> None:
    from suseoro.workflow.orders import OrderService
    from suseoro.workflow.quotes import QuoteService

    fixture = make_workflow_fixture(tmp_path)
    duplicated_id = fixture.add_candidate(
        title="중복", author="저자", isbn="9788937464010", quantity=1, unit_price=10_000
    )
    missing_id = fixture.add_candidate(
        title="누락", author="저자", isbn="9788936434267", quantity=1, unit_price=10_000
    )
    approved = _approved(fixture)
    quote = QuoteService(fixture.connection).ingest(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        approval_revision_id=approved["revision_id"],
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        workspace_version=fixture.workspace_version(),
        vendor_name="불완전 업체",
        rows=[
            {
                "isbn": "9788937464010",
                "title": "중복",
                "author": "저자",
                "quantity": 1,
                "unit_price": 9_000,
            },
            {
                "isbn": "9788937464010",
                "title": "중복",
                "author": "저자",
                "quantity": 1,
                "unit_price": 8_500,
            },
        ],
        reason="불완전 견적",
        idempotency_key="incomplete-quote",
        request_id=str(uuid.uuid4()),
    )
    result = OrderService(
        fixture.connection, tmp_path / "validation-artifacts"
    ).generate_revision(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        approval_revision_id=approved["revision_id"],
        quote_id=quote["quote_id"],
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        workspace_version=fixture.workspace_version(),
        reason="coverage 확인",
        idempotency_key="invalid-order-coverage",
        request_id=str(uuid.uuid4()),
    )
    assert result["status"] == "INVALID"
    assert result["diagnostics"] == {
        "missing_candidate_ids": [missing_id],
        "duplicate_candidate_ids": [duplicated_id],
        "over_allocations": [
            {"candidate_id": duplicated_id, "expected": 1, "allocated": 2}
        ],
        "price_conflict_candidate_ids": [duplicated_id],
        "unmapped_quote_row_ids": [],
    }
    persisted = fixture.connection.execute(
        "SELECT diagnostics_json FROM order_validation_results WHERE id = ?",
        (result["validation_id"],),
    ).fetchone()
    assert persisted is not None
    assert (
        fixture.connection.execute(
            "SELECT COUNT(*) FROM order_revisions WHERE workspace_id = ?",
            (fixture.workspace_id,),
        ).fetchone()[0]
        == 0
    )


def test_advanced_split_uses_remembered_vendor_templates_and_complete_allocations(
    tmp_path,
) -> None:
    from io import BytesIO

    from openpyxl import load_workbook

    from suseoro.workflow.orders import (
        OrderRuleError,
        OrderService,
        OrderTemplateService,
    )
    from suseoro.workflow.quotes import QuoteService

    fixture = make_workflow_fixture(tmp_path)
    first_id = fixture.add_candidate(
        title="첫 책",
        author="저자",
        isbn="9788937464010",
        quantity=1,
        unit_price=10_000,
    )
    second_id = fixture.add_candidate(
        title="둘째 책",
        author="저자",
        isbn="9788936434267",
        quantity=2,
        unit_price=10_000,
    )
    approved = _approved(fixture)
    quotes = QuoteService(fixture.connection)
    quote_a = quotes.ingest(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        approval_revision_id=approved["revision_id"],
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        workspace_version=fixture.workspace_version(),
        vendor_name="업체 A",
        rows=[
            {
                "isbn": "9788937464010",
                "title": "첫 책",
                "author": "저자",
                "quantity": 1,
                "unit_price": 9_000,
            }
        ],
        reason="A 견적",
        idempotency_key="quote-a",
        request_id=str(uuid.uuid4()),
    )
    quote_b = quotes.ingest(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        approval_revision_id=approved["revision_id"],
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        workspace_version=fixture.workspace_version(),
        vendor_name="업체 B",
        rows=[
            {
                "isbn": "9788936434267",
                "title": "둘째 책",
                "author": "저자",
                "quantity": 2,
                "unit_price": 8_000,
            }
        ],
        reason="B 견적",
        idempotency_key="quote-b",
        request_id=str(uuid.uuid4()),
    )
    templates = OrderTemplateService(fixture.connection)
    templates.save(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        vendor_name="업체 A",
        columns=(("title", "A 도서명"), ("quantity", "A 수량")),
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        workspace_version=fixture.workspace_version(),
        reason="A 양식 기억",
        idempotency_key="template-a",
        request_id=str(uuid.uuid4()),
    )
    templates.save(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        vendor_name="업체 B",
        columns=(("isbn", "B ISBN"), ("quantity", "B 수량")),
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        workspace_version=fixture.workspace_version(),
        reason="B 양식 기억",
        idempotency_key="template-b",
        request_id=str(uuid.uuid4()),
    )
    allocations = [
        {
            "quote_id": quote_a["quote_id"],
            "quote_row_id": quote_a["rows"][0]["quote_row_id"],
            "quantity": 1,
        },
        {
            "quote_id": quote_b["quote_id"],
            "quote_row_id": quote_b["rows"][0]["quote_row_id"],
            "quantity": 2,
        },
    ]
    order_service = OrderService(fixture.connection, tmp_path / "split-artifacts")
    with pytest.raises(OrderRuleError) as disabled:
        order_service.generate_revision(
            school_id=fixture.school_id,
            workspace_id=fixture.workspace_id,
            approval_revision_id=approved["revision_id"],
            quote_id=quote_a["quote_id"],
            allocations=allocations,
            advanced_split_enabled=False,
            actor_id=fixture.operator_id,
            actor_roles=("OPERATOR",),
            workspace_version=fixture.workspace_version(),
            reason="비활성 분할 발주",
            idempotency_key="split-disabled",
            request_id=str(uuid.uuid4()),
        )
    assert disabled.value.code == "ADVANCED_SPLIT_ORDER_DISABLED"
    order = order_service.generate_revision(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        approval_revision_id=approved["revision_id"],
        quote_id=quote_a["quote_id"],
        allocations=allocations,
        advanced_split_enabled=True,
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        workspace_version=fixture.workspace_version(),
        reason="분할 발주",
        idempotency_key="split-order",
        request_id=str(uuid.uuid4()),
    )
    assert order["status"] == "READY"
    assert {item["vendor_name"] for item in order["artifacts"]} == {"업체 A", "업체 B"}
    assert {row["candidate_id"] for row in order["allocations"]} == {
        first_id,
        second_id,
    }
    workbooks = {
        item["vendor_name"]: load_workbook(BytesIO(item["path"].read_bytes()))["발주서"]
        for item in order["artifacts"]
    }
    assert [cell.value for cell in workbooks["업체 A"][1]] == ["A 도서명", "A 수량"]
    assert [cell.value for cell in workbooks["업체 B"][1]] == ["B ISBN", "B 수량"]
    assert (
        fixture.connection.execute(
            "SELECT COUNT(*) FROM order_vendor_artifacts WHERE order_revision_id = ?",
            (order["revision_id"],),
        ).fetchone()[0]
        == 2
    )


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
        vendor_name="../../escaped-vendor",
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
    artifact_scope = (
        tmp_path / "artifacts" / fixture.school_id / fixture.workspace_id
    ).resolve()
    assert first["path"].resolve().is_relative_to(artifact_scope)
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
    assert second["state"] == "ORDER_READY"
    with pytest.raises(OrderRuleError) as stale_first:
        service.mark_sent(
            school_id=fixture.school_id,
            workspace_id=fixture.workspace_id,
            order_revision_id=first["revision_id"],
            actor_id=fixture.operator_id,
            actor_roles=("OPERATOR",),
            workspace_version=fixture.workspace_version(),
            reason="과거 r1 재전달 시도",
            idempotency_key="stale-r1-send",
            request_id=str(uuid.uuid4()),
        )
    assert stale_first.value.code == "ORDER_REVISION_NOT_CURRENT"
    service.mark_sent(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        order_revision_id=second["revision_id"],
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        workspace_version=fixture.workspace_version(),
        reason="r2 전달",
        idempotency_key="send-r2",
        request_id=str(uuid.uuid4()),
    )
    third = service.generate_revision(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        approval_revision_id=approved["revision_id"],
        quote_id=quote["quote_id"],
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        workspace_version=fixture.workspace_version(),
        reason="전달 뒤 r3",
        idempotency_key="order-3",
        request_id=str(uuid.uuid4()),
    )
    assert third["revision_number"] == 3
    assert third["state"] == "ORDER_READY"
    with pytest.raises(OrderRuleError) as stale_second:
        service.mark_sent(
            school_id=fixture.school_id,
            workspace_id=fixture.workspace_id,
            order_revision_id=second["revision_id"],
            actor_id=fixture.operator_id,
            actor_roles=("OPERATOR",),
            workspace_version=fixture.workspace_version(),
            reason="과거 r2 재전달 시도",
            idempotency_key="stale-r2-send",
            request_id=str(uuid.uuid4()),
        )
    assert stale_second.value.code == "ORDER_REVISION_NOT_CURRENT"
    sent_third = service.mark_sent(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        order_revision_id=third["revision_id"],
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        workspace_version=fixture.workspace_version(),
        reason="r3 전달",
        idempotency_key="send-r3",
        request_id=str(uuid.uuid4()),
    )
    assert sent_third["state"] == "ORDER_SENT"
    current = fixture.connection.execute(
        "SELECT order_revision_id, transmission_id FROM workspace_current_orders WHERE workspace_id = ?",
        (fixture.workspace_id,),
    ).fetchone()
    assert current["order_revision_id"] == third["revision_id"]
    assert current["transmission_id"] == sent_third["transmission_id"]

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
    scope = over_service.generate_revision(
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
    assert scope["status"] == "INVALID"
    assert scope["diagnostics"]["over_allocations"]


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
    from suseoro.workflow.orders import OrderService
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
    current_scope = OrderService(
        fixture.connection, tmp_path / "artifacts"
    ).generate_revision(
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
    assert current_scope["status"] == "INVALID"
    assert current_scope["diagnostics"]["unmapped_quote_row_ids"]


@pytest.mark.parametrize(
    ("state", "allowed"),
    [
        ("DRAFT", False),
        ("ANALYZING", False),
        ("CANDIDATE_REVIEW", False),
        ("APPROVAL_PENDING", False),
        ("CHANGES_REQUESTED", False),
        ("APPROVED", False),
        ("QUOTE_REVIEW", True),
        ("ORDER_READY", True),
        ("ORDER_SENT", True),
        ("RECEIVING", True),
        ("COMPLETED", False),
    ],
)
def test_order_template_save_obeys_order_preparation_state_boundary(
    tmp_path, state: str, allowed: bool
) -> None:
    from suseoro.workflow.orders import OrderRuleError, OrderTemplateService

    fixture = make_workflow_fixture(tmp_path, state=state)
    service = OrderTemplateService(fixture.connection)
    arguments = {
        "school_id": fixture.school_id,
        "workspace_id": fixture.workspace_id,
        "vendor_name": "업체",
        "columns": (("title", "도서명"), ("quantity", "수량")),
        "actor_id": fixture.operator_id,
        "actor_roles": ("OPERATOR",),
        "workspace_version": 1,
        "reason": "업체 양식 저장",
        "idempotency_key": f"template-state-{state}",
        "request_id": str(uuid.uuid4()),
    }
    if allowed:
        result = service.save(**arguments)
        assert result["state"] == state
        assert result["row_version"] == 2
        return

    with pytest.raises(OrderRuleError) as rejected:
        service.save(**arguments)
    assert rejected.value.code == "ORDER_TEMPLATE_STATE_INVALID"
    assert fixture.workspace_version() == 1
    assert (
        fixture.connection.execute(
            "SELECT COUNT(*) FROM workflow_order_templates"
        ).fetchone()[0]
        == 0
    )
    assert (
        fixture.connection.execute(
            "SELECT COUNT(*) FROM audit_events WHERE action = 'ORDER_TEMPLATE_SAVED'"
        ).fetchone()[0]
        == 0
    )
