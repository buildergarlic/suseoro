"""Production durable-job handlers."""

from __future__ import annotations

import csv
import json
import logging
import re
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
from suseoro.ingestion.mapping import canonical_field_for_header, infer_mapping
from suseoro.ingestion.parsers.docx import parse_docx
from suseoro.ingestion.parsers.hwp import parse_hwp
from suseoro.ingestion.parsers.hwpx import parse_hwpx
from suseoro.ingestion.parsers.marc import MarcParseResult, parse_marc
from suseoro.ingestion.parsers.pdf import parse_pdf
from suseoro.ingestion.parsers.tabular import parse_tabular
from suseoro.ingestion.templates import MappingTemplateStore, ParserCache
from suseoro.jobs.public_errors import mapping_required_error, parser_failure
from suseoro.jobs.repository import JobRepository
from suseoro.jobs.runner import DurableJobRunner, JobContext
from suseoro.jobs.source_snapshot import source_rows_snapshot
from suseoro.security.sessions import format_utc, utc_now
from suseoro.services.audit import record_audit_event
from suseoro.services.comparison import ComparisonService

logger = logging.getLogger(__name__)


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


_TABLE_NODE = re.compile(
    r"(?P<table>(?:^|/)table\[(?P<table_number>\d+)\])"
    r"/row\[(?P<row>\d+)\]/cell\[(?P<cell>\d+)\]$"
)
_DOCUMENT_FORMATS = frozenset({"DOCX", "HWP", "HWPX", "PDF"})


def _document_text(row: ParsedRow) -> str:
    field = row.fields.get("text")
    value = field.value if field is not None else row.raw_values.get("text")
    return "" if value is None else str(value).strip()


def _unique_headers(values: list[str]) -> list[str]:
    headers: list[str] = []
    used: dict[str, int] = {}
    for index, value in enumerate(values, start=1):
        base = value.strip() or f"column_{index}"
        used[base] = used.get(base, 0) + 1
        headers.append(base if used[base] == 1 else f"{base}__{used[base]}")
    return headers


def _table_row(
    *,
    result: ParseResult,
    headers: list[str],
    values: list[str],
    sheet: str | None,
    source_row: int,
    source_columns: dict[str, int] | None = None,
) -> ParsedRow:
    padded = values + [""] * max(0, len(headers) - len(values))
    return ParsedRow(
        status=RowStatus.SUCCESS,
        provenance=Provenance(
            sheet=sheet,
            source_row=source_row,
            source_columns=source_columns
            or {header: index for index, header in enumerate(headers, start=1)},
            source_file_sha256=next(
                (
                    row.provenance.source_file_sha256
                    for row in result.rows
                    if row.provenance.source_file_sha256
                ),
                None,
            ),
        ),
        raw_values={header: padded[index] for index, header in enumerate(headers)},
        fields={},
    )


def _xml_document_table_rows(result: ParseResult) -> list[ParsedRow]:
    tables: dict[tuple[str, str], dict[int, dict[int, ParsedRow]]] = {}
    for row in result.rows:
        if row.status != RowStatus.SUCCESS:
            continue
        node = str(row.raw_values.get("node") or "")
        match = _TABLE_NODE.search(node)
        if match is None:
            continue
        sheet_part = (row.provenance.sheet or "").split("#", 1)[0]
        table_path = node.rsplit("/row[", 1)[0]
        table = tables.setdefault((sheet_part, table_path), {})
        table.setdefault(int(match.group("row")), {})[int(match.group("cell"))] = row
    all_parsed: list[ParsedRow] = []
    for (sheet_part, table_path), rows in tables.items():
        ordered = sorted(rows.items())
        if len(ordered) < 2:
            continue
        _, header_cells = ordered[0]
        headers = _unique_headers(
            [_document_text(header_cells[index]) for index in sorted(header_cells)]
        )
        if len(headers) < 2:
            continue
        parsed: list[ParsedRow] = []
        for row_number, cells in ordered[1:]:
            values = [
                _document_text(cells[index]) if index in cells else ""
                for index in range(1, len(headers) + 1)
            ]
            if not any(values):
                continue
            parsed.append(
                _table_row(
                    result=result,
                    headers=headers,
                    values=values,
                    sheet=f"{sheet_part}#{table_path}",
                    source_row=len(all_parsed) + len(parsed) + 1,
                )
            )
        if parsed:
            all_parsed.extend(parsed)
    return all_parsed


def _hwp_document_table_rows(result: ParseResult) -> list[ParsedRow]:
    by_table: dict[tuple[str, int], list[ParsedRow]] = {}
    for row in result.rows:
        if row.status == RowStatus.SUCCESS and row.raw_values.get("cell") is not None:
            by_table.setdefault(
                (
                    row.provenance.sheet or "BodyText",
                    int(row.raw_values.get("table") or 1),
                ),
                [],
            ).append(row)
    all_parsed: list[ParsedRow] = []
    for (stream, table_number), cells in by_table.items():
        cells.sort(key=lambda row: int(row.raw_values.get("cell") or 0))
        candidates: list[tuple[int, int, list[str]]] = []
        for width in range(2, min(20, len(cells) // 2) + 1):
            if len(cells) % width:
                continue
            headers = [_document_text(row) for row in cells[:width]]
            recognized = sum(
                canonical_field_for_header(header) is not None for header in headers
            )
            if recognized:
                candidates.append((recognized, width, headers))
        if not candidates:
            continue
        _, width, headers = max(candidates, key=lambda item: (item[0], item[1]))
        parsed: list[ParsedRow] = []
        for offset in range(width, len(cells), width):
            group = cells[offset : offset + width]
            values = [_document_text(row) for row in group]
            if not any(values):
                continue
            parsed.append(
                _table_row(
                    result=result,
                    headers=_unique_headers(headers),
                    values=values,
                    sheet=f"{stream}#table[{table_number}]",
                    source_row=len(all_parsed) + len(parsed) + 1,
                )
            )
        all_parsed.extend(parsed)
    return all_parsed


def _pdf_document_table_rows(result: ParseResult) -> list[ParsedRow]:
    lines: list[tuple[ParsedRow, str]] = []
    for row in result.rows:
        if row.status != RowStatus.SUCCESS:
            continue
        lines.extend(
            (row, line.strip())
            for line in _document_text(row).splitlines()
            if line.strip()
        )
    if len(lines) < 2:
        return []
    for delimiter in ("\t", ",", ";", "|"):
        header_values = next(csv.reader([lines[0][1]], delimiter=delimiter))
        if len(header_values) < 2:
            continue
        headers = _unique_headers(header_values)
        parsed: list[ParsedRow] = []
        for line_number, (source, line) in enumerate(lines[1:], start=2):
            values = next(csv.reader([line], delimiter=delimiter))
            if len(values) != len(headers):
                parsed = []
                break
            parsed.append(
                _table_row(
                    result=result,
                    headers=headers,
                    values=values,
                    sheet=source.provenance.sheet,
                    source_row=line_number,
                )
            )
        if parsed:
            return parsed
    return []


def _procurement_document_table(result: ParseResult) -> ParseResult:
    """Recover a durable row matrix from the common document parser outcome."""
    if (
        result.role != DocumentRole.VENDOR_QUOTE
        or result.detected_format not in _DOCUMENT_FORMATS
    ):
        return result
    successful_rows = [row for row in result.rows if row.status == RowStatus.SUCCESS]
    if successful_rows and all(
        len(row.raw_values) >= 2
        and set(row.provenance.source_columns) == set(row.raw_values)
        and sorted(row.provenance.source_columns.values())
        == list(range(1, len(row.raw_values) + 1))
        and not row.fields
        for row in successful_rows
    ):
        # _table_row already produced this header-keyed matrix. Original
        # paragraph/cell/page records retain parser metadata in raw_values and
        # a text field, so they cannot satisfy this structural check. Cache
        # reads sort JSON keys, so use the preserved column indices instead of
        # dictionary insertion order when recognizing the recovered matrix.
        return result
    if result.detected_format in {"DOCX", "HWPX"}:
        rows = _xml_document_table_rows(result)
    elif result.detected_format == "HWP":
        rows = _hwp_document_table_rows(result)
    else:
        rows = _pdf_document_table_rows(result)
    parser_errors = [row for row in result.rows if row.status == RowStatus.ROW_ERROR]
    if rows:
        rows.extend(parser_errors)
    elif parser_errors:
        rows = parser_errors
    else:
        first = result.rows[0] if result.rows else None
        rows = [
            ParsedRow(
                status=RowStatus.ROW_ERROR,
                provenance=Provenance(
                    sheet=first.provenance.sheet if first else None,
                    source_row=first.provenance.source_row if first else 0,
                    source_file_sha256=(
                        first.provenance.source_file_sha256 if first else None
                    ),
                ),
                raw_values={"detected_format": result.detected_format},
                fields={},
                error_code="DOCUMENT_TABLE_REQUIRED",
                error_message=(
                    "No reliable procurement table was found; convert the table "
                    "to CSV/XLSX or use a document with explicit table cells"
                ),
            )
        ]
    kwargs = dict(result.__dict__)
    kwargs.update(rows=rows, header_rows={})
    return type(result)(**kwargs)


def parser_version_for_format(detected_format: str) -> str:
    return {
        "DOCX": "docx-v2",
        "HWP": "hwp-v2",
        "HWPX": "hwpx-v2",
        "MARC": "marc-v1",
        "PDF": "pdf-v2",
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


def _preview_scalar(value: Any) -> str | int | float | bool | None:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


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
        missing = (
            [
                field
                for field in sorted(required)
                if field not in fields or fields[field].value in (None, "")
            ]
            if row.status == RowStatus.SUCCESS
            or row.error_code == "MISSING_REQUIRED_FIELD"
            else []
        )
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
    config_version: int,
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
        "FAILED"
        if not result.rows
        else "ROW_ERROR"
        if any(row.status == RowStatus.ROW_ERROR for row in result.rows)
        else "SUCCESS"
    )
    activation_allowed = (
        int(result.activation_allowed) if isinstance(result, MarcParseResult) else 1
    )
    if not result.rows:
        activation_allowed = 0
    connection.execute(
        """
        UPDATE source_documents
        SET status = ?, template_version = ?, activation_allowed = ?, completed_at = ?,
            parsed_config_version = ?
        WHERE id = ?
        """,
        (
            status,
            result.template_version,
            activation_allowed,
            completed_at,
            config_version,
            document_id,
        ),
    )


def _resolve_repair_after_parse(
    connection: sqlite3.Connection,
    *,
    document_id: str,
    request_id: str,
) -> None:
    repair = connection.execute(
        """
        SELECT repair.id, repair.school_id, repair.actor_id, repair.generation,
               repair.role, repair.vendor_scope,
               repair.requested_start_local_date,
               repair.requested_through_local_date,
               document.status, document.parsed_config_version,
               document.requested_start_local_date AS document_start,
               document.requested_through_local_date AS document_through,
               config.role AS config_role, config.vendor_scope AS config_vendor,
               config.row_version AS config_version,
               (
                   SELECT COUNT(*) FROM source_rows AS source_row
                   WHERE source_row.source_document_id = document.id
                     AND source_row.status = 'SUCCESS'
               ) AS successful_rows
        FROM upload_repair_obligations AS repair
        JOIN source_documents AS document
          ON document.id = repair.pending_source_document_id
        JOIN source_configurations AS config
          ON config.source_document_id = document.id
        WHERE repair.pending_source_document_id = ?
          AND repair.status = 'REPAIRING'
        """,
        (document_id,),
    ).fetchone()
    if repair is None:
        return
    valid = (
        repair["role"] != DocumentRole.UNKNOWN.value
        and repair["config_role"] == repair["role"]
        and repair["config_vendor"] == repair["vendor_scope"]
        and repair["document_start"] == repair["requested_start_local_date"]
        and repair["document_through"] == repair["requested_through_local_date"]
        and repair["status"] in {"SUCCESS", "ROW_ERROR"}
        and repair["parsed_config_version"] == repair["config_version"]
        and int(repair["successful_rows"]) > 0
    )
    if not valid:
        return
    updated = connection.execute(
        """
        UPDATE upload_repair_obligations
        SET status = 'RESOLVED', resolved_source_document_id = ?,
            pending_source_document_id = NULL, updated_at = ?
        WHERE id = ? AND status = 'REPAIRING'
          AND pending_source_document_id = ? AND generation = ?
        """,
        (
            document_id,
            format_utc(utc_now()),
            repair["id"],
            document_id,
            repair["generation"],
        ),
    )
    if updated.rowcount != 1:
        return
    record_audit_event(
        connection,
        actor_id=repair["actor_id"],
        school_id=repair["school_id"],
        action="UPLOAD_REPAIR_RESOLVED",
        entity_type="upload_repair_obligation",
        entity_id=repair["id"],
        before={"status": "REPAIRING", "generation": repair["generation"]},
        after={"status": "RESOLVED", "source_id": document_id},
        request_id=request_id,
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
                       COALESCE(config.remember_template, 0) AS remember_template,
                       COALESCE(config.row_version, 1) AS config_version,
                       (
                           SELECT COUNT(*) FROM workspace_sources AS owner
                           WHERE owner.source_document_id = document.id
                             AND owner.school_id = document.school_id
                       ) AS workspace_link_count
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
                if int(document["workspace_link_count"]) != 1:
                    raise ValueError(
                        "ingestion source document has multiple workspaces"
                    )
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
                base_result = _procurement_document_table(
                    _parse_result_from_payload(cached.result)
                )
                successful_base_rows = [
                    row for row in base_result.rows if row.status == RowStatus.SUCCESS
                ]
                headers = (
                    list(successful_base_rows[0].raw_values)
                    if successful_base_rows
                    else []
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
                preview_rows = [
                    [_preview_scalar(row.raw_values.get(header)) for header in headers]
                    for row in successful_base_rows[:20]
                ]
                inference = infer_mapping(headers, preview_rows)
                available_semantics = {
                    str(semantic) for semantic in effective_mapping.values() if semantic
                }
                available_semantics.update(
                    semantic
                    for header in headers
                    if (
                        semantic := canonical_field_for_header(header.split("__", 1)[0])
                    )
                )
                available_semantics.update(
                    field for row in base_result.rows for field in row.fields
                )
                missing_required = sorted(required - available_semantics)
                tabular_low_confidence = (
                    base_result.detected_format
                    in {"CSV", "TSV", "TXT", "XLS", "XLSX", "XLSB", "ODS"}
                    and inference.confidence < 0.75
                )
                if (
                    base_result.rows
                    and headers
                    and (
                        missing_required
                        or (
                            required
                            and not configured_mapping
                            and template is None
                            and tabular_low_confidence
                        )
                    )
                ):
                    JobRepository(connection).record_file_result(
                        job_id=context.job.id,
                        claim_token=context.job.claim_token,
                        claim_generation=context.job.claim_generation,
                        source_document_id=document_id,
                        status="PARTIAL",
                        total_rows=len(base_result.rows),
                        processed_rows=0,
                        error=mapping_required_error(
                            {
                                "headers": headers,
                                "preview_rows": preview_rows,
                                "suggested_mapping": inference.mapping,
                                "required_fields": sorted(required),
                                "confidence": inference.confidence,
                                "questions": inference.questions,
                            }
                        ),
                        public_mapping_payload_version=1,
                    )
                    connection.commit()
                    context.checkpoint(stage="PARSING", current=current, total=total)
                    continue
                result = _apply_mapping(
                    base_result,
                    role=role,
                    mapping=effective_mapping,
                )
                current_config = connection.execute(
                    """
                    SELECT row_version FROM source_configurations
                    WHERE source_document_id = ? AND school_id = ?
                    """,
                    (document_id, context.job.school_id),
                ).fetchone()
                if current_config is None or int(current_config["row_version"]) != int(
                    document["config_version"]
                ):
                    raise ValueError("source configuration changed during parse")
                _persist_parse_result(
                    connection,
                    document_id=document_id,
                    result=result,
                    completed_at=format_utc(context.clock()),
                    config_version=int(document["config_version"]),
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
                    if not result.rows
                    else "FAILED"
                    if result.rows and row_errors == len(result.rows)
                    else "PARTIAL"
                    if row_errors
                    else "SUCCESS"
                )
                item_error = {"code": "NO_LOGICAL_ROWS"} if not result.rows else None
                JobRepository(connection).record_file_result(
                    job_id=context.job.id,
                    claim_token=context.job.claim_token,
                    claim_generation=context.job.claim_generation,
                    source_document_id=document_id,
                    status=item_status,
                    total_rows=len(result.rows),
                    processed_rows=len(result.rows) - row_errors,
                    row_error_count=row_errors,
                    error=item_error,
                )
                _resolve_repair_after_parse(
                    connection,
                    document_id=document_id,
                    request_id=str(payload.get("request_id") or context.job.id),
                )
            except Exception:  # One file must not drop successfully parsed peers.
                logger.exception("Source document %s parsing failed", document_id)
                public_error = parser_failure()
                connection.execute(
                    """
                    UPDATE source_documents SET status = 'FAILED', completed_at = ?
                        , parsed_config_version = NULL
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
                    total_rows=0,
                    processed_rows=0,
                    error=public_error,
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
                        public_error["message"],
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
        raw_snapshot = payload.get("source_snapshot")
        if payload.get("source_snapshot_version") != 1 or not isinstance(
            raw_snapshot, list
        ):
            raise RuntimeError("comparison source snapshot is required")
        snapshot_by_id = {
            str(item.get("id")): item
            for item in raw_snapshot
            if isinstance(item, dict) and item.get("id") is not None
        }

        def validate_snapshot(document_id: str | None = None) -> None:
            selected = (
                (document_id,)
                if document_id is not None
                else tuple(snapshot_by_id.keys())
            )
            if set(snapshot_by_id) != set(document_ids):
                raise RuntimeError("comparison source snapshot is incomplete")
            for selected_id in selected:
                current = connection.execute(
                    """
                    SELECT document.status, document.parser_version,
                           document.completed_at, document.parsed_config_version,
                           file.sha256,
                           COALESCE(config.role, document.role) AS role,
                           COALESCE(config.row_version, 1) AS config_version,
                           COALESCE(config.mapping_json, '{}') AS mapping_json
                    FROM source_documents document
                    JOIN source_files file ON file.id = document.source_file_id
                    JOIN workspace_sources link
                      ON link.source_document_id = document.id
                    LEFT JOIN source_configurations config
                      ON config.source_document_id = document.id
                    WHERE document.id = ? AND document.school_id = ?
                      AND link.workspace_id = ? AND link.school_id = ?
                    """,
                    (
                        selected_id,
                        context.job.school_id,
                        context.job.workspace_id,
                        context.job.school_id,
                    ),
                ).fetchone()
                expected = snapshot_by_id[selected_id]
                if current is None:
                    raise RuntimeError("comparison source snapshot changed")
                row_count, row_digest = source_rows_snapshot(connection, selected_id)
                if (
                    any(
                        current[name] != expected.get(name)
                        for name in (
                            "status",
                            "sha256",
                            "role",
                            "config_version",
                            "parsed_config_version",
                            "mapping_json",
                            "parser_version",
                            "completed_at",
                        )
                    )
                    or row_count != expected.get("row_count")
                    or row_digest != expected.get("row_digest")
                ):
                    raise RuntimeError("comparison source snapshot changed")
            catalog_id = payload.get("catalog_version_id")
            if catalog_id is not None:
                active = connection.execute(
                    """
                    SELECT 1 FROM catalog_versions
                    WHERE id = ? AND school_id = ? AND status = 'ACTIVE'
                    """,
                    (catalog_id, context.job.school_id),
                ).fetchone()
                if active is None:
                    raise RuntimeError("comparison catalog snapshot changed")

        validate_snapshot()
        document_ids_json = json.dumps(document_ids)
        documents = connection.execute(
            """
            SELECT id FROM source_documents
            WHERE id IN (SELECT value FROM json_each(?)) AND school_id = ?
            """,
            (document_ids_json, context.job.school_id),
        ).fetchall()
        if {row["id"] for row in documents} != set(document_ids):
            raise ValueError("COMPARE source document school scope mismatch")
        total = connection.execute(
            """
            SELECT COUNT(*) FROM source_rows
            WHERE source_document_id IN (SELECT value FROM json_each(?))
            """,
            (document_ids_json,),
        ).fetchone()[0]
        completed = connection.execute(
            """
            SELECT COUNT(*) FROM comparison_row_results
            WHERE workspace_id = ?
              AND source_document_id IN (SELECT value FROM json_each(?))
            """,
            (context.job.workspace_id, document_ids_json),
        ).fetchone()[0]
        context.checkpoint(stage="VALIDATING", current=completed, total=total)
        service = ComparisonService(connection)
        for file_batch in _chunks(document_ids, file_batch_size):
            context.ensure_not_cancelled()
            for document_id in file_batch:
                validate_snapshot(document_id)
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
                            validate_snapshot(document_id)
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
                    validate_snapshot(document_id)
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
                        """
                        SELECT COUNT(*) FROM comparison_row_results
                        WHERE workspace_id = ?
                          AND source_document_id IN (
                              SELECT value FROM json_each(?)
                          )
                        """,
                        (context.job.workspace_id, document_ids_json),
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
