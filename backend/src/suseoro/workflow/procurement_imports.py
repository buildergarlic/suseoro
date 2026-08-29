"""Durable composition of common-parser rows into quotes and deliveries."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from pydantic import ValidationError

from suseoro.api.schemas import DeliveryResponse, MappingRequired, QuoteResponse
from suseoro.jobs.public_errors import sanitize_public_mapping_required
from suseoro.jobs.repository import JobRepository
from suseoro.security.sessions import format_utc, utc_now
from suseoro.services.audit import record_audit_event
from suseoro.workflow._common import (
    WorkflowDomainError,
    mutation_transaction,
    require_role,
)
from suseoro.workflow.quotes import QuoteService
from suseoro.workflow.receiving import ReceivingService


class ProcurementImportRuleError(WorkflowDomainError):
    """Stable public rule failure for file-to-workflow composition."""


_ROW_ERROR_MESSAGES = {
    "MISSING_REQUIRED_FIELD": "필수 열 값을 확인해 주세요.",
    "PARSER_FAILURE": "파일을 안전하게 읽지 못했습니다. 원본 형식을 확인해 주세요.",
    "NO_LOGICAL_ROWS": "가져올 도서 행을 찾지 못했습니다.",
    "DOCUMENT_TABLE_REQUIRED": "표 구조를 확인하지 못했습니다. CSV·엑셀로 변환하거나 표가 있는 문서로 다시 올려 주세요.",
    "DOCUMENT_ARCHIVE_ERROR": "문서를 안전하게 읽지 못했습니다. CSV·엑셀 또는 정상 문서로 변환해 다시 올려 주세요.",
    "DOCUMENT_XML_ERROR": "문서 표를 안전하게 읽지 못했습니다. CSV·엑셀 또는 정상 문서로 변환해 다시 올려 주세요.",
    "PDF_ERROR": "PDF를 안전하게 읽지 못했습니다. 암호를 해제하거나 CSV·엑셀로 변환해 다시 올려 주세요.",
    "PDF_PAGE_ERROR": "PDF 표의 일부를 읽지 못했습니다. CSV·엑셀로 변환해 다시 올려 주세요.",
    "HWP_ENCRYPTED": "암호·배포용 HWP는 읽을 수 없습니다. 보호를 해제하거나 HWPX·CSV·엑셀로 변환해 다시 올려 주세요.",
    "HWP_DAMAGED": "HWP가 손상되었습니다. 복구하거나 HWPX·CSV·엑셀로 변환해 다시 올려 주세요.",
    "HWP_DAMAGED_RECORD": "HWP 표의 일부가 손상되었습니다. HWPX·CSV·엑셀로 변환해 다시 올려 주세요.",
    "HWP_UNSUPPORTED_OBJECT": "지원하지 않는 HWP 개체가 있습니다. HWPX·CSV·엑셀로 변환해 다시 올려 주세요.",
    "OCR_UNAVAILABLE": "PDF 글자를 인식할 수 없습니다. 텍스트 PDF 또는 CSV·엑셀로 변환해 다시 올려 주세요.",
    "OCR_LANGUAGE_UNAVAILABLE": "한국어 PDF 글자 인식이 준비되지 않았습니다. 텍스트 PDF 또는 CSV·엑셀로 변환해 다시 올려 주세요.",
    "OCR_TIMEOUT": "PDF 글자 인식 시간이 초과되었습니다. 페이지를 나누거나 CSV·엑셀로 변환해 다시 올려 주세요.",
    "OCR_CANCELLED": "PDF 글자 인식이 중단되었습니다. 다시 시도하거나 CSV·엑셀로 변환해 올려 주세요.",
    "OCR_ERROR": "PDF 글자를 안전하게 인식하지 못했습니다. 텍스트 PDF 또는 CSV·엑셀로 변환해 다시 올려 주세요.",
}


def _field_value(fields: dict[str, Any], *names: str) -> Any:
    for name in names:
        value = fields.get(name)
        if isinstance(value, dict) and value.get("value") not in (None, ""):
            return value["value"]
    return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _integer(value: Any, *, default: int | None = None) -> int | None:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        raise TypeError("boolean is not a number")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        raise ValueError("fractional number is not allowed")
    normalized = str(value).strip().replace(",", "").replace("원", "").replace(" ", "")
    return int(normalized)


def _boolean(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    normalized = str(value).strip().casefold()
    if normalized in {"1", "true", "yes", "y", "예", "품절", "o"}:
        return True
    if normalized in {"", "0", "false", "no", "n", "아니오", "정상", "x"}:
        return False
    raise ValueError("boolean value is not recognized")


def _normalized_row(row: sqlite3.Row, *, kind: str) -> dict[str, Any]:
    fields = json.loads(row["fields_json"])
    try:
        normalized = {
            "isbn": _text(_field_value(fields, "isbn")),
            "title": _text(_field_value(fields, "title")) or "",
            "author": _text(_field_value(fields, "author")),
            "edition": _text(_field_value(fields, "edition")),
            "quantity": _integer(
                _field_value(fields, "quantity"),
                default=1,
            ),
            "unit_price": _integer(_field_value(fields, "unit_price")),
        }
        if kind == "QUOTE":
            normalized.update(
                {
                    "publisher": _text(_field_value(fields, "publisher")),
                    "list_price": _integer(_field_value(fields, "list_price")),
                    "out_of_stock": _boolean(_field_value(fields, "out_of_stock")),
                }
            )
    except (TypeError, ValueError) as error:
        raise ProcurementImportRuleError(
            "PROCUREMENT_SOURCE_ROWS_INVALID",
            "수량·가격·품절 열의 값을 확인해 주세요.",
        ) from error
    return normalized


class ProcurementImportService:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def _intent(self, school_id: str, import_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            """
            SELECT imported.*, document.status AS source_status,
                   document.parser_version, document.template_version,
                   document.detected_format, document.completed_at AS source_completed_at,
                   file.original_filename
            FROM procurement_source_imports AS imported
            JOIN source_documents AS document
              ON document.id = imported.source_document_id
             AND document.school_id = imported.school_id
            JOIN source_files AS file ON file.id = document.source_file_id
            WHERE imported.id = ? AND imported.school_id = ?
            """,
            (import_id, school_id),
        ).fetchone()
        if row is None:
            raise ProcurementImportRuleError("PROCUREMENT_IMPORT_NOT_FOUND")
        return row

    def _latest_file_result(self, intent: sqlite3.Row) -> dict[str, Any] | None:
        job = self.connection.execute(
            """
            SELECT job.id
            FROM durable_jobs AS job
            JOIN job_file_results AS result ON result.job_id = job.id
            WHERE job.school_id = ? AND job.workspace_id = ?
              AND result.source_document_id = ?
              AND job.job_type IN ('INGEST', 'PARSE')
            ORDER BY job.created_at DESC, job.id DESC LIMIT 1
            """,
            (
                intent["school_id"],
                intent["workspace_id"],
                intent["source_document_id"],
            ),
        ).fetchone()
        if job is None:
            return None
        return next(
            (
                item
                for item in JobRepository(self.connection).file_results(job["id"])
                if item["source_document_id"] == intent["source_document_id"]
            ),
            None,
        )

    def _rows(self, intent: sqlite3.Row) -> list[sqlite3.Row]:
        return self.connection.execute(
            """
            SELECT source_row.*, linked.result_row_id
            FROM source_rows AS source_row
            LEFT JOIN procurement_source_import_rows AS linked
              ON linked.source_row_id = source_row.id
             AND linked.import_id = ?
            WHERE source_row.source_document_id = ?
            ORDER BY source_row.source_row, source_row.id
            """,
            (intent["id"], intent["source_document_id"]),
        ).fetchall()

    def public_item(self, school_id: str, import_id: str) -> dict[str, Any]:
        intent = self._intent(school_id, import_id)
        rows = self._rows(intent)
        latest = self._latest_file_result(intent)
        successful = sum(row["status"] == "SUCCESS" for row in rows)
        canonical_successful = sum(
            row["status"] == "SUCCESS"
            and bool(
                _text(
                    _field_value(
                        json.loads(row["fields_json"]),
                        "title",
                    )
                )
            )
            for row in rows
        )
        row_errors = sum(row["status"] == "ROW_ERROR" for row in rows)
        mapping_required = None
        if latest is not None:
            raw_mapping = sanitize_public_mapping_required(
                latest.get("error"),
                latest.get("mapping_required"),
                latest.get("_public_mapping_payload_version"),
            )
            if raw_mapping is not None:
                try:
                    mapping_required = MappingRequired.model_validate(
                        raw_mapping
                    ).model_dump(mode="json")
                except ValidationError:
                    mapping_required = None
        if intent["status"] == "IMPORTED":
            status = "IMPORTED_PARTIAL" if row_errors else "IMPORTED"
        elif mapping_required is not None:
            status = "MAPPING_REQUIRED"
        elif intent["source_status"] == "PENDING":
            status = "PARSING"
        elif intent["source_status"] == "FAILED" or not canonical_successful:
            status = "FAILED"
        elif row_errors:
            status = "PARTIAL"
        else:
            status = "READY"
        total_rows = int(latest["total_rows"]) if latest else len(rows)
        processed_rows = int(latest["processed_rows"]) if latest else successful
        effective_errors = int(latest["row_error_count"]) if latest else row_errors
        public_rows = []
        for row in rows:
            error = None
            if row["status"] == "ROW_ERROR":
                code = str(row["error_code"] or "ROW_ERROR")
                error = {
                    "code": code,
                    "message": _ROW_ERROR_MESSAGES.get(
                        code, "이 행의 값을 확인해 주세요."
                    ),
                }
            public_rows.append(
                {
                    "source_row_id": row["id"],
                    "status": row["status"],
                    "provenance": {
                        "sheet": row["sheet_name"],
                        "source_row": row["source_row"],
                    },
                    "error": error,
                    "result_row_id": row["result_row_id"],
                }
            )
        return {
            "import_id": intent["id"],
            "kind": intent["kind"],
            "source_id": intent["source_document_id"],
            "filename": intent["original_filename"] or "unnamed",
            "vendor_name": intent["vendor_name"],
            "target_revision_id": intent["target_revision_id"],
            "status": status,
            "detected_format": intent["detected_format"],
            "parser_version": intent["parser_version"],
            "template_version": intent["template_version"],
            "total_rows": total_rows,
            "processed_rows": processed_rows,
            "row_error_count": effective_errors,
            "count_confidence": latest["count_confidence"] if latest else "EXACT",
            "mapping_required": mapping_required,
            "result_id": intent["result_id"],
            "rows": public_rows,
            "created_at": intent["created_at"],
            "completed_at": intent["completed_at"],
        }

    def list(self, school_id: str, workspace_id: str) -> list[dict[str, Any]]:
        ids = self.connection.execute(
            """
            SELECT id FROM procurement_source_imports
            WHERE school_id = ? AND workspace_id = ?
            ORDER BY created_at DESC, id DESC LIMIT 100
            """,
            (school_id, workspace_id),
        ).fetchall()
        return [self.public_item(school_id, row["id"]) for row in ids]

    def compose(
        self,
        *,
        school_id: str,
        import_id: str,
        actor_id: str,
        actor_roles: tuple[str, ...],
        workspace_version: int,
        idempotency_key: str,
        request_id: str,
    ) -> dict[str, Any]:
        with mutation_transaction(self.connection):
            return self._compose(
                school_id=school_id,
                import_id=import_id,
                actor_id=actor_id,
                actor_roles=actor_roles,
                workspace_version=workspace_version,
                idempotency_key=idempotency_key,
                request_id=request_id,
            )

    def _compose(
        self,
        *,
        school_id: str,
        import_id: str,
        actor_id: str,
        actor_roles: tuple[str, ...],
        workspace_version: int,
        idempotency_key: str,
        request_id: str,
    ) -> dict[str, Any]:
        require_role(
            self.connection,
            school_id=school_id,
            actor_id=actor_id,
            actor_roles=actor_roles,
            role="OPERATOR",
            error_type=ProcurementImportRuleError,
        )
        intent = self._intent(school_id, import_id)
        if intent["status"] == "IMPORTED":
            stored = json.loads(intent["response_json"])
            return {
                "import_id": import_id,
                "kind": intent["kind"],
                "status": "IMPORTED",
                "result_id": intent["result_id"],
                "state": stored["state"],
                "row_version": stored["row_version"],
            }
        public = self.public_item(school_id, import_id)
        if public["status"] == "MAPPING_REQUIRED":
            raise ProcurementImportRuleError("PROCUREMENT_MAPPING_REQUIRED")
        if public["status"] == "PARSING":
            raise ProcurementImportRuleError("PROCUREMENT_SOURCE_PROCESSING")
        if public["status"] == "FAILED":
            raise ProcurementImportRuleError("PROCUREMENT_SOURCE_FAILED")
        if public["status"] == "PARTIAL":
            raise ProcurementImportRuleError("PROCUREMENT_SOURCE_PARTIAL")
        source_rows = [row for row in self._rows(intent) if row["status"] == "SUCCESS"]
        if not source_rows:
            raise ProcurementImportRuleError("PROCUREMENT_SOURCE_FAILED")
        normalized_rows = [
            _normalized_row(row, kind=intent["kind"]) for row in source_rows
        ]
        # The immutable import identity is the child command identity. The
        # caller's HTTP retry key must never create a second quote/delivery.
        service_key = f"procurement-import:{import_id}"
        if intent["kind"] == "QUOTE":
            result = QuoteService(self.connection).ingest(
                school_id=school_id,
                workspace_id=intent["workspace_id"],
                approval_revision_id=intent["target_revision_id"],
                actor_id=actor_id,
                actor_roles=actor_roles,
                workspace_version=workspace_version,
                vendor_name=intent["vendor_name"],
                rows=normalized_rows,
                reason=intent["reason"],
                idempotency_key=service_key,
                request_id=request_id,
            )
            typed_result = QuoteResponse.model_validate(result).model_dump(mode="json")
            result_id = str(typed_result["quote_id"])
            result_row_ids = [str(row["quote_row_id"]) for row in typed_result["rows"]]
        else:
            result = ReceivingService(self.connection).record_delivery(
                school_id=school_id,
                workspace_id=intent["workspace_id"],
                order_revision_id=intent["target_revision_id"],
                actor_id=actor_id,
                actor_roles=actor_roles,
                workspace_version=workspace_version,
                rows=normalized_rows,
                reason=intent["reason"],
                idempotency_key=service_key,
                request_id=request_id,
            )
            typed_result = DeliveryResponse.model_validate(result).model_dump(
                mode="json"
            )
            result_id = str(typed_result["delivery_batch_id"])
            result_row_ids = [
                str(row["id"])
                for row in self.connection.execute(
                    """
                    SELECT id FROM delivery_rows
                    WHERE delivery_batch_id = ? ORDER BY rowid
                    """,
                    (result_id,),
                ).fetchall()
            ]
        if len(result_row_ids) != len(source_rows):
            raise RuntimeError("procurement import row accounting mismatch")
        now = format_utc(utc_now())
        encoded = json.dumps(
            typed_result,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        updated = self.connection.execute(
            """
            UPDATE procurement_source_imports
            SET status = 'IMPORTED', result_id = ?, response_json = ?, completed_at = ?
            WHERE id = ? AND school_id = ? AND status = 'PENDING'
            """,
            (result_id, encoded, now, import_id, school_id),
        )
        if updated.rowcount != 1:
            raise ProcurementImportRuleError(
                "PROCUREMENT_IMPORT_CONFLICT",
                "다른 요청이 먼저 이 파일을 반영했습니다. 작업 상태를 새로고침해 주세요.",
            )
        self.connection.executemany(
            """
            INSERT INTO procurement_source_import_rows (
                import_id, source_row_id, result_row_id, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            (
                (import_id, row["id"], result_row_id, now)
                for row, result_row_id in zip(source_rows, result_row_ids, strict=True)
            ),
        )
        record_audit_event(
            self.connection,
            actor_id=actor_id,
            school_id=school_id,
            action="PROCUREMENT_SOURCE_IMPORTED",
            entity_type="procurement_source_import",
            entity_id=import_id,
            before={"status": "PENDING"},
            after={
                "status": "IMPORTED",
                "kind": intent["kind"],
                "result_id": result_id,
                "source_document_id": intent["source_document_id"],
            },
            request_id=request_id,
        )
        return {
            "import_id": import_id,
            "kind": intent["kind"],
            "status": "IMPORTED",
            "result_id": result_id,
            "state": typed_result["state"],
            "row_version": typed_result["row_version"],
        }
