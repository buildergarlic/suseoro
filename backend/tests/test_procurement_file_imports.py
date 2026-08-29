from __future__ import annotations

import io
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook
from workflow_fixtures import WorkflowFixture, make_workflow_fixture

from suseoro.api.app import create_app
from suseoro.config import Settings
from suseoro.db.connection import connect
from suseoro.jobs.handlers import build_job_runner, parser_version_for_format
from suseoro.repositories.auth import UserRecord, issue_session
from suseoro.workflow.approvals import ApprovalService
from suseoro.workflow.orders import OrderService
from suseoro.workflow.quotes import QuoteService


def _approve(fixture: WorkflowFixture) -> dict[str, object]:
    requested = ApprovalService(fixture.connection).request_approval(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        workspace_version=fixture.workspace_version(),
        candidate_collection_revision=fixture.candidate_collection_revision(),
        budget_won=50_000,
        reason="파일 견적 검증 승인 요청",
        idempotency_key="file-import-approval-request",
        request_id=str(uuid.uuid4()),
    )
    ApprovalService(fixture.connection).approve(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        revision_id=str(requested["revision_id"]),
        actor_id=fixture.reviewer_id,
        actor_roles=("REVIEWER",),
        workspace_version=fixture.workspace_version(),
        reason="파일 입력 검증 승인",
        idempotency_key="file-import-approval-decision",
        request_id=str(uuid.uuid4()),
    )
    return requested


def _client(
    fixture: WorkflowFixture, tmp_path: Path
) -> tuple[TestClient, Settings, str]:
    settings = Settings(
        data_dir=tmp_path,
        database_path=tmp_path / "workflow.sqlite3",
        secure_cookies=False,
    )
    user = UserRecord(
        id=fixture.operator_id,
        school_id=fixture.school_id,
        username="operator",
        display_name="담당자",
        roles=("OPERATOR",),
    )
    issued = issue_session(fixture.connection, user, 3_600)
    fixture.connection.commit()
    client = TestClient(create_app(settings), base_url="https://testserver")
    client.cookies.set("suseoro_session", issued.session_token)
    client.cookies.set("suseoro_csrf", issued.csrf_token)
    return client, settings, issued.csrf_token


def _headers(csrf: str, key: str, *, version: int | None = None) -> dict[str, str]:
    result = {
        "X-CSRF-Token": csrf,
        "Idempotency-Key": key,
        "X-Request-ID": str(uuid.uuid4()),
    }
    if version is not None:
        result["If-Match"] = f'"{version}"'
    return result


def _run_ingestion(settings: Settings) -> None:
    with connect(settings.database_path) as worker_connection:
        completed = build_job_runner(worker_connection).run_once()
    assert completed is not None
    assert completed.status in {"SUCCEEDED", "PARTIAL", "FAILED"}


def _xlsx_bytes(rows: list[list[object]]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    for row in rows:
        sheet.append(row)
    payload = io.BytesIO()
    workbook.save(payload)
    workbook.close()
    return payload.getvalue()


def _pdf_bytes(text: str) -> bytes:
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("ascii")
    objects = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [4 0 R] /Count 1 >>",
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        4: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 3 0 R >> >> /Contents 5 0 R >>",
        5: f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream",
    }
    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for object_id in range(1, 6):
        offsets.append(len(output))
        output.extend(f"{object_id} 0 obj\n".encode())
        output.extend(objects[object_id])
        output.extend(b"\nendobj\n")
    xref = len(output)
    output.extend(b"xref\n0 6\n0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(
        f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(output)


def test_csv_quote_uses_immutable_common_parser_then_durable_typed_composition(
    tmp_path: Path,
) -> None:
    fixture = make_workflow_fixture(tmp_path)
    fixture.add_candidate(
        title="도서관의 책",
        author="김사서",
        isbn="9788937464010",
        quantity=2,
        unit_price=12_000,
    )
    approved = _approve(fixture)
    client, settings, csrf = _client(fixture, tmp_path)
    csv_payload = (
        "ISBN,제목,저자,수량,공급가,표시가격,품절,판본\r\n"
        "9788937464010,도서관의 책,김사서,2,11000,12000,아니오,개정판\r\n"
    ).encode("utf-8-sig")

    with client:
        uploaded = client.post(
            f"/api/v2/workspaces/{fixture.workspace_id}/sources",
            headers=_headers(csrf, "quote-file-upload"),
            files={"files": ("quote.csv", csv_payload, "text/csv")},
            data={
                "role": "VENDOR_QUOTE",
                "vendor_scope": "푸른서점",
                "procurement_kind": "QUOTE",
                "target_revision_id": approved["revision_id"],
                "reason": "푸른서점 견적 비교",
            },
        )
        assert uploaded.status_code == 202
        item = uploaded.json()["items"][0]
        assert item["procurement_import_id"]
        immutable_source_id = item["source_id"]

        _run_ingestion(settings)
        before = client.get(
            f"/api/v2/workspaces/{fixture.workspace_id}/procurement-imports"
        )
        assert before.status_code == 200
        pending = before.json()["items"][0]
        assert pending["status"] == "READY"
        assert pending["kind"] == "QUOTE"
        assert pending["detected_format"] == "CSV"
        assert pending["parser_version"] == "tabular-v1"
        assert pending["total_rows"] == 1
        assert pending["processed_rows"] == 1
        assert pending["row_error_count"] == 0
        assert pending["rows"][0]["provenance"]["source_row"] == 2
        assert isinstance(pending["rows"][0]["provenance"]["sheet"], str)

        composed = client.post(
            f"/api/v2/procurement-imports/{item['procurement_import_id']}/compose",
            headers=_headers(
                csrf,
                "quote-file-compose",
                version=fixture.workspace_version(),
            ),
        )
        assert composed.status_code == 201
        assert composed.json()["kind"] == "QUOTE"
        assert composed.json()["status"] == "IMPORTED"

        after = client.get(
            f"/api/v2/workspaces/{fixture.workspace_id}/procurement-imports"
        ).json()["items"][0]
        assert after["status"] == "IMPORTED"
        assert after["result_id"] == composed.json()["result_id"]
        assert after["rows"][0]["result_row_id"]

        source = client.get(f"/api/v2/sources/{immutable_source_id}")
        remap = client.patch(
            f"/api/v2/sources/{immutable_source_id}/mapping",
            headers=_headers(
                csrf,
                "imported-source-remap",
                version=source.json()["row_version"],
            ),
            json={
                "role": "VENDOR_QUOTE",
                "vendor_scope": "푸른서점",
                "mapping": source.json()["mapping"],
                "remember_template": False,
            },
        )
        assert remap.status_code == 409
        assert remap.json()["detail"]["code"] == "SOURCE_ALREADY_IMPORTED"

        reparsed = client.post(
            f"/api/v2/sources/{immutable_source_id}/parse",
            headers=_headers(csrf, "imported-source-reparse"),
        )
        assert reparsed.status_code == 409
        assert reparsed.json()["detail"]["code"] == "SOURCE_ALREADY_IMPORTED"

    with connect(settings.database_path) as connection:
        source = connection.execute(
            """
            SELECT file.storage_path, file.sha256, document.status
            FROM source_documents AS document
            JOIN source_files AS file ON file.id = document.source_file_id
            WHERE document.id = ?
            """,
            (immutable_source_id,),
        ).fetchone()
        quote = connection.execute(
            "SELECT vendor_name, total_won, discount_won FROM vendor_quotes WHERE id = ?",
            (composed.json()["result_id"],),
        ).fetchone()
    assert Path(source["storage_path"]).read_bytes() == csv_payload
    assert len(source["sha256"]) == 64
    assert source["status"] == "SUCCESS"
    assert dict(quote) == {
        "vendor_name": "푸른서점",
        "total_won": 22_000,
        "discount_won": 2_000,
    }


def test_procurement_compose_rolls_back_child_when_intent_seal_crashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A post-child crash must leave neither a quote nor a stranded intent."""
    fixture = make_workflow_fixture(tmp_path)
    fixture.add_candidate(
        title="원자적으로 반영할 책",
        author="김사서",
        isbn="9788937464010",
        unit_price=12_000,
    )
    approved = _approve(fixture)
    client, settings, csrf = _client(fixture, tmp_path)
    payload = (
        "ISBN,제목,저자,수량,공급가\r\n"
        "9788937464010,원자적으로 반영할 책,김사서,1,11000\r\n"
    ).encode("utf-8-sig")

    with client:
        uploaded = client.post(
            f"/api/v2/workspaces/{fixture.workspace_id}/sources",
            headers=_headers(csrf, "atomic-compose-upload"),
            files={"files": ("quote.csv", payload, "text/csv")},
            data={
                "role": "VENDOR_QUOTE",
                "vendor_scope": "원자성서점",
                "procurement_kind": "QUOTE",
                "target_revision_id": approved["revision_id"],
                "reason": "원자성 검증",
            },
        )
        import_id = uploaded.json()["items"][0]["procurement_import_id"]
        _run_ingestion(settings)
        initial_version = fixture.workspace_version()
        real_ingest = QuoteService.ingest

        def crash_after_real_ingest(service, **kwargs):
            real_ingest(service, **kwargs)
            raise RuntimeError("simulated crash after child mutation")

        with monkeypatch.context() as patcher:
            patcher.setattr(QuoteService, "ingest", crash_after_real_ingest)
            failed = client.post(
                f"/api/v2/procurement-imports/{import_id}/compose",
                headers=_headers(csrf, "atomic-compose-first", version=initial_version),
            )
            assert failed.status_code == 500

        with connect(settings.database_path) as connection:
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM vendor_quotes WHERE workspace_id = ?",
                    (fixture.workspace_id,),
                ).fetchone()[0]
                == 0
            )
            assert (
                connection.execute(
                    "SELECT status FROM procurement_source_imports WHERE id = ?",
                    (import_id,),
                ).fetchone()["status"]
                == "PENDING"
            )
            assert (
                connection.execute(
                    "SELECT row_version FROM acquisition_workspaces WHERE id = ?",
                    (fixture.workspace_id,),
                ).fetchone()["row_version"]
                == initial_version
            )

        retried = client.post(
            f"/api/v2/procurement-imports/{import_id}/compose",
            headers=_headers(csrf, "atomic-compose-retry", version=initial_version),
        )
        assert retried.status_code == 201
        assert retried.json()["status"] == "IMPORTED"

    with connect(settings.database_path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM vendor_quotes WHERE workspace_id = ?",
                (fixture.workspace_id,),
            ).fetchone()[0]
            == 1
        )


def test_concurrent_procurement_composition_seals_one_result(
    tmp_path: Path,
) -> None:
    """Concurrent requests for one ready source receive one durable result."""
    fixture = make_workflow_fixture(tmp_path)
    fixture.add_candidate(
        title="동시 반영할 책",
        author="김사서",
        isbn="9788937464010",
        unit_price=12_000,
    )
    approved = _approve(fixture)
    client, settings, csrf = _client(fixture, tmp_path)
    payload = (
        "ISBN,제목,저자,수량,공급가\r\n9788937464010,동시 반영할 책,김사서,1,11000\r\n"
    ).encode("utf-8-sig")
    with client:
        uploaded = client.post(
            f"/api/v2/workspaces/{fixture.workspace_id}/sources",
            headers=_headers(csrf, "concurrent-compose-upload"),
            files={"files": ("quote.csv", payload, "text/csv")},
            data={
                "role": "VENDOR_QUOTE",
                "vendor_scope": "동시성서점",
                "procurement_kind": "QUOTE",
                "target_revision_id": approved["revision_id"],
                "reason": "동시 반영 검증",
            },
        )
        import_id = uploaded.json()["items"][0]["procurement_import_id"]
        _run_ingestion(settings)
        version = fixture.workspace_version()
        session_token = client.cookies.get("suseoro_session")

    contenders = [
        TestClient(create_app(settings), base_url="https://testserver")
        for _ in range(2)
    ]
    for contender in contenders:
        contender.cookies.set("suseoro_session", session_token)
        contender.cookies.set("suseoro_csrf", csrf)
    start = Barrier(2)

    def compose(contender: TestClient, key: str):
        start.wait(timeout=5)
        return contender.post(
            f"/api/v2/procurement-imports/{import_id}/compose",
            headers=_headers(csrf, key, version=version),
        )

    with contenders[0], contenders[1], ThreadPoolExecutor(max_workers=2) as pool:
        futures = (
            pool.submit(compose, contenders[0], "concurrent-compose-a"),
            pool.submit(compose, contenders[1], "concurrent-compose-b"),
        )
        responses = [future.result(timeout=10) for future in futures]

    assert [response.status_code for response in responses] == [201, 201]
    assert len({response.json()["result_id"] for response in responses}) == 1
    with connect(settings.database_path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM vendor_quotes WHERE workspace_id = ?",
                (fixture.workspace_id,),
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                """
                SELECT COUNT(*) FROM procurement_source_import_rows
                WHERE import_id = ?
                """,
                (import_id,),
            ).fetchone()[0]
            == 1
        )


def test_procurement_composition_reads_only_canonical_parser_fields(
    tmp_path: Path,
) -> None:
    """Raw cells cannot bypass the parser's canonical semantic mapping."""
    fixture = make_workflow_fixture(tmp_path)
    fixture.add_candidate(
        title="정규 필드만 쓸 책",
        author="김사서",
        isbn="9788937464010",
        unit_price=12_000,
    )
    approved = _approve(fixture)
    client, settings, csrf = _client(fixture, tmp_path)
    payload = (
        "ISBN,제목,저자,수량,공급가,품절\r\n"
        "9788937464010,정규 필드만 쓸 책,김사서,3,11000,예\r\n"
    ).encode("utf-8-sig")

    with client:
        uploaded = client.post(
            f"/api/v2/workspaces/{fixture.workspace_id}/sources",
            headers=_headers(csrf, "canonical-only-upload"),
            files={"files": ("quote.csv", payload, "text/csv")},
            data={
                "role": "VENDOR_QUOTE",
                "vendor_scope": "정규필드서점",
                "procurement_kind": "QUOTE",
                "target_revision_id": approved["revision_id"],
                "reason": "정규 필드 경계 검증",
            },
        )
        import_id = uploaded.json()["items"][0]["procurement_import_id"]
        source_id = uploaded.json()["items"][0]["source_id"]
        _run_ingestion(settings)

        # Simulate a historical normalized snapshot where optional canonical
        # fields were not established, while the immutable raw cells remain.
        with connect(settings.database_path) as connection:
            row = connection.execute(
                "SELECT id, fields_json FROM source_rows WHERE source_document_id = ?",
                (source_id,),
            ).fetchone()
            fields = json.loads(row["fields_json"])
            fields = {"title": fields["title"]}
            connection.execute(
                "UPDATE source_rows SET fields_json = ? WHERE id = ?",
                (json.dumps(fields, ensure_ascii=False), row["id"]),
            )
            connection.commit()

        composed = client.post(
            f"/api/v2/procurement-imports/{import_id}/compose",
            headers=_headers(
                csrf, "canonical-only-compose", version=fixture.workspace_version()
            ),
        )
        assert composed.status_code == 201
        quote = client.get(f"/api/v2/quotes/{composed.json()['result_id']}").json()

    assert quote["total_won"] == 0
    assert quote["missing_price_count"] == 1
    assert quote["out_of_stock_count"] == 0
    assert quote["rows"][0]["quantity"] == 1
    assert quote["rows"][0]["isbn13"] is None


def test_xlsx_delivery_manifest_composes_only_persisted_normalized_rows(
    tmp_path: Path,
) -> None:
    fixture = make_workflow_fixture(tmp_path)
    fixture.add_candidate(
        title="차분한 수서",
        author="이담당",
        isbn="9788936434267",
        quantity=2,
        unit_price=15_000,
    )
    approved = _approve(fixture)
    quote = QuoteService(fixture.connection).ingest(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        approval_revision_id=str(approved["revision_id"]),
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        workspace_version=fixture.workspace_version(),
        vendor_name="새봄서점",
        rows=[
            {
                "isbn": "9788936434267",
                "title": "차분한 수서",
                "author": "이담당",
                "quantity": 2,
                "unit_price": 14_000,
            }
        ],
        reason="발주용 견적",
        idempotency_key="delivery-seed-quote",
        request_id=str(uuid.uuid4()),
    )
    order_service = OrderService(fixture.connection, tmp_path / "orders")
    order = order_service.generate_revision(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        approval_revision_id=str(approved["revision_id"]),
        quote_id=str(quote["quote_id"]),
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        workspace_version=fixture.workspace_version(),
        reason="발주 파일 생성",
        idempotency_key="delivery-seed-order",
        request_id=str(uuid.uuid4()),
    )
    order_service.mark_sent(
        school_id=fixture.school_id,
        workspace_id=fixture.workspace_id,
        order_revision_id=str(order["revision_id"]),
        actor_id=fixture.operator_id,
        actor_roles=("OPERATOR",),
        workspace_version=fixture.workspace_version(),
        reason="업체 전달 완료",
        idempotency_key="delivery-seed-sent",
        request_id=str(uuid.uuid4()),
    )
    client, settings, csrf = _client(fixture, tmp_path)
    manifest = _xlsx_bytes(
        [
            ["ISBN", "제목", "저자", "수량", "단가", "판본"],
            ["9788936434267", "차분한 수서", "이담당", 1, 14_000, "초판"],
            ["9788936434267", "차분한 수서", "이담당", 1, 14_000, "초판"],
        ]
    )

    with client:
        uploaded = client.post(
            f"/api/v2/workspaces/{fixture.workspace_id}/sources",
            headers=_headers(csrf, "delivery-file-upload"),
            files={
                "files": (
                    "delivery.xlsx",
                    manifest,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
            data={
                "role": "VENDOR_QUOTE",
                "vendor_scope": "납품명세서:새봄서점",
                "procurement_kind": "DELIVERY",
                "target_revision_id": order["revision_id"],
                "reason": "1차 납품명세서 비교",
            },
        )
        assert uploaded.status_code == 202
        import_id = uploaded.json()["items"][0]["procurement_import_id"]
        _run_ingestion(settings)

        ready = client.get(
            f"/api/v2/workspaces/{fixture.workspace_id}/procurement-imports"
        ).json()["items"][0]
        assert ready["status"] == "READY"
        assert ready["kind"] == "DELIVERY"
        assert ready["detected_format"] == "XLSX"
        assert ready["processed_rows"] == 2

        composed = client.post(
            f"/api/v2/procurement-imports/{import_id}/compose",
            headers=_headers(
                csrf,
                "delivery-file-compose",
                version=fixture.workspace_version(),
            ),
        )
        assert composed.status_code == 201
        assert composed.json()["kind"] == "DELIVERY"
        assert composed.json()["status"] == "IMPORTED"
        progress = client.get(
            f"/api/v2/workspaces/{fixture.workspace_id}/receiving/status"
        ).json()
        assert progress["delivery_count"] == 1
        assert progress["delivered_quantity"] == 2


def test_mapping_and_row_errors_are_reload_safe_with_public_provenance(
    tmp_path: Path,
) -> None:
    fixture = make_workflow_fixture(tmp_path)
    fixture.add_candidate(
        title="도서관의 책",
        author="김사서",
        isbn="9788937464010",
        unit_price=12_000,
    )
    approved = _approve(fixture)
    client, settings, csrf = _client(fixture, tmp_path)

    with client:
        uploaded = client.post(
            f"/api/v2/workspaces/{fixture.workspace_id}/sources",
            headers=_headers(csrf, "mapping-file-upload"),
            files={
                "files": (
                    "unknown.csv",
                    "A,B,C\r\n9788937464010,도서관의 책,12000\r\n".encode(),
                    "text/csv",
                )
            },
            data={
                "role": "VENDOR_QUOTE",
                "vendor_scope": "처음 보는 업체",
                "procurement_kind": "QUOTE",
                "target_revision_id": approved["revision_id"],
                "reason": "열 연결 확인",
            },
        )
        import_id = uploaded.json()["items"][0]["procurement_import_id"]
        _run_ingestion(settings)
        current = client.get(
            f"/api/v2/workspaces/{fixture.workspace_id}/procurement-imports"
        ).json()["items"][0]
        assert current["import_id"] == import_id
        assert current["status"] == "MAPPING_REQUIRED"
        assert current["mapping_required"]["headers"] == ["A", "B", "C"]
        assert current["mapping_required"]["preview_rows"] == [
            ["9788937464010", "도서관의 책", "12000"]
        ]
        assert current["count_confidence"] == "EXACT"

        blocked = client.post(
            f"/api/v2/procurement-imports/{import_id}/compose",
            headers=_headers(
                csrf,
                "mapping-file-compose",
                version=fixture.workspace_version(),
            ),
        )
        assert blocked.status_code == 409
        assert blocked.json()["detail"]["code"] == "PROCUREMENT_MAPPING_REQUIRED"


def test_partial_source_never_silently_drops_failed_rows_during_composition(
    tmp_path: Path,
) -> None:
    fixture = make_workflow_fixture(tmp_path)
    fixture.add_candidate(
        title="도서관의 책",
        author="김사서",
        isbn="9788937464010",
        unit_price=12_000,
    )
    approved = _approve(fixture)
    client, settings, csrf = _client(fixture, tmp_path)
    payload = (
        "ISBN,제목,저자,수량,단가\r\n"
        "9788937464010,도서관의 책,김사서,1,11000\r\n"
        "9788936434267,,이담당,1,9000\r\n"
    ).encode("utf-8-sig")

    with client:
        uploaded = client.post(
            f"/api/v2/workspaces/{fixture.workspace_id}/sources",
            headers=_headers(csrf, "partial-procurement-upload"),
            files={"files": ("partial.csv", payload, "text/csv")},
            data={
                "role": "VENDOR_QUOTE",
                "vendor_scope": "부분 오류 업체",
                "procurement_kind": "QUOTE",
                "target_revision_id": approved["revision_id"],
                "reason": "부분 오류 검증",
            },
        )
        import_id = uploaded.json()["items"][0]["procurement_import_id"]
        _run_ingestion(settings)
        current = client.get(
            f"/api/v2/workspaces/{fixture.workspace_id}/procurement-imports"
        ).json()["items"][0]
        assert current["status"] == "PARTIAL"
        assert current["total_rows"] == 2
        assert current["processed_rows"] == 1
        assert current["row_error_count"] == 1
        assert current["rows"][1]["error"]["code"] == "MISSING_REQUIRED_FIELD"

        blocked = client.post(
            f"/api/v2/procurement-imports/{import_id}/compose",
            headers=_headers(
                csrf,
                "partial-procurement-compose",
                version=fixture.workspace_version(),
            ),
        )
        assert blocked.status_code == 409
        assert blocked.json()["detail"]["code"] == "PROCUREMENT_SOURCE_PARTIAL"
    assert (
        fixture.connection.execute("SELECT COUNT(*) FROM vendor_quotes").fetchone()[0]
        == 0
    )


def test_pdf_is_retained_and_routed_but_fails_closed_before_row_composition(
    tmp_path: Path,
) -> None:
    fixture = make_workflow_fixture(tmp_path)
    fixture.add_candidate(
        title="PDF 견적 도서",
        author="김사서",
        isbn="9788937464010",
        unit_price=12_000,
    )
    approved = _approve(fixture)
    client, settings, csrf = _client(fixture, tmp_path)
    payload = _pdf_bytes("ISBN 9788937464010 PDF quote 12000")

    with client:
        uploaded = client.post(
            f"/api/v2/workspaces/{fixture.workspace_id}/sources",
            headers=_headers(csrf, "pdf-procurement-upload"),
            files={"files": ("quote.pdf", payload, "application/pdf")},
            data={
                "role": "VENDOR_QUOTE",
                "vendor_scope": "PDF 업체",
                "procurement_kind": "QUOTE",
                "target_revision_id": approved["revision_id"],
                "reason": "PDF 원본 보존 검증",
            },
        )
        body = uploaded.json()
        source_id = body["items"][0]["source_id"]
        import_id = body["items"][0]["procurement_import_id"]
        _run_ingestion(settings)
        current = client.get(
            f"/api/v2/workspaces/{fixture.workspace_id}/procurement-imports"
        ).json()["items"][0]
        assert current["detected_format"] == "PDF"
        assert current["parser_version"] == "pdf-v1"
        assert current["status"] == "UNSUPPORTED_FORMAT"
        blocked = client.post(
            f"/api/v2/procurement-imports/{import_id}/compose",
            headers=_headers(
                csrf,
                "pdf-procurement-compose",
                version=fixture.workspace_version(),
            ),
        )
        assert blocked.status_code == 409
        assert blocked.json()["detail"]["code"] == "PROCUREMENT_FORMAT_NOT_COMPOSABLE"
    with connect(settings.database_path) as connection:
        source_path = connection.execute(
            """
            SELECT file.storage_path FROM source_documents AS document
            JOIN source_files AS file ON file.id = document.source_file_id
            WHERE document.id = ?
            """,
            (source_id,),
        ).fetchone()["storage_path"]
    assert Path(source_path).read_bytes() == payload


@pytest.mark.parametrize(
    ("detected_format", "parser_version"),
    [
        ("CSV", "tabular-v1"),
        ("XLSX", "tabular-v1"),
        ("PDF", "pdf-v1"),
        ("DOCX", "docx-v1"),
        ("HWPX", "hwpx-v1"),
        ("HWP", "hwp-v1"),
    ],
)
def test_procurement_file_formats_keep_the_common_parser_routing_contract(
    detected_format: str, parser_version: str
) -> None:
    assert parser_version_for_format(detected_format) == parser_version
