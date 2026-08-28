"""SQLite repository for cancellable, retryable, recoverable jobs."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from suseoro.security.sessions import format_utc, parse_utc, utc_now


@dataclass(frozen=True)
class Job:
    id: str
    school_id: str
    workspace_id: str | None
    job_type: str
    status: str
    stage: str
    payload: dict[str, Any]
    progress_current: int
    progress_total: int | None
    cancel_requested_at: datetime | None
    heartbeat_at: datetime | None
    error: dict[str, Any] | None
    retry_count: int
    claim_token: str | None
    claim_generation: int
    created_at: datetime
    updated_at: datetime


def _job(row: sqlite3.Row | None) -> Job | None:
    if row is None:
        return None
    return Job(
        id=row["id"],
        school_id=row["school_id"],
        workspace_id=row["workspace_id"],
        job_type=row["job_type"],
        status=row["status"],
        stage=row["stage"],
        payload=json.loads(row["payload_json"]),
        progress_current=row["progress_current"],
        progress_total=row["progress_total"],
        cancel_requested_at=(
            parse_utc(row["cancel_requested_at"])
            if row["cancel_requested_at"]
            else None
        ),
        heartbeat_at=parse_utc(row["heartbeat_at"]) if row["heartbeat_at"] else None,
        error=json.loads(row["error_json"]) if row["error_json"] else None,
        retry_count=row["retry_count"],
        claim_token=row["claim_token"],
        claim_generation=row["claim_generation"],
        created_at=parse_utc(row["created_at"]),
        updated_at=parse_utc(row["updated_at"]),
    )


class JobRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def create(
        self,
        *,
        school_id: str,
        job_type: str,
        payload: dict[str, Any],
        workspace_id: str | None = None,
        progress_total: int | None = None,
        now: datetime | None = None,
    ) -> Job:
        if workspace_id is not None:
            workspace = self.connection.execute(
                "SELECT school_id FROM acquisition_workspaces WHERE id = ?",
                (workspace_id,),
            ).fetchone()
            if workspace is None or workspace["school_id"] != school_id:
                raise ValueError("workspace does not belong to the requested school")
        timestamp = format_utc(now or utc_now())
        job_id = str(uuid.uuid4())
        self.connection.execute(
            """
            INSERT INTO durable_jobs (
                id, school_id, workspace_id, job_type, status, stage,
                payload_json, progress_current, progress_total,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'QUEUED', 'QUEUED', ?, 0, ?, ?, ?)
            """,
            (
                job_id,
                school_id,
                workspace_id,
                job_type,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                progress_total,
                timestamp,
                timestamp,
            ),
        )
        return self.get(job_id)

    def get(self, job_id: str) -> Job | None:
        return _job(
            self.connection.execute(
                "SELECT * FROM durable_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        )

    def claim_next(self, *, now: datetime | None = None) -> Job | None:
        timestamp = format_utc(now or utc_now())
        while True:
            row = self.connection.execute(
                """
                SELECT id FROM durable_jobs
                WHERE status = 'QUEUED'
                ORDER BY created_at, id
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            claimed = self.connection.execute(
                """
                UPDATE durable_jobs
                SET status = 'RUNNING', stage = 'STARTING',
                    claim_token = ?, claim_generation = claim_generation + 1,
                    heartbeat_at = ?, updated_at = ?
                WHERE id = ? AND status = 'QUEUED'
                """,
                (str(uuid.uuid4()), timestamp, timestamp, row["id"]),
            )
            if claimed.rowcount == 1:
                return self.get(row["id"])

    def update_progress(
        self,
        job_id: str,
        *,
        claim_token: str,
        stage: str,
        current: int,
        total: int | None,
        now: datetime | None = None,
    ) -> Job:
        if current < 0 or (total is not None and (total < 0 or current > total)):
            raise ValueError("invalid job progress")
        timestamp = format_utc(now or utc_now())
        updated = self.connection.execute(
            """
            UPDATE durable_jobs
            SET stage = ?, progress_current = ?, progress_total = ?,
                heartbeat_at = ?, updated_at = ?
            WHERE id = ? AND status = 'RUNNING' AND claim_token = ?
            """,
            (stage, current, total, timestamp, timestamp, job_id, claim_token),
        )
        if updated.rowcount != 1:
            raise RuntimeError("job claim is no longer current")
        return self.get(job_id)

    def assert_claim(
        self,
        job_id: str,
        claim_token: str,
        claim_generation: int | None = None,
    ) -> Job:
        job = self.get(job_id)
        if (
            job is None
            or job.status != "RUNNING"
            or job.claim_token != claim_token
            or (
                claim_generation is not None
                and job.claim_generation != claim_generation
            )
        ):
            raise RuntimeError("job claim is no longer current")
        return job

    def fence_claim(
        self,
        job_id: str,
        claim_token: str,
        claim_generation: int | None = None,
    ) -> Job:
        """Validate a claim while acquiring the current write transaction's lock."""
        generation_clause = (
            "" if claim_generation is None else " AND claim_generation = ?"
        )
        parameters: tuple[str | int, ...] = (job_id, claim_token)
        if claim_generation is not None:
            parameters += (claim_generation,)
        fenced = self.connection.execute(
            f"""
            UPDATE durable_jobs
            SET claim_generation = claim_generation
            WHERE id = ? AND status = 'RUNNING' AND claim_token = ?
                {generation_clause}
            """,
            parameters,
        )
        if fenced.rowcount != 1:
            raise RuntimeError("job claim is no longer current")
        return self.get(job_id)

    def request_cancel(self, job_id: str, *, now: datetime | None = None) -> Job:
        timestamp = format_utc(now or utc_now())
        updated = self.connection.execute(
            """
            UPDATE durable_jobs
            SET cancel_requested_at = COALESCE(cancel_requested_at, ?), updated_at = ?
            WHERE id = ? AND status IN ('QUEUED', 'RUNNING')
            """,
            (timestamp, timestamp, job_id),
        )
        if updated.rowcount != 1:
            raise RuntimeError("job cannot be cancelled")
        return self.get(job_id)

    def mark_cancelled(
        self, job_id: str, *, claim_token: str, now: datetime | None = None
    ) -> Job:
        timestamp = format_utc(now or utc_now())
        updated = self.connection.execute(
            """
            UPDATE durable_jobs
            SET status = 'CANCELLED', stage = 'CANCELLED',
                heartbeat_at = ?, updated_at = ?
            WHERE id = ? AND status = 'RUNNING' AND claim_token = ?
            """,
            (timestamp, timestamp, job_id, claim_token),
        )
        if updated.rowcount != 1:
            raise RuntimeError("job claim cannot transition to cancelled")
        return self.get(job_id)

    def mark_succeeded(
        self, job_id: str, *, claim_token: str, now: datetime | None = None
    ) -> Job:
        timestamp = format_utc(now or utc_now())
        updated = self.connection.execute(
            """
            UPDATE durable_jobs
            SET status = 'SUCCEEDED', stage = 'COMPLETED', error_json = NULL,
                heartbeat_at = ?, updated_at = ?
            WHERE id = ? AND status = 'RUNNING' AND claim_token = ?
            """,
            (timestamp, timestamp, job_id, claim_token),
        )
        if updated.rowcount != 1:
            raise RuntimeError("job claim cannot transition to succeeded")
        from suseoro.workflow.states import complete_analysis_for_succeeded_job

        complete_analysis_for_succeeded_job(self.connection, job_id=job_id)
        return self.get(job_id)

    def mark_failed(
        self,
        job_id: str,
        *,
        claim_token: str,
        error: dict[str, Any],
        now: datetime | None = None,
    ) -> Job:
        timestamp = format_utc(now or utc_now())
        updated = self.connection.execute(
            """
            UPDATE durable_jobs
            SET status = 'FAILED', stage = 'FAILED', error_json = ?,
                heartbeat_at = ?, updated_at = ?
            WHERE id = ? AND status = 'RUNNING' AND claim_token = ?
            """,
            (
                json.dumps(error, ensure_ascii=False, sort_keys=True),
                timestamp,
                timestamp,
                job_id,
                claim_token,
            ),
        )
        if updated.rowcount != 1:
            raise RuntimeError("job claim cannot transition to failed")
        return self.get(job_id)

    def retry(self, job_id: str, *, now: datetime | None = None) -> Job:
        timestamp = format_utc(now or utc_now())
        updated = self.connection.execute(
            """
            UPDATE durable_jobs
            SET status = 'QUEUED', stage = 'QUEUED', retry_count = retry_count + 1,
                cancel_requested_at = NULL, heartbeat_at = NULL,
                claim_token = NULL, error_json = NULL, updated_at = ?
            WHERE id = ? AND status IN ('FAILED', 'CANCELLED')
            """,
            (timestamp, job_id),
        )
        if updated.rowcount != 1:
            raise RuntimeError("only failed or cancelled jobs can be retried")
        return self.get(job_id)

    def recover_stale(
        self, *, stale_before: datetime, now: datetime | None = None
    ) -> list[str]:
        timestamp = format_utc(now or utc_now())
        cutoff = format_utc(stale_before)
        rows = self.connection.execute(
            """
            SELECT id FROM durable_jobs
            WHERE status = 'RUNNING'
              AND (heartbeat_at IS NULL OR heartbeat_at < ?)
            ORDER BY created_at, id
            """,
            (cutoff,),
        ).fetchall()
        recovered: list[str] = []
        error = json.dumps(
            {
                "type": "StaleJobRecovered",
                "message": "작업 프로세스 중단 뒤 재개 대기 중입니다.",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        for row in rows:
            updated = self.connection.execute(
                """
                UPDATE durable_jobs
                SET status = 'QUEUED', stage = 'RECOVERING',
                    retry_count = retry_count + 1, error_json = ?,
                    claim_token = NULL, heartbeat_at = NULL, updated_at = ?
                WHERE id = ? AND status = 'RUNNING'
                  AND (heartbeat_at IS NULL OR heartbeat_at < ?)
                """,
                (error, timestamp, row["id"], cutoff),
            )
            if updated.rowcount == 1:
                recovered.append(row["id"])
        return recovered

    def record_file_result(
        self,
        *,
        job_id: str,
        claim_token: str,
        claim_generation: int,
        source_document_id: str,
        status: str,
        total_rows: int,
        processed_rows: int,
        error: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> None:
        job = self.assert_claim(job_id, claim_token, claim_generation)
        document = self.connection.execute(
            "SELECT school_id FROM source_documents WHERE id = ?",
            (source_document_id,),
        ).fetchone()
        if (
            job.job_type != "COMPARE"
            or job.workspace_id is None
            or document is None
            or document["school_id"] != job.school_id
        ):
            raise ValueError("job file result scope does not match a COMPARE job")
        timestamp = format_utc(now or utc_now())
        self.connection.execute(
            """
            INSERT INTO job_file_results (
                id, job_id, source_document_id, status, total_rows,
                processed_rows, error_json, claim_token, claim_generation,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (job_id, source_document_id) DO UPDATE SET
                status = excluded.status, total_rows = excluded.total_rows,
                processed_rows = excluded.processed_rows,
                error_json = excluded.error_json,
                claim_token = excluded.claim_token,
                claim_generation = excluded.claim_generation,
                updated_at = excluded.updated_at
            """,
            (
                str(uuid.uuid4()),
                job_id,
                source_document_id,
                status,
                total_rows,
                processed_rows,
                json.dumps(error, ensure_ascii=False, sort_keys=True)
                if error
                else None,
                claim_token,
                claim_generation,
                timestamp,
                timestamp,
            ),
        )
