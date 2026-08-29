from __future__ import annotations

import uuid
from types import SimpleNamespace

from workflow_fixtures import make_workflow_fixture


def test_procurement_reads_are_reload_safe_and_hide_local_paths(tmp_path) -> None:
    from test_receiving_scans import _order_ready

    from suseoro.api.routes.procurement import (
        get_current_order,
        get_quote,
        list_quotes,
    )

    fixture = make_workflow_fixture(tmp_path / "procurement")
    fixture.add_candidate(
        title="다정한 물리학", author="저자", isbn="9788937464010", unit_price=12_000
    )
    ready = _order_ready(fixture, tmp_path / "artifacts")
    user = SimpleNamespace(school_id=fixture.school_id)

    quotes = list_quotes(
        fixture.workspace_id,
        user=user,
        connection=fixture.connection,
        cursor=None,
        limit=50,
    )
    card = quotes["items"][0]
    assert {
        "list_total_won",
        "discount_won",
        "out_of_stock_count",
        "missing_price_count",
        "list_mismatch_count",
        "needs_review_count",
        "unmatched_count",
        "approval_revision_id",
    } <= card.keys()
    detail = get_quote(card["id"], user=user, connection=fixture.connection)
    assert detail["rows"][0]["approval_row_id"]

    current = get_current_order(
        fixture.workspace_id, user=user, connection=fixture.connection
    )["order"]
    assert current["revision_id"] == ready["revision_id"]
    assert current["budget_won"] >= current["total_won"]
    assert "path" not in str(current).casefold()


def test_approval_review_and_receiving_progress_expose_decision_inputs(
    tmp_path,
) -> None:
    from test_receiving_scans import _order_ready

    from suseoro.api.routes.deliveries import (
        get_receiving_status,
        list_deliveries,
        list_receiving_differences,
    )
    from suseoro.workflow.approvals import ApprovalService
    from suseoro.workflow.receiving import ReceivingService

    fixture = make_workflow_fixture(tmp_path / "review")
    fixture.add_candidate(
        title="검토할 책",
        author="저자",
        isbn="9788937464010",
        edition="개정판",
        unit_price=10_000,
    )
    ready = _order_ready(fixture, tmp_path / "review-artifacts")
    approval = ApprovalService(fixture.connection).view_revision(
        school_id=fixture.school_id,
        revision_id=fixture.connection.execute(
            "SELECT approval_revision_id FROM order_revisions WHERE id = ?",
            (ready["revision_id"],),
        ).fetchone()["approval_revision_id"],
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
    )
    assert approval["metadata"]["candidate_count"] == 1
    assert approval["metadata"]["unresolved_complete"] is True
    assert approval["metadata"]["source_counts_verified"] is True
    assert approval["metadata"]["request_reason"]
    assert "previous_revision" in approval["metadata"]

    user = SimpleNamespace(school_id=fixture.school_id)
    progress = get_receiving_status(
        fixture.workspace_id, user=user, connection=fixture.connection
    )
    assert progress["order_revision_id"] == ready["revision_id"]
    assert progress["ordered_quantity"] == 1
    assert progress["can_complete"] is False
    assert progress["blocking_reasons"]
    assert (
        list_deliveries(
            fixture.workspace_id,
            user=user,
            connection=fixture.connection,
            cursor=None,
            limit=50,
        )["items"]
        == []
    )

    ReceivingService(fixture.connection).record_delivery(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        order_revision_id=ready["revision_id"],
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        workspace_version=fixture.workspace_version(),
        rows=[
            {
                "isbn": "9788936434267",
                "title": "주문하지 않은 책",
                "quantity": 1,
                "unit_price": 9_000,
            }
        ],
        reason="식별 정보가 필요한 차이",
        idempotency_key="difference-identifiers",
        request_id=str(uuid.uuid4()),
    )
    page = list_receiving_differences(
        fixture.workspace_id,
        user=user,
        connection=fixture.connection,
        kind=None,
        disposition=None,
        active=True,
        cursor=None,
        limit=50,
    )
    missing = next(item for item in page["items"] if item["kind"] == "MISSING")
    assert missing["details"]["title"] == "검토할 책"
    assert missing["details"]["isbn13"] == "9788937464010"
    assert missing["details"]["edition"] == "개정판"
