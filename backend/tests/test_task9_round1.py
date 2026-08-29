from __future__ import annotations

import json
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from test_receiving_scans import _order_ready
from test_task8_round4 import (
    _seed_source_row,
    _seed_terminal_file_result,
    _source_id,
)
from test_task8_round5 import _0010_directory, _file_result
from workflow_fixtures import NOW, make_workflow_fixture

from suseoro.db import migrations as migration_module
from suseoro.db.migrations import apply_migrations
from suseoro.jobs.repository import JobRepository
from suseoro.services.public_replay import sanitize_persisted_response
from suseoro.services.upload_idempotency import upload_scope_replay_allowance
from suseoro.workflow.approvals import ApprovalError, ApprovalService
from suseoro.workflow.orders import OrderService
from suseoro.workflow.procurement_imports import ProcurementImportService
from suseoro.workflow.quotes import QuoteService
from suseoro.workflow.receiving import ReceivingService


def _migration_directory() -> Path:
    return Path(migration_module.__file__).with_name("migrations")


def _through_0017_directory(destination: Path) -> Path:
    destination.mkdir()
    for source in _migration_directory().glob("*.sql"):
        if source.stem != "0018_task9_round1_receiving_links":
            shutil.copy2(source, destination / source.name)
    return destination


def _link_source(fixture, source_id: str) -> None:
    fixture.connection.execute(
        """
        INSERT INTO workspace_sources (
            workspace_id, source_document_id, school_id, created_at
        ) VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        """,
        (fixture.workspace_id, source_id, fixture.school_id),
    )


def _configure_role(fixture, source_id: str, role: str) -> None:
    fixture.connection.execute(
        """
        INSERT INTO source_configurations (
            source_document_id, school_id, role, mapping_json,
            row_version, updated_at, vendor_scope, remember_template
        ) VALUES (?, ?, ?, '{}', 1, CURRENT_TIMESTAMP, '*', 0)
        """,
        (source_id, fixture.school_id, role),
    )


def _insert_source(fixture, role: str) -> str:
    source_file_id = str(uuid.uuid4())
    source_id = str(uuid.uuid4())
    fixture.connection.execute(
        """
        INSERT INTO source_files (
            id, sha256, size_bytes, storage_path, original_filename,
            detected_format, created_at
        ) VALUES (?, ?, 1, 'round1.csv', 'round1.csv', 'CSV', ?)
        """,
        (source_file_id, uuid.uuid4().hex * 2, NOW),
    )
    fixture.connection.execute(
        """
        INSERT INTO source_documents (
            id, source_file_id, school_id, role, parser_version, status,
            detected_format, created_at, completed_at
        ) VALUES (?, ?, ?, ?, 'tabular-v1', 'SUCCESS', 'CSV', ?, ?)
        """,
        (source_id, source_file_id, fixture.school_id, role, NOW, NOW),
    )
    return source_id


def _request(fixture, key: str, *, budget_won: int = 20_000) -> dict[str, object]:
    return ApprovalService(fixture.connection).request_approval(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        workspace_version=fixture.workspace_version(),
        candidate_collection_revision=fixture.candidate_collection_revision(),
        budget_won=budget_won,
        reason="검증된 목록 승인 요청",
        idempotency_key=key,
        request_id=str(uuid.uuid4()),
    )


def _order_ready_with_manual_matches(fixture, artifact_root: Path) -> dict[str, object]:
    requested = _request(fixture, "manual-order-request", budget_won=50_000)
    ApprovalService(fixture.connection).approve(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        revision_id=requested["revision_id"],
        actor_id=fixture.reviewer_id,
        actor_roles=("REVIEWER",),
        workspace_version=fixture.workspace_version(),
        reason="수동 연결 검증 승인",
        idempotency_key="manual-order-approve",
        request_id=str(uuid.uuid4()),
    )
    approval_rows = fixture.connection.execute(
        "SELECT * FROM approval_rows WHERE approval_revision_id = ? ORDER BY title",
        (requested["revision_id"],),
    ).fetchall()
    quote = QuoteService(fixture.connection).ingest(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        approval_revision_id=requested["revision_id"],
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        workspace_version=fixture.workspace_version(),
        vendor_name="수동 연결 업체",
        rows=[
            {
                "isbn": row["isbn13"],
                "title": row["title"],
                "author": row["author"],
                "quantity": row["quantity"],
                "unit_price": row["unit_price"],
            }
            for row in approval_rows
        ],
        reason="ISBN 없는 견적",
        idempotency_key="manual-order-quote",
        request_id=str(uuid.uuid4()),
    )
    quote_id = quote["quote_id"]
    index = 0
    while True:
        pending = fixture.connection.execute(
            """
            SELECT id, approval_row_id FROM vendor_quote_rows
            WHERE quote_id = ? AND match_status = 'NEEDS_REVIEW'
            ORDER BY title LIMIT 1
            """,
            (quote_id,),
        ).fetchone()
        if pending is None:
            break
        matched = QuoteService(fixture.connection).confirm_manual_match(
            school_id=fixture.school_id,
            workspace_id=fixture.workspace_id,
            quote_id=quote_id,
            quote_row_id=pending["id"],
            approval_row_id=pending["approval_row_id"],
            actor_id=fixture.operator_id,
            actor_roles=("OPERATOR",),
            workspace_version=fixture.workspace_version(),
            reason="제목과 저자 확인",
            idempotency_key=f"manual-order-match-{index}",
            request_id=str(uuid.uuid4()),
        )
        quote_id = matched["quote_id"]
        index += 1
    order_service = OrderService(fixture.connection, artifact_root)
    order = order_service.generate_revision(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        approval_revision_id=requested["revision_id"],
        quote_id=quote_id,
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        workspace_version=fixture.workspace_version(),
        reason="수동 연결 발주",
        idempotency_key="manual-order-generate",
        request_id=str(uuid.uuid4()),
    )
    assert order["status"] == "READY"
    order_service.mark_sent(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        order_revision_id=order["revision_id"],
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        workspace_version=fixture.workspace_version(),
        reason="업체 전달",
        idempotency_key="manual-order-sent",
        request_id=str(uuid.uuid4()),
    )
    return order


def test_stale_cancelled_approval_revision_returns_actionable_domain_conflict(
    tmp_path: Path,
) -> None:
    fixture = make_workflow_fixture(tmp_path / "stale-approval-revision")
    fixture.add_candidate(
        title="최신 승인본만 결정할 책",
        author="김사서",
        isbn="9788937464010",
        unit_price=10_000,
    )
    service = ApprovalService(fixture.connection)
    first = _request(fixture, "stale-approval-first")
    service.cancel_request(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        revision_id=first["revision_id"],
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        workspace_version=fixture.workspace_version(),
        reason="최신 목록으로 다시 요청",
        idempotency_key="stale-approval-cancel",
        request_id=str(uuid.uuid4()),
    )
    second = _request(fixture, "stale-approval-second")

    with pytest.raises(ApprovalError) as stale:
        service.approve(
            school_id=fixture.school_id,
            workspace_id=fixture.workspace_id,
            revision_id=first["revision_id"],
            actor_id=fixture.reviewer_id,
            actor_roles=("REVIEWER",),
            workspace_version=fixture.workspace_version(),
            reason="오래된 화면에서 승인",
            idempotency_key="stale-approval-decision",
            request_id=str(uuid.uuid4()),
        )
    assert stale.value.code == "APPROVAL_REVISION_NOT_CURRENT"
    assert (
        fixture.connection.execute(
            "SELECT COUNT(*) FROM approval_decisions WHERE approval_revision_id = ?",
            (first["revision_id"],),
        ).fetchone()[0]
        == 0
    )
    assert second["state"] == "APPROVAL_PENDING"


def test_stale_approval_domain_conflict_has_actionable_public_envelope() -> None:
    from suseoro.api.errors import install_error_handlers

    app = FastAPI()
    install_error_handlers(app)

    @app.get("/stale-approval")
    def stale_approval() -> None:
        raise ApprovalError("APPROVAL_REVISION_NOT_CURRENT")

    response = TestClient(app, raise_server_exceptions=False).get(
        "/stale-approval",
        headers={"X-Request-ID": str(uuid.uuid4())},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "APPROVAL_REVISION_NOT_CURRENT"
    assert response.json()["detail"]["message"] == (
        "승인 요청이 바뀌었습니다. 최신 승인 요청을 다시 확인해 주세요."
    )


def test_decided_old_revision_is_stale_when_a_new_revision_is_pending(
    tmp_path: Path,
) -> None:
    fixture = make_workflow_fixture(tmp_path / "decided-stale-revision")
    fixture.add_candidate(
        title="다시 요청할 책",
        author="김사서",
        isbn="9788937464010",
        unit_price=10_000,
    )
    service = ApprovalService(fixture.connection)
    first = _request(fixture, "decided-stale-first")
    service.reject(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        revision_id=first["revision_id"],
        actor_id=fixture.reviewer_id,
        actor_roles=("REVIEWER",),
        workspace_version=fixture.workspace_version(),
        reason="목록을 한 번 더 확인해 주세요.",
        idempotency_key="decided-stale-reject",
        request_id=str(uuid.uuid4()),
    )
    second = _request(fixture, "decided-stale-second")

    with pytest.raises(ApprovalError) as stale:
        service.approve(
            school_id=fixture.school_id,
            workspace_id=fixture.workspace_id,
            revision_id=first["revision_id"],
            actor_id=fixture.reviewer_id,
            actor_roles=("REVIEWER",),
            workspace_version=fixture.workspace_version(),
            reason="오래된 결정 다시 시도",
            idempotency_key="decided-stale-approve-old",
            request_id=str(uuid.uuid4()),
        )
    assert stale.value.code == "APPROVAL_REVISION_NOT_CURRENT"
    assert (
        fixture.connection.execute(
            "SELECT COUNT(*) FROM approval_decisions WHERE approval_revision_id = ?",
            (second["revision_id"],),
        ).fetchone()[0]
        == 0
    )


def test_upload_allowance_fails_closed_before_using_inconsistent_replay(
    tmp_path: Path,
) -> None:
    fixture = make_workflow_fixture(tmp_path / "invalid-upload-allowance")
    route = f"POST /api/v2/workspaces/{fixture.workspace_id}/sources"
    key = "invalid-upload-allowance"
    fixture.connection.execute(
        """
        INSERT INTO upload_idempotency_claims (
            id, school_id, actor_id, route, key, request_fingerprint,
            generation, state, response_status, response_body,
            created_at, updated_at, request_metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?, 1, 'COMPLETED', 202, ?, ?, ?, ?)
        """,
        (
            str(uuid.uuid4()),
            fixture.school_id,
            fixture.operator_id,
            route,
            key,
            "a" * 64,
            json.dumps(
                {
                    "job_id": str(uuid.uuid4()),
                    "items": [
                        {
                            "filename": "contradictory.csv",
                            "status": "ACCEPTED",
                            "source_id": str(uuid.uuid4()),
                            "error": {"code": "FILE_TOO_LARGE", "message": "private"},
                        }
                    ],
                }
            ),
            NOW,
            NOW,
            json.dumps(
                {"files": [{"filename": "contradictory.csv", "size_bytes": 10}]}
            ),
        ),
    )
    fixture.connection.commit()
    database_path = Path(
        fixture.connection.execute("PRAGMA database_list").fetchone()["file"]
    )

    with pytest.raises(HTTPException) as invalid:
        upload_scope_replay_allowance(
            database_path,
            school_id=fixture.school_id,
            actor_id=fixture.operator_id,
            route=route,
            key=key,
        )
    assert invalid.value.status_code == 409
    assert invalid.value.detail == {"code": "HISTORICAL_REPLAY_INVALID"}


def test_approval_count_gate_uses_effective_configured_purchase_request_role(
    tmp_path: Path,
) -> None:
    """A configured role change must both add and remove a source from the gate."""
    configured_purchase = make_workflow_fixture(
        tmp_path / "configured-purchase",
        migrations_dir=_0010_directory(tmp_path / "configured-purchase-migrations"),
    )
    source_id = _insert_source(configured_purchase, "VENDOR_QUOTE")
    _configure_role(configured_purchase, source_id, "PURCHASE_REQUEST")
    _link_source(configured_purchase, source_id)
    _seed_terminal_file_result(
        configured_purchase,
        source_id=source_id,
        total_rows=3,
        processed_rows=3,
        error={"code": "ROW_ERRORS"},
    )
    _seed_source_row(
        configured_purchase, source_id=source_id, source_row=3, status="ROW_ERROR"
    )
    configured_purchase.connection.commit()
    apply_migrations(configured_purchase.connection, _migration_directory())
    configured_purchase.add_candidate(
        title="설정 역할 추천책", author="저자", isbn="9788937464010", unit_price=10_000
    )

    with pytest.raises(HTTPException) as blocked:
        _request(configured_purchase, "configured-purchase-blocked")
    assert blocked.value.status_code == 409
    assert blocked.value.detail["code"] == "SOURCE_COUNTS_UNVERIFIED"

    configured_quote = make_workflow_fixture(
        tmp_path / "configured-quote",
        migrations_dir=_0010_directory(tmp_path / "configured-quote-migrations"),
    )
    source_id = _source_id(configured_quote)
    _configure_role(configured_quote, source_id, "VENDOR_QUOTE")
    _link_source(configured_quote, source_id)
    _seed_terminal_file_result(
        configured_quote,
        source_id=source_id,
        total_rows=3,
        processed_rows=3,
        error={"code": "ROW_ERRORS"},
    )
    _seed_source_row(
        configured_quote, source_id=source_id, source_row=3, status="ROW_ERROR"
    )
    configured_quote.connection.commit()
    apply_migrations(configured_quote.connection, _migration_directory())
    configured_quote.add_candidate(
        title="견적 역할 자료", author="저자", isbn="9788936434267", unit_price=10_000
    )

    assert _request(configured_quote, "configured-quote-ignored")["state"] == (
        "APPROVAL_PENDING"
    )


def test_migrated_exact_count_evidence_expires_when_result_is_retried(
    tmp_path: Path,
) -> None:
    """Changing the extraction result timestamp invalidates captured EXACT evidence."""
    fixture = make_workflow_fixture(
        tmp_path / "stale-evidence",
        migrations_dir=_0010_directory(tmp_path / "stale-evidence-migrations"),
    )
    source_id = _source_id(fixture)
    job_id, _result_id = _seed_terminal_file_result(
        fixture,
        source_id=source_id,
        total_rows=2,
        processed_rows=2,
        error={"code": "ROW_ERRORS"},
    )
    _seed_source_row(fixture, source_id=source_id, source_row=100, status="SUCCESS")
    _seed_source_row(fixture, source_id=source_id, source_row=101, status="ROW_ERROR")
    _link_source(fixture, source_id)
    fixture.connection.commit()
    apply_migrations(fixture.connection, _migration_directory())
    assert _file_result(fixture, job_id)["count_confidence"] == "EXACT"

    claim = fixture.connection.execute(
        "SELECT claim_token, claim_generation FROM durable_jobs WHERE id = ?",
        (job_id,),
    ).fetchone()
    fixture.connection.execute(
        "UPDATE durable_jobs SET status = 'RUNNING' WHERE id = ?", (job_id,)
    )
    JobRepository(fixture.connection).record_file_result(
        job_id=job_id,
        claim_token=claim["claim_token"],
        claim_generation=claim["claim_generation"],
        source_document_id=source_id,
        status="PARTIAL",
        total_rows=2,
        processed_rows=1,
        row_error_count=1,
        error={"code": "ROW_ERRORS"},
        now=datetime(2026, 8, 30, tzinfo=UTC),
    )
    fixture.connection.execute(
        "UPDATE durable_jobs SET status = 'PARTIAL' WHERE id = ?", (job_id,)
    )
    fixture.connection.commit()
    assert _file_result(fixture, job_id)["count_confidence"] == "UNVERIFIED"
    fixture.add_candidate(
        title="재시도된 추출 결과",
        author="저자",
        isbn="9788937464010",
        unit_price=10_000,
    )
    with pytest.raises(HTTPException) as blocked:
        _request(fixture, "stale-evidence-blocked")
    assert blocked.value.detail["code"] == "SOURCE_COUNTS_UNVERIFIED"


@pytest.mark.parametrize(
    "item",
    [
        {
            "filename": "unknown.csv",
            "status": "MYSTERY",
            "source_id": None,
            "error": None,
        },
        {
            "filename": "accepted.csv",
            "status": "ACCEPTED",
            "source_id": "source-1",
            "error": {"code": "FILE_TOO_LARGE", "message": "private"},
        },
        {
            "filename": "failed.csv",
            "status": "FAILED",
            "source_id": None,
            "error": None,
        },
        {
            "filename": "orphan.csv",
            "status": "ACCEPTED",
            "source_id": None,
            "error": None,
        },
    ],
)
def test_upload_replay_rejects_inconsistent_persisted_item_state(
    item: dict[str, object],
) -> None:
    """Unknown or contradictory status/source/error tuples fail closed."""
    with pytest.raises(HTTPException) as invalid:
        sanitize_persisted_response(
            "POST /api/v2/workspaces/w/sources",
            {
                "job_id": None,
                "items": [item],
            },
        )
    assert invalid.value.status_code == 409
    assert invalid.value.detail == {"code": "HISTORICAL_REPLAY_INVALID"}


def test_delivery_recomputation_preserves_only_identical_difference_fingerprint(
    tmp_path: Path,
) -> None:
    fixture = make_workflow_fixture(tmp_path / "fingerprint")
    fixture.add_candidate(
        title="세 권 주문한 책",
        author="저자",
        isbn="9788937464010",
        quantity=3,
        unit_price=10_000,
    )
    order = _order_ready(fixture, tmp_path / "fingerprint-artifacts")
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
                "title": "세 권 주문한 책",
                "quantity": 1,
                "unit_price": 9_000,
            }
        ],
        reason="1차 납품",
        idempotency_key="fingerprint-delivery-1",
        request_id=str(uuid.uuid4()),
    )
    quantity = next(item for item in first["differences"] if item["kind"] == "QUANTITY")
    row = fixture.connection.execute(
        "SELECT row_version FROM receiving_differences WHERE id = ?", (quantity["id"],)
    ).fetchone()
    service.set_disposition(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        difference_id=quantity["id"],
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        submitted_version=row["row_version"],
        disposition="ACCEPTED",
        reason="1권 도착 차이 수용",
        idempotency_key="fingerprint-disposition",
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
                "isbn": "9788936434267",
                "title": "미주문 책",
                "quantity": 1,
                "unit_price": 5_000,
            }
        ],
        reason="무관한 2차 납품",
        idempotency_key="fingerprint-delivery-2",
        request_id=str(uuid.uuid4()),
    )
    unchanged = next(
        item for item in second["differences"] if item["kind"] == "QUANTITY"
    )
    persisted = fixture.connection.execute(
        "SELECT disposition FROM receiving_differences WHERE id = ?", (unchanged["id"],)
    ).fetchone()
    assert unchanged["id"] == quantity["id"]
    assert persisted["disposition"] == "ACCEPTED"

    third = service.record_delivery(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        order_revision_id=order["revision_id"],
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        workspace_version=fixture.workspace_version(),
        rows=[
            {
                "isbn": "9788937464010",
                "title": "세 권 주문한 책",
                "quantity": 1,
                "unit_price": 9_000,
            }
        ],
        reason="사실이 바뀐 3차 납품",
        idempotency_key="fingerprint-delivery-3",
        request_id=str(uuid.uuid4()),
    )
    changed = next(item for item in third["differences"] if item["kind"] == "QUANTITY")
    persisted = fixture.connection.execute(
        "SELECT disposition, details_json FROM receiving_differences WHERE id = ?",
        (changed["id"],),
    ).fetchone()
    assert json.loads(persisted["details_json"])["received"] == 2
    assert persisted["disposition"] is None


def test_receiving_reload_totals_use_stable_links_for_null_isbn_rows(
    tmp_path: Path,
) -> None:
    from suseoro.api.routes.deliveries import get_receiving_status

    fixture = make_workflow_fixture(tmp_path / "null-isbn")
    fixture.add_candidate(
        title="ISBN 없는 첫 책", author="첫 저자", isbn=None, unit_price=10_000
    )
    fixture.add_candidate(
        title="ISBN 없는 둘째 책", author="둘째 저자", isbn=None, unit_price=11_000
    )
    order = _order_ready_with_manual_matches(fixture, tmp_path / "null-isbn-artifacts")
    ReceivingService(fixture.connection).record_delivery(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        order_revision_id=order["revision_id"],
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        workspace_version=fixture.workspace_version(),
        rows=[
            {
                "isbn": None,
                "title": "ISBN 없는 첫 책",
                "author": "첫 저자",
                "quantity": 1,
                "unit_price": 9_000,
            }
        ],
        reason="첫 책만 납품",
        idempotency_key="null-isbn-delivery",
        request_id=str(uuid.uuid4()),
    )
    status = get_receiving_status(
        fixture.workspace_id,
        user=SimpleNamespace(school_id=fixture.school_id),
        connection=fixture.connection,
    )
    by_title = {row["title"]: row["delivered_quantity"] for row in status["rows"]}
    assert by_title == {"ISBN 없는 첫 책": 1, "ISBN 없는 둘째 책": 0}
    assert status["delivered_quantity"] == 1


def test_complete_manifest_without_barcode_or_disposition_cannot_finish(
    tmp_path: Path,
) -> None:
    from suseoro.api.routes.deliveries import get_receiving_status
    from suseoro.workflow.receiving import ReceivingRuleError

    fixture = make_workflow_fixture(tmp_path / "manifest-only")
    fixture.add_candidate(
        title="실물 확인할 책", author="저자", isbn="9788937464010", unit_price=10_000
    )
    order = _order_ready(fixture, tmp_path / "manifest-only-artifacts")
    service = ReceivingService(fixture.connection)
    service.record_delivery(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        order_revision_id=order["revision_id"],
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        workspace_version=fixture.workspace_version(),
        rows=[
            {
                "isbn": "9788937464010",
                "title": "실물 확인할 책",
                "quantity": 1,
                "unit_price": 9_000,
            }
        ],
        reason="완전한 명세서",
        idempotency_key="manifest-only-delivery",
        request_id=str(uuid.uuid4()),
    )
    status = get_receiving_status(
        fixture.workspace_id,
        user=SimpleNamespace(school_id=fixture.school_id),
        connection=fixture.connection,
    )
    assert status["delivered_quantity"] == status["ordered_quantity"] == 1
    assert status["scanned_quantity"] == 0
    assert status["can_complete"] is False
    assert any("바코드" in reason for reason in status["blocking_reasons"])
    with pytest.raises(ReceivingRuleError, match="RECEIVING_INCOMPLETE"):
        service.complete(
            school_id=fixture.school_id,
            workspace_id=fixture.workspace_id,
            actor_id=fixture.operator_id,
            actor_roles=("OPERATOR",),
            workspace_version=fixture.workspace_version(),
            reason="명세서만으로 완료 시도",
            idempotency_key="manifest-only-complete",
            request_id=str(uuid.uuid4()),
        )


def test_0018_upgrade_reconciles_normalized_delivery_and_preserves_exact_disposition(
    tmp_path: Path,
) -> None:
    from suseoro.api.routes.deliveries import get_receiving_status

    fixture = make_workflow_fixture(
        tmp_path / "pre-0018-database",
        migrations_dir=_through_0017_directory(tmp_path / "through-0017"),
    )
    fixture.add_candidate(
        title="ISBN 없는 책: 하나",
        author="김 저자",
        isbn=None,
        unit_price=10_000,
    )
    order = _order_ready_with_manual_matches(fixture, tmp_path / "pre-0018-artifacts")
    ordered = fixture.connection.execute(
        "SELECT * FROM order_rows WHERE order_revision_id = ?",
        (order["revision_id"],),
    ).fetchone()
    assert ordered is not None
    batch_id = str(uuid.uuid4())
    delivered_id = str(uuid.uuid4())
    fixture.connection.execute(
        """
        INSERT INTO delivery_batches (
            id, school_id, workspace_id, order_revision_id, delivery_number,
            reason, created_by_user_id, created_at, sealed_at
        ) VALUES (?, ?, ?, ?, 1, '과거 명세', ?, ?, NULL)
        """,
        (
            batch_id,
            fixture.school_id,
            fixture.workspace_id,
            order["revision_id"],
            fixture.operator_id,
            NOW,
        ),
    )
    fixture.connection.execute(
        """
        INSERT INTO delivery_rows (
            id, delivery_batch_id, isbn13, title, author, edition,
            quantity, unit_price, created_at
        ) VALUES (?, ?, NULL, ?, ?, NULL, 1, 9000, ?)
        """,
        (delivered_id, batch_id, "ISBN 없는 책 하나", "김저자", NOW),
    )
    fixture.connection.execute(
        "UPDATE delivery_batches SET sealed_at = ? WHERE id = ?",
        (NOW, batch_id),
    )
    stale_missing_id = str(uuid.uuid4())
    preserved_price_id = str(uuid.uuid4())
    fixture.connection.executemany(
        """
        INSERT INTO receiving_differences (
            id, school_id, workspace_id, order_revision_id, order_row_id,
            kind, reference_key, details_json, disposition, active,
            row_version, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1, ?, ?)
        """,
        (
            (
                stale_missing_id,
                fixture.school_id,
                fixture.workspace_id,
                order["revision_id"],
                ordered["id"],
                "MISSING",
                f"delivery:missing:{ordered['id']}",
                '{"expected":1,"received":0}',
                None,
                NOW,
                NOW,
            ),
            (
                preserved_price_id,
                fixture.school_id,
                fixture.workspace_id,
                order["revision_id"],
                ordered["id"],
                "UNIT_PRICE",
                f"delivery:price:{delivered_id}",
                '{"expected":10000,"received":9000}',
                "ACCEPTED",
                NOW,
                NOW,
            ),
        ),
    )
    fixture.connection.commit()

    apply_migrations(fixture.connection, _migration_directory())

    allocation = fixture.connection.execute(
        "SELECT * FROM delivery_row_order_allocations WHERE delivery_row_id = ?",
        (delivered_id,),
    ).fetchone()
    assert allocation is not None
    assert allocation["order_row_id"] == ordered["id"]
    active = fixture.connection.execute(
        """
        SELECT * FROM receiving_differences
        WHERE workspace_id = ? AND active = 1 AND reference_key LIKE 'delivery:%'
        ORDER BY kind
        """,
        (fixture.workspace_id,),
    ).fetchall()
    assert [(row["id"], row["kind"], row["disposition"]) for row in active] == [
        (preserved_price_id, "UNIT_PRICE", "ACCEPTED")
    ]
    assert active[0]["reference_key"] == (
        f"delivery:price:{delivered_id}:{ordered['id']}"
    )
    assert (
        fixture.connection.execute(
            "SELECT active FROM receiving_differences WHERE id = ?",
            (stale_missing_id,),
        ).fetchone()["active"]
        == 0
    )
    status = get_receiving_status(
        fixture.workspace_id,
        user=SimpleNamespace(school_id=fixture.school_id),
        connection=fixture.connection,
    )
    assert status["delivered_quantity"] == status["ordered_quantity"] == 1


def test_same_isbn_scan_uses_expected_order_row_hint_for_that_attempt(
    tmp_path: Path,
) -> None:
    fixture = make_workflow_fixture(tmp_path / "duplicate-isbn-scan")
    isbn = "9788937464010"
    fixture.add_candidate(title="동일 ISBN 첫 행", author="저자", isbn=isbn)
    fixture.add_candidate(
        title="동일 ISBN 둘째 행", author="저자", isbn="9788936434267"
    )
    order = _order_ready(fixture, tmp_path / "duplicate-isbn-artifacts")
    # The normal quote helper rejects ambiguous duplicate ISBNs before order
    # generation. Emulate a valid pre-seal duplicate order snapshot in this
    # isolated fixture so the scan service is exercised against that state.
    second_row_id = fixture.connection.execute(
        "SELECT id FROM order_rows WHERE order_revision_id = ? AND isbn13 != ?",
        (order["revision_id"], isbn),
    ).fetchone()["id"]
    fixture.connection.execute("DROP TRIGGER immutable_order_rows_update")
    fixture.connection.execute(
        "UPDATE order_rows SET isbn13 = ? WHERE id = ?", (isbn, second_row_id)
    )
    fixture.connection.commit()
    service = ReceivingService(fixture.connection)
    session = service.start_scan_session(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        order_revision_id=order["revision_id"],
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        workspace_version=fixture.workspace_version(),
        reason="중복 ISBN 실물 행 확인",
        idempotency_key="duplicate-isbn-session",
        request_id=str(uuid.uuid4()),
    )
    rows = fixture.connection.execute(
        "SELECT id FROM order_rows WHERE order_revision_id = ? AND isbn13 = ?",
        (order["revision_id"], isbn),
    ).fetchall()
    assert len(rows) == 2
    arbitrary = fixture.connection.execute(
        """
        SELECT id FROM order_rows
        WHERE order_revision_id = ? AND isbn13 = ? LIMIT 1
        """,
        (order["revision_id"], isbn),
    ).fetchone()["id"]
    expected = next(row["id"] for row in rows if row["id"] != arbitrary)

    scanned = service.scan(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        session_id=session["session_id"],
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        isbn=isbn,
        expected_order_row_id=expected,
        idempotency_key="duplicate-isbn-targeted-scan",
        request_id=str(uuid.uuid4()),
    )

    assert scanned["code"] == "NORMAL"
    assert scanned["order_row_id"] == expected
    assert scanned["scanned_quantity"] == 1


def test_procurement_public_item_suppresses_unproven_mapping_payload() -> None:
    class StubProcurementImportService(ProcurementImportService):
        def _intent(self, school_id: str, import_id: str):
            return {
                "id": import_id,
                "kind": "QUOTE",
                "source_document_id": "source",
                "original_filename": "legacy.xlsx",
                "vendor_name": "업체",
                "target_revision_id": "approval",
                "status": "PENDING",
                "source_status": "FAILED",
                "detected_format": "XLSX",
                "parser_version": "tabular-v1",
                "template_version": None,
                "result_id": None,
                "created_at": NOW,
                "completed_at": None,
            }

        def _rows(self, intent):
            return []

        def _latest_file_result(self, intent):
            return {
                "total_rows": 0,
                "processed_rows": 0,
                "row_error_count": 0,
                "count_confidence": "UNVERIFIED",
                "error": {"code": "MAPPING_REQUIRED"},
                "mapping_required": {
                    "headers": ["제목"],
                    "sample_rows": [["책"]],
                    "private_path": "C:/database/internal.sqlite3",
                },
                "_public_mapping_payload_version": None,
            }

    item = StubProcurementImportService(None).public_item("school", "import")

    assert item["status"] == "FAILED"
    assert item["mapping_required"] is None
    assert "private_path" not in json.dumps(item, ensure_ascii=False)


@pytest.mark.parametrize(
    "code",
    [
        "OCR_UNAVAILABLE",
        "OCR_LANGUAGE_UNAVAILABLE",
        "OCR_TIMEOUT",
        "OCR_CANCELLED",
        "OCR_ERROR",
    ],
)
def test_procurement_pdf_ocr_errors_have_conversion_guidance(code: str) -> None:
    class StubProcurementImportService(ProcurementImportService):
        def _intent(self, school_id: str, import_id: str):
            return {
                "id": import_id,
                "kind": "QUOTE",
                "source_document_id": "source",
                "original_filename": "scan.pdf",
                "vendor_name": "업체",
                "target_revision_id": "approval",
                "status": "PENDING",
                "source_status": "FAILED",
                "detected_format": "PDF",
                "parser_version": "pdf-v2",
                "template_version": None,
                "result_id": None,
                "created_at": NOW,
                "completed_at": None,
            }

        def _rows(self, intent):
            return [
                {
                    "id": "row",
                    "status": "ROW_ERROR",
                    "fields_json": "{}",
                    "error_code": code,
                    "sheet_name": "1",
                    "source_row": 1,
                    "result_row_id": None,
                }
            ]

        def _latest_file_result(self, intent):
            return {
                "total_rows": 1,
                "processed_rows": 0,
                "row_error_count": 1,
                "count_confidence": "EXACT",
                "error": {"code": "ALL_LOGICAL_ROWS_FAILED"},
                "mapping_required": None,
                "_public_mapping_payload_version": None,
            }

    item = StubProcurementImportService(None).public_item("school", "import")

    message = item["rows"][0]["error"]["message"]
    assert "CSV·엑셀" in message
    assert "변환" in message
