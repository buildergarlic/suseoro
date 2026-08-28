"""Production durable-job handlers."""

from __future__ import annotations

import csv
import json
import sqlite3
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from suseoro.ingestion.contracts import (
    DocumentRole,
    FieldWarning,
    ParsedField,
    ParsedRow,
    ParseResult,
    Provenance,
    RowStatus,
)
from suseoro.ingestion.mapping import canonical_field_for_header
from suseoro.ingestion.parsers.docx import parse_docx
from suseoro.ingestion.parsers.hwp import parse_hwp
from suseoro.ingestion.parsers.hwpx import parse_hwpx
from suseoro.ingestion.parsers.marc import MarcParseResult, parse_marc
from suseoro.ingestion.parsers.pdf import parse_pdf
from suseoro.ingestion.parsers.tabular import parse_tabular
from suseoro.ingestion.templates import MappingTemplateStore, ParserCache
from suseoro.jobs.repository import JobRepository
from suseoro.jobs.runner import DurableJobRunner, JobContext
from suseoro.security.sessions import format_utc, utc_now
from suseoro.services.comparison import ComparisonService


def _chunks(values: tuple[str, ...], size: int):
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


def _parse_source(path: Path, *, digest: str, detected_format: str, role: str):
    parser = {
        "DOCX": parse_docx,
        "HWP": parse_hwp,
        "HWPX": parse_hwpx,
        "MARC": parse_marc,
        "PDF": parse_pdf,
    }.get(detected_format, parse_tabular)
    return parser(path, role=DocumentRole(role), sha256=digest)


def _parse_source_preserving_unknown_headers(
    path: Path, *, digest: str, detected_format: str, role: str
) -> ParseResult:
    result = _parse_source(
        path, digest=digest, detected_format=detected_format, role=role
    )
    if detected_format not in {"CSV", "TSV", "TXT"} or any(
        row.raw_values for row in result.rows
    ):
        return result
    last_error: UnicodeDecodeError | None = None
    for encoding in ("utf-8-sig", "cp949", "euc-kr"):
        try:
            with path.open("r", encoding=encoding, newline="") as source:
                sample = source.read(4096)
                source.seek(0)
                delimiter = "\t" if detected_format == "TSV" else ","
                if detected_format == "TXT":
                    try:
                        delimiter = (
                            csv.Sniffer().sniff(sample, delimiters=",\t;").delimiter
                        )
                    except csv.Error:
                        delimiter = "\t"
                values = list(csv.reader(source, delimiter=delimiter))
            break
        except UnicodeDecodeError as error:
            last_error = error
    else:
        raise last_error or ValueError("source text encoding is unsupported")
    if not values:
        return result
    headers = [
        str(value).strip() or f"column_{index}"
        for index, value in enumerate(values[0], 1)
    ]
    rows = [
        ParsedRow(
            status=RowStatus.SUCCESS,
            provenance=Provenance(
                sheet=None,
                source_row=index,
                source_file_sha256=digest,
            ),
            raw_values={
                header: row[column] if column < len(row) else None
                for column, header in enumerate(headers)
            },
            fields={},
        )
        for index, row in enumerate(values[1:], start=2)
        if any(value.strip() for value in row)
    ]
    return ParseResult(
        role=DocumentRole(role),
        detected_format=detected_format,
        parser_version=result.parser_version,
        parser_backend=result.parser_backend,
        rows=rows,
        header_rows={"Sheet1": 1},
        encoding=encoding,
    )


def parser_version_for_format(detected_format: str) -> str:
    return {
        "DOCX": "docx-v1",
        "HWP": "hwp-v1",
        "HWPX": "hwpx-v1",
        "MARC": "marc-v1",
        "PDF": "pdf-v1",
    }.get(detected_format, "tabular-v1")


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _parse_result_payload(result: ParseResult) -> dict[str, Any]:
    return {
        "role": result.role.value,
        "detected_format": result.detected_format,
        "parser_version": result.parser_version,
        "parser_backend": result.parser_backend,
        "header_rows": result.header_rows,
        "template_version": result.template_version,
        "encoding": result.encoding,
        "source_kind": result.source_kind,
        "is_marc": isinstance(result, MarcParseResult),
        "activation_allowed": getattr(result, "activation_allowed", True),
        "rows": [
            {
                "status": row.status.value,
                "provenance": {
                    "sheet": row.provenance.sheet,
                    "source_row": row.provenance.source_row,
                    "source_columns": row.provenance.source_columns,
                    "source_file_sha256": row.provenance.source_file_sha256,
                },
                "raw_values": row.raw_values,
                "fields": {
                    name: {
                        "value": field.value,
                        "raw_value": field.raw_value,
                        "warnings": [warning.__dict__ for warning in field.warnings],
                    }
                    for name, field in row.fields.items()
                },
                "warnings": [warning.__dict__ for warning in row.warnings],
                "error_code": row.error_code,
                "error_message": row.error_message,
            }
            for row in result.rows
        ],
    }


def _parse_result_from_payload(payload: dict[str, Any]) -> ParseResult:
    rows = []
    for item in payload["rows"]:
        provenance = item["provenance"]
        rows.append(
            ParsedRow(
                status=RowStatus(item["status"]),
                provenance=Provenance(
                    sheet=provenance["sheet"],
                    source_row=int(provenance["source_row"]),
                    source_columns=dict(provenance.get("source_columns", {})),
                    source_file_sha256=provenance.get("source_file_sha256"),
                ),
                raw_values=dict(item["raw_values"]),
                fields={
                    name: ParsedField(
                        value=field["value"],
                        raw_value=field["raw_value"],
                        warnings=tuple(
                            FieldWarning(**warning)
                            for warning in field.get("warnings", [])
                        ),
                    )
                    for name, field in item["fields"].items()
                },
                warnings=tuple(
                    FieldWarning(**warning) for warning in item.get("warnings", [])
                ),
                error_code=item.get("error_code"),
                error_message=item.get("error_message"),
            )
        )
    result_type = MarcParseResult if payload.get("is_marc") else ParseResult
    kwargs = {
        "role": DocumentRole(payload["role"]),
        "detected_format": payload["detected_format"],
        "parser_version": payload["parser_version"],
        "parser_backend": payload["parser_backend"],
        "rows": rows,
        "header_rows": dict(payload.get("header_rows", {})),
        "template_version": payload.get("template_version"),
        "encoding": payload.get("encoding"),
        "source_kind": payload.get("source_kind", "FILE"),
    }
    if result_type is MarcParseResult:
        kwargs["activation_allowed"] = bool(payload.get("activation_allowed", True))
    return result_type(**kwargs)


def _required_fields(role: DocumentRole) -> set[str]:
    if role in {
        DocumentRole.PURCHASE_REQUEST,
        DocumentRole.VENDOR_QUOTE,
        DocumentRole.CATALOG_FULL,
        DocumentRole.CATALOG_DELTA_REGISTRATION,
        DocumentRole.CATALOG_DELTA_UPDATE,
    }:
        return {"title"}
    return set()


def _apply_mapping(
    result: ParseResult,
    *,
    role: DocumentRole,
    mapping: dict[str, str | None],
) -> ParseResult:
    required = _required_fields(role)
    remapped: list[ParsedRow] = []
    for row in result.rows:
        fields = dict(row.fields)
        source_columns = dict(row.provenance.source_columns)
        for column, (header, raw_value) in enumerate(row.raw_values.items(), start=1):
            semantic = mapping.get(header)
            if semantic is None:
                semantic = canonical_field_for_header(header.split("__", 1)[0])
            if not semantic:
                continue
            value = raw_value
            if semantic in {"isbn", "registration_number", "call_number"}:
                value = None if raw_value is None else str(raw_value)
            fields[semantic] = ParsedField(value=value, raw_value=raw_value)
            source_columns[semantic] = column
        missing = [
            field
            for field in sorted(required)
            if field not in fields or fields[field].value in (None, "")
        ]
        status = (
            RowStatus.ROW_ERROR
            if missing
            else RowStatus.SUCCESS
            if row.error_code == "MISSING_REQUIRED_FIELD"
            else row.status
        )
        remapped.append(
            ParsedRow(
                status=status,
                provenance=Provenance(
                    sheet=row.provenance.sheet,
                    source_row=row.provenance.source_row,
                    source_columns=source_columns,
                    source_file_sha256=row.provenance.source_file_sha256,
                ),
                raw_values=row.raw_values,
                fields=fields,
                warnings=row.warnings,
                error_code=(
                    "MISSING_REQUIRED_FIELD"
                    if missing
                    else None
                    if row.error_code == "MISSING_REQUIRED_FIELD"
                    else row.error_code
                ),
                error_message=(
                    "Missing required fields: " + ", ".join(missing)
                    if missing
                    else None
                    if row.error_code == "MISSING_REQUIRED_FIELD"
                    else row.error_message
                ),
            )
        )
    kwargs = dict(result.__dict__)
    kwargs.update(role=role, rows=remapped)
    return type(result)(**kwargs)


def _persist_parse_result(
    connection: sqlite3.Connection,
    *,
    document_id: str,
    result: ParseResult,
    completed_at: str,
) -> None:
    connection.execute(
        "DELETE FROM source_rows WHERE source_document_id = ?", (document_id,)
    )
    for row in result.rows:
        fields = {
            name: {
                "value": field.value,
                "raw_value": field.raw_value,
                "warnings": [
                    {"code": warning.code, "message": warning.message}
                    for warning in field.warnings
                ],
            }
            for name, field in row.fields.items()
        }
        warnings = [
            {"code": warning.code, "message": warning.message}
            for warning in row.warnings
        ]
        connection.execute(
            """
            INSERT INTO source_rows (
                id, source_document_id, sheet_name, source_row, status,
                raw_json, fields_json, warnings_json, error_code,
                error_message, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                document_id,
                row.provenance.sheet,
                row.provenance.source_row,
                row.status.value,
                _json(row.raw_values),
                _json(fields),
                _json(warnings),
                row.error_code,
                row.error_message,
                completed_at,
            ),
        )
    status = (
        "ROW_ERROR"
        if any(row.status == RowStatus.ROW_ERROR for row in result.rows)
        else "SUCCESS"
    )
    activation_allowed = (
        int(result.activation_allowed) if isinstance(result, MarcParseResult) else 1
    )
    connection.execute(
        """
        UPDATE source_documents
        SET status = ?, template_version = ?, activation_allowed = ?, completed_at = ?
        WHERE id = ?
        """,
        (
            status,
            result.template_version,
            activation_allowed,
            completed_at,
            document_id,
        ),
    )


def build_ingestion_handler(
    connection: sqlite3.Connection,
) -> Callable[[JobContext, dict[str, Any]], None]:
    """Build a checkpointed INGEST/PARSE handler with per-file outcomes."""

    def handle(context: JobContext, payload: dict[str, Any]) -> None:
        raw_document_ids = payload.get("source_document_ids")
        if not isinstance(raw_document_ids, list) or not raw_document_ids:
            raise ValueError("ingestion payload requires source_document_ids")
        document_ids = tuple(dict.fromkeys(str(value) for value in raw_document_ids))
        total = len(document_ids)
        context.checkpoint(stage="VALIDATING", current=0, total=total)
        for current, document_id in enumerate(document_ids, start=1):
            context.ensure_not_cancelled()
            document = connection.execute(
                """
                SELECT document.id, COALESCE(config.role, document.role) AS role,
                       document.parser_version, file.sha256,
                       file.storage_path, file.detected_format,
                       COALESCE(config.mapping_json, '{}') AS mapping_json,
                       COALESCE(config.vendor_scope, '*') AS vendor_scope,
                       COALESCE(config.remember_template, 0) AS remember_template
                FROM source_documents document
                JOIN source_files file ON file.id = document.source_file_id
                LEFT JOIN source_configurations config
                  ON config.source_document_id = document.id
                WHERE document.id = ? AND document.school_id = ?
                """,
                (document_id, context.job.school_id),
            ).fetchone()
            if document is None:
                raise ValueError("ingestion source document school scope mismatch")
            try:
                role = DocumentRole(document["role"])
                cached = ParserCache(connection).get_or_parse(
                    sha256=document["sha256"],
                    parser_version=document["parser_version"],
                    role=role,
                    parse=lambda storage_path=document["storage_path"], digest=document["sha256"], detected_format=document["detected_format"], configured_role=document["role"]: (
                        _parse_result_payload(
                            _parse_source_preserving_unknown_headers(
                                Path(storage_path),
                                digest=digest,
                                detected_format=detected_format,
                                role=configured_role,
                            )
                        )
                    ),
                    claim_token=context.job.claim_token,
                    claim_generation=context.job.claim_generation,
                )
                base_result = _parse_result_from_payload(cached.result)
                headers = (
                    list(base_result.rows[0].raw_values) if base_result.rows else []
                )
                required = _required_fields(role)
                configured_mapping = json.loads(document["mapping_json"])
                template_store = MappingTemplateStore(connection)
                template = None
                if not configured_mapping and headers:
                    template = template_store.find(
                        school_id=context.job.school_id,
                        vendor_scope=document["vendor_scope"],
                        role=role,
                        headers=headers,
                        required_fields=required,
                    )
                effective_mapping = (
                    configured_mapping
                    if configured_mapping
                    else template.mapping
                    if template
                    else {}
                )
                result = _apply_mapping(
                    base_result,
                    role=role,
                    mapping=effective_mapping,
                )
                _persist_parse_result(
                    connection,
                    document_id=document_id,
                    result=result,
                    completed_at=format_utc(context.clock()),
                )
                if document["remember_template"] and configured_mapping and headers:
                    template_store.save(
                        school_id=context.job.school_id,
                        vendor_scope=document["vendor_scope"],
                        role=role,
                        headers=headers,
                        mapping=configured_mapping,
                        required_fields=required,
                        template_version=result.template_version
                        or result.parser_version,
                    )
                row_errors = sum(
                    row.status == RowStatus.ROW_ERROR for row in result.rows
                )
                item_status = (
                    "FAILED"
                    if result.rows and row_errors == len(result.rows)
                    else "PARTIAL"
                    if row_errors
                    else "SUCCESS"
                )
                JobRepository(connection).record_file_result(
                    job_id=context.job.id,
                    claim_token=context.job.claim_token,
                    claim_generation=context.job.claim_generation,
                    source_document_id=document_id,
                    status=item_status,
                    total_rows=len(result.rows),
                    processed_rows=len(result.rows),
                )
            except Exception as error:  # noqa: BLE001 - one file must not drop peers
                connection.execute(
                    """
                    UPDATE source_documents SET status = 'FAILED', completed_at = ?
                    WHERE id = ? AND school_id = ?
                    """,
                    (
                        format_utc(context.clock()),
                        document_id,
                        context.job.school_id,
                    ),
                )
                JobRepository(connection).record_file_result(
                    job_id=context.job.id,
                    claim_token=context.job.claim_token,
                    claim_generation=context.job.claim_generation,
                    source_document_id=document_id,
                    status="FAILED",
                    total_rows=1,
                    processed_rows=1,
                    error={"type": type(error).__name__, "message": str(error)},
                )
                connection.execute(
                    """
                    INSERT INTO source_rows (
                        id, source_document_id, source_row, status, raw_json,
                        fields_json, warnings_json, error_code, error_message,
                        created_at
                    ) VALUES (?, ?, 0, 'ROW_ERROR', '{}', '{}', '[]',
                              'PARSER_FAILURE', ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        document_id,
                        str(error),
                        format_utc(context.clock()),
                    ),
                )
            connection.commit()
            context.checkpoint(stage="PARSING", current=current, total=total)

    return handle


def build_comparison_handler(
    connection: sqlite3.Connection,
    *,
    file_batch_size: int = 4,
    row_batch_size: int = 100,
) -> Callable[[JobContext, dict[str, Any]], None]:
    """Build a bounded, checkpointed COMPARE handler over real persisted rows."""
    if file_batch_size <= 0 or row_batch_size <= 0:
        raise ValueError("comparison batch sizes must be positive")

    def handle(context: JobContext, payload: dict[str, Any]) -> None:
        if context.job.workspace_id is None:
            raise ValueError("COMPARE job requires a workspace")
        raw_document_ids = payload.get("source_document_ids")
        if not isinstance(raw_document_ids, list) or not raw_document_ids:
            raise ValueError("COMPARE payload requires source_document_ids")
        document_ids = tuple(dict.fromkeys(str(value) for value in raw_document_ids))
        placeholders = ",".join("?" for _ in document_ids)
        documents = connection.execute(
            f"""
            SELECT id FROM source_documents
            WHERE id IN ({placeholders}) AND school_id = ?
            """,
            (*document_ids, context.job.school_id),
        ).fetchall()
        if {row["id"] for row in documents} != set(document_ids):
            raise ValueError("COMPARE source document school scope mismatch")
        total = connection.execute(
            f"""
            SELECT COUNT(*) FROM source_rows
            WHERE source_document_id IN ({placeholders})
            """,
            document_ids,
        ).fetchone()[0]
        completed = connection.execute(
            f"""
            SELECT COUNT(*) FROM comparison_row_results
            WHERE workspace_id = ? AND source_document_id IN ({placeholders})
            """,
            (context.job.workspace_id, *document_ids),
        ).fetchone()[0]
        context.checkpoint(stage="VALIDATING", current=completed, total=total)
        service = ComparisonService(connection)
        for file_batch in _chunks(document_ids, file_batch_size):
            context.ensure_not_cancelled()
            for document_id in file_batch:
                processed_batch = False
                while True:
                    rows = connection.execute(
                        """
                        SELECT sr.id
                        FROM source_rows sr
                        WHERE sr.source_document_id = ?
                          AND NOT EXISTS (
                              SELECT 1 FROM comparison_row_results crr
                              WHERE crr.workspace_id = ?
                                AND crr.source_row_id = sr.id
                          )
                        ORDER BY sr.source_row, sr.id
                        LIMIT ?
                        """,
                        (document_id, context.job.workspace_id, row_batch_size),
                    ).fetchall()
                    row_ids = tuple(row["id"] for row in rows)
                    if not row_ids:
                        if not processed_batch:
                            service.compare_documents(
                                school_id=context.job.school_id,
                                workspace_id=context.job.workspace_id,
                                source_document_ids=(document_id,),
                                source_row_ids=(),
                                job_id=context.job.id,
                                claim_token=context.job.claim_token,
                                claim_generation=context.job.claim_generation,
                            )
                            # Results are the recovery source of truth. Publish
                            # the completed file boundary before checking for a
                            # concurrently requested cancellation.
                            connection.commit()
                        break
                    service.compare_documents(
                        school_id=context.job.school_id,
                        workspace_id=context.job.workspace_id,
                        source_document_ids=(document_id,),
                        source_row_ids=row_ids,
                        job_id=context.job.id,
                        claim_token=context.job.claim_token,
                        claim_generation=context.job.claim_generation,
                    )
                    # A process death after this commit may leave job progress
                    # behind, but restart derives it from immutable row results
                    # and never scores this batch twice.
                    connection.commit()
                    processed_batch = True
                    completed = connection.execute(
                        f"""
                        SELECT COUNT(*) FROM comparison_row_results
                        WHERE workspace_id = ?
                          AND source_document_id IN ({placeholders})
                        """,
                        (context.job.workspace_id, *document_ids),
                    ).fetchone()[0]
                    context.checkpoint(
                        stage="COMPARING", current=completed, total=total
                    )
            context.ensure_not_cancelled()
        context.checkpoint(stage="FINALIZING", current=total, total=total)

    return handle


def build_job_runner(
    connection: sqlite3.Connection,
    *,
    file_batch_size: int = 4,
    row_batch_size: int = 100,
    clock: Callable[[], datetime] = utc_now,
) -> DurableJobRunner:
    """Wire production ingestion and comparison into a claim-fenced runner."""
    ingestion_handler = build_ingestion_handler(connection)
    return DurableJobRunner(
        JobRepository(connection),
        handlers={
            "INGEST": ingestion_handler,
            "PARSE": ingestion_handler,
            "COMPARE": build_comparison_handler(
                connection,
                file_batch_size=file_batch_size,
                row_batch_size=row_batch_size,
            ),
        },
        clock=clock,
    )
