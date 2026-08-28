"""Production durable-job handlers."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from suseoro.ingestion.contracts import DocumentRole, ParseResult, RowStatus
from suseoro.ingestion.parsers.docx import parse_docx
from suseoro.ingestion.parsers.hwp import parse_hwp
from suseoro.ingestion.parsers.hwpx import parse_hwpx
from suseoro.ingestion.parsers.marc import MarcParseResult, parse_marc
from suseoro.ingestion.parsers.pdf import parse_pdf
from suseoro.ingestion.parsers.tabular import parse_tabular
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
                       file.sha256,
                       file.storage_path, file.detected_format
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
                result = _parse_source(
                    Path(document["storage_path"]),
                    digest=document["sha256"],
                    detected_format=document["detected_format"],
                    role=document["role"],
                )
                _persist_parse_result(
                    connection,
                    document_id=document_id,
                    result=result,
                    completed_at=format_utc(context.clock()),
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
