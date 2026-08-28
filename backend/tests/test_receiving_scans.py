from __future__ import annotations

import sqlite3
import uuid

import pytest
from workflow_fixtures import make_workflow_fixture


def _order_ready(fixture, tmp_path):
    from suseoro.workflow.approvals import ApprovalService
    from suseoro.workflow.orders import OrderService
    from suseoro.workflow.quotes import QuoteService

    approval = ApprovalService(fixture.connection)
    requested = approval.request_approval(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        workspace_version=fixture.workspace_version(),
        budget_won=50_000,
        reason="승인 요청",
        idempotency_key="request",
        request_id=str(uuid.uuid4()),
    )
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
    quote = QuoteService(fixture.connection).ingest(
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
                "edition": "초판",
                "quantity": 2,
                "unit_price": 9_000,
            }
        ],
        reason="견적",
        idempotency_key="quote",
        request_id=str(uuid.uuid4()),
    )
    orders = OrderService(fixture.connection, tmp_path / "artifacts")
    order = orders.generate_revision(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        approval_revision_id=requested["revision_id"],
        quote_id=quote["quote_id"],
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        workspace_version=fixture.workspace_version(),
        reason="발주",
        idempotency_key="order",
        request_id=str(uuid.uuid4()),
    )
    orders.mark_sent(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        order_revision_id=order["revision_id"],
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        workspace_version=fixture.workspace_version(),
        reason="전달",
        idempotency_key="sent",
        request_id=str(uuid.uuid4()),
    )
    return order


def test_multiple_partial_deliveries_accumulate_and_compare_differences(
    tmp_path,
) -> None:
    from suseoro.workflow.receiving import ReceivingService

    fixture = make_workflow_fixture(tmp_path)
    fixture.add_candidate(
        title="책",
        author="저자",
        isbn="9788937464010",
        quantity=2,
        unit_price=10_000,
        edition="초판",
    )
    order = _order_ready(fixture, tmp_path)
    service = ReceivingService(fixture.connection)
    first = service.record_delivery(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        order_revision_id=order["revision_id"],
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        workspace_version=fixture.workspace_version(),
        rows=[
            {
                "isbn": "9788937464010",
                "title": "책",
                "edition": "초판",
                "quantity": 1,
                "unit_price": 9_000,
            }
        ],
        reason="1차 납품",
        idempotency_key="delivery-1",
        request_id=str(uuid.uuid4()),
    )
    second = service.record_delivery(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        order_revision_id=order["revision_id"],
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        workspace_version=fixture.workspace_version(),
        rows=[
            {
                "isbn": "9788937464010",
                "title": "책",
                "edition": "개정판",
                "quantity": 1,
                "unit_price": 10_000,
            },
            {
                "isbn": "9788936434267",
                "title": "미주문",
                "edition": None,
                "quantity": 1,
                "unit_price": 5_000,
            },
            {
                "isbn": "9788954699914",
                "title": "책",
                "edition": "초판",
                "quantity": 1,
                "unit_price": 9_000,
            },
        ],
        reason="2차 납품",
        idempotency_key="delivery-2",
        request_id=str(uuid.uuid4()),
    )
    assert first["delivery_number"] == 1
    first_kinds = {difference["kind"] for difference in first["differences"]}
    assert "QUANTITY" in first_kinds
    assert "MISSING" not in first_kinds
    assert second["delivery_number"] == 2
    kinds = {difference["kind"] for difference in second["differences"]}
    assert {"UNIT_PRICE", "ISBN", "EDITION", "UNORDERED"}.issubset(kinds)
    assert second["received_quantity"] == 4
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        fixture.connection.execute(
            "UPDATE delivery_batches SET reason = '변조' WHERE id = ?",
            (first["delivery_batch_id"],),
        )
    fixture.connection.rollback()


def test_only_one_active_scan_session_and_scan_idempotency(tmp_path) -> None:
    from suseoro.workflow.receiving import ReceivingRuleError, ReceivingService

    fixture = make_workflow_fixture(tmp_path)
    fixture.add_candidate(title="책", author="저자", isbn="9788937464010", quantity=2)
    order = _order_ready(fixture, tmp_path)
    service = ReceivingService(fixture.connection)
    session = service.start_scan_session(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        order_revision_id=order["revision_id"],
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        workspace_version=fixture.workspace_version(),
        reason="스캔 시작",
        idempotency_key="session-1",
        request_id=str(uuid.uuid4()),
    )
    with pytest.raises(ReceivingRuleError) as active:
        service.start_scan_session(
            school_id=fixture.school_id,
            workspace_id=fixture.workspace_id,
            order_revision_id=order["revision_id"],
            actor_id=fixture.operator_id,
            actor_roles=("OPERATOR",),
            workspace_version=fixture.workspace_version(),
            reason="중복 세션",
            idempotency_key="session-2",
            request_id=str(uuid.uuid4()),
        )
    assert active.value.code == "ACTIVE_SCAN_SESSION_EXISTS"
    first = service.scan(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        session_id=session["session_id"],
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        isbn="9788937464010",
        idempotency_key="scan-1",
        request_id=str(uuid.uuid4()),
    )
    replay = service.scan(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        session_id=session["session_id"],
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        isbn="9788937464010",
        idempotency_key="scan-1",
        request_id=str(uuid.uuid4()),
    )
    second = service.scan(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        session_id=session["session_id"],
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        isbn="9788937464010",
        idempotency_key="scan-2",
        request_id=str(uuid.uuid4()),
    )
    over = service.scan(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        session_id=session["session_id"],
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        isbn="9788937464010",
        idempotency_key="scan-3",
        request_id=str(uuid.uuid4()),
    )
    unordered = service.scan(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        session_id=session["session_id"],
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        isbn="9788936434267",
        idempotency_key="scan-4",
        request_id=str(uuid.uuid4()),
    )
    order_row_id = fixture.connection.execute(
        "SELECT id FROM order_rows WHERE order_revision_id = ?",
        (order["revision_id"],),
    ).fetchone()["id"]
    expected_match = service.scan(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        session_id=session["session_id"],
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        isbn="9788937464010",
        expected_order_row_id=order_row_id,
        idempotency_key="scan-expected-match",
        request_id=str(uuid.uuid4()),
    )
    edition = service.scan(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        session_id=session["session_id"],
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        isbn="9788936434267",
        expected_order_row_id=order_row_id,
        idempotency_key="scan-5",
        request_id=str(uuid.uuid4()),
    )
    assert first == replay
    assert first["code"] == second["code"] == "NORMAL"
    assert first["scanned_quantity"] == 1
    assert replay["scanned_quantity"] == 1
    assert second["scanned_quantity"] == 2
    assert over["code"] == "OVER"
    assert unordered["code"] == "UNORDERED"
    assert expected_match["code"] == "OVER"
    assert edition["code"] == "EDITION_MISMATCH"
    assert (
        fixture.connection.execute(
            "SELECT COUNT(*) AS n FROM scan_events WHERE session_id = ?",
            (session["session_id"],),
        ).fetchone()["n"]
        == 6
    )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        fixture.connection.execute(
            "UPDATE scan_events SET result_code = 'NORMAL' WHERE session_id = ?",
            (session["session_id"],),
        )
    fixture.connection.rollback()


def test_completion_requires_quantities_or_dispositions_on_every_difference(
    tmp_path,
) -> None:
    from suseoro.workflow.receiving import ReceivingRuleError, ReceivingService

    fixture = make_workflow_fixture(tmp_path)
    fixture.add_candidate(title="책", author="저자", isbn="9788937464010", quantity=2)
    order = _order_ready(fixture, tmp_path)
    service = ReceivingService(fixture.connection)
    session = service.start_scan_session(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        order_revision_id=order["revision_id"],
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        workspace_version=fixture.workspace_version(),
        reason="스캔",
        idempotency_key="session",
        request_id=str(uuid.uuid4()),
    )
    service.scan(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        session_id=session["session_id"],
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        isbn="9788937464010",
        idempotency_key="scan-one",
        request_id=str(uuid.uuid4()),
    )
    with pytest.raises(ReceivingRuleError) as incomplete:
        service.complete(
            school_id=fixture.school_id,
            workspace_id=fixture.workspace_id,
            actor_id=fixture.operator_id,
            actor_roles=("OPERATOR",),
            workspace_version=fixture.workspace_version(),
            reason="미완료",
            idempotency_key="complete-no",
            request_id=str(uuid.uuid4()),
        )
    assert incomplete.value.code == "RECEIVING_INCOMPLETE"
    shortage = fixture.connection.execute(
        "SELECT id, row_version FROM receiving_differences WHERE workspace_id = ? AND kind = 'MISSING'",
        (fixture.workspace_id,),
    ).fetchone()
    disposition = service.set_disposition(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        difference_id=shortage["id"],
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        submitted_version=shortage["row_version"],
        disposition="ADDITIONAL_DELIVERY_PLANNED",
        reason="추가 납품 예정",
        idempotency_key="disposition",
        request_id=str(uuid.uuid4()),
    )
    assert disposition["disposition"] == "ADDITIONAL_DELIVERY_PLANNED"
    completed = service.complete(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        workspace_version=fixture.workspace_version(),
        reason="처리 방침 확인",
        idempotency_key="complete-yes",
        request_id=str(uuid.uuid4()),
    )
    assert completed["state"] == "COMPLETE"
