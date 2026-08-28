"""Production durable-job handlers."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime
from typing import Any

from suseoro.jobs.repository import JobRepository
from suseoro.jobs.runner import DurableJobRunner, JobContext
from suseoro.security.sessions import utc_now
from suseoro.services.comparison import ComparisonService


def _chunks(values: tuple[str, ...], size: int):
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


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
    """Wire the production COMPARE handler into a claim-fenced runner."""
    return DurableJobRunner(
        JobRepository(connection),
        handlers={
            "COMPARE": build_comparison_handler(
                connection,
                file_batch_size=file_batch_size,
                row_batch_size=row_batch_size,
            )
        },
        clock=clock,
    )
