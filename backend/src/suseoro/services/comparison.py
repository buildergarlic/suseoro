"""Source-row-accounted comparison orchestration."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from suseoro.catalog.contracts import CatalogRecord, ComparisonSummary
from suseoro.catalog.normalization import normalize_book
from suseoro.catalog.repository import CatalogRepository
from suseoro.jobs.public_errors import comparison_file_failure
from suseoro.jobs.repository import JobRepository
from suseoro.matching.engine import MatchingEngine
from suseoro.security.sessions import format_utc, utc_now

_OUTCOMES = ("CANDIDATE", "NEEDS_REVIEW", "EXCLUDED", "ROW_ERROR")


@contextmanager
def _claim_batch_transaction(connection: sqlite3.Connection):
    owns_transaction = not connection.in_transaction
    savepoint = "comparison_claim_" + uuid.uuid4().hex
    if owns_transaction:
        connection.execute("BEGIN IMMEDIATE")
    else:
        connection.execute(f"SAVEPOINT {savepoint}")
    try:
        yield
    except BaseException:
        if owns_transaction:
            connection.rollback()
        else:
            connection.execute(f"ROLLBACK TO {savepoint}")
            connection.execute(f"RELEASE {savepoint}")
        raise
    else:
        if owns_transaction:
            connection.commit()
        else:
            connection.execute(f"RELEASE {savepoint}")


def _field_value(fields: dict[str, Any], name: str) -> Any:
    value = fields.get(name)
    if isinstance(value, dict) and "value" in value:
        return value["value"]
    return value


def _authors(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value if str(item).strip())
    return tuple(part.strip() for part in re.split(r"[;,]", str(value)) if part.strip())


def _record_from_row(row: sqlite3.Row) -> tuple[CatalogRecord, dict[str, Any]]:
    raw = json.loads(row["raw_json"])
    fields = json.loads(row["fields_json"])
    title = _field_value(fields, "title")
    if title is None or not str(title).strip():
        raise ValueError("recommendation title is required")
    return (
        CatalogRecord(
            source_item_id=row["id"],
            source_row_id=row["id"],
            isbn=_field_value(fields, "isbn"),
            title=str(title),
            subtitle=_field_value(fields, "subtitle"),
            authors=_authors(
                _field_value(fields, "authors")
                if "authors" in fields
                else _field_value(fields, "author")
            ),
            publisher=_field_value(fields, "publisher"),
            volume=_field_value(fields, "volume"),
            edition=_field_value(fields, "edition"),
            series=_field_value(fields, "series"),
            publication_date=_field_value(fields, "publication_date"),
            price=_field_value(fields, "price"),
            pages=_field_value(fields, "pages"),
            kdc=_field_value(fields, "kdc"),
            raw_fields=raw,
        ),
        raw,
    )


class ComparisonService:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def _insert_row_error(
        self,
        *,
        school_id: str,
        workspace_id: str,
        document_id: str,
        row: sqlite3.Row,
        reason: str,
        now: str,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO comparison_row_results (
                id, school_id, workspace_id, source_document_id,
                source_row_id, outcome, reason, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'ROW_ERROR', ?, ?, ?)
            ON CONFLICT (workspace_id, source_row_id) DO UPDATE SET
                outcome = 'ROW_ERROR', reason = excluded.reason,
                recommendation_id = NULL, evidence_holding_id = NULL,
                score = NULL, updated_at = excluded.updated_at
            """,
            (
                str(uuid.uuid4()),
                school_id,
                workspace_id,
                document_id,
                row["id"],
                reason,
                now,
                now,
            ),
        )

    def _compare_success_row(
        self,
        *,
        school_id: str,
        workspace_id: str,
        document_id: str,
        row: sqlite3.Row,
        engine: MatchingEngine,
        now: str,
    ) -> str:
        record, raw = _record_from_row(row)
        normalized = normalize_book(record)
        existing = self.connection.execute(
            """
            SELECT id FROM recommendations
            WHERE workspace_id = ? AND source_row_id = ?
            """,
            (workspace_id, row["id"]),
        ).fetchone()
        recommendation_id = existing["id"] if existing else str(uuid.uuid4())
        recommendation_values = (
            str(record.isbn) if record.isbn is not None else None,
            normalized.isbn13,
            record.title,
            record.subtitle,
            json.dumps(record.authors, ensure_ascii=False),
            record.publisher,
            record.volume,
            record.edition,
            record.series,
            json.dumps(raw, ensure_ascii=False, sort_keys=True),
            normalized.title_key,
            normalized.subtitle_key,
            normalized.author_key,
            normalized.publisher_key,
            normalized.volume_key,
            normalized.edition_key,
            normalized.series_key,
        )
        if existing:
            self.connection.execute(
                """
                UPDATE recommendations SET
                    isbn_input = ?, isbn13 = ?, original_title = ?,
                    original_subtitle = ?, original_authors_json = ?,
                    original_publisher = ?, original_volume = ?,
                    original_edition = ?, original_series = ?, original_json = ?,
                    title_key = ?, subtitle_key = ?, author_key = ?,
                    publisher_key = ?, volume_key = ?, edition_key = ?, series_key = ?
                WHERE id = ?
                """,
                (*recommendation_values, recommendation_id),
            )
        else:
            self.connection.execute(
                """
                INSERT INTO recommendations (
                    id, school_id, workspace_id, source_document_id, source_row_id,
                    isbn_input, isbn13, original_title, original_subtitle,
                    original_authors_json, original_publisher, original_volume,
                    original_edition, original_series, original_json, title_key,
                    subtitle_key, author_key, publisher_key, volume_key,
                    edition_key, series_key, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    recommendation_id,
                    school_id,
                    workspace_id,
                    document_id,
                    row["id"],
                    *recommendation_values,
                    now,
                ),
            )
        decision = engine.classify(record)
        self.connection.execute(
            """
            INSERT INTO candidate_decisions (
                id, school_id, workspace_id, recommendation_id, outcome,
                reason, evidence_holding_id, score, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (recommendation_id) DO UPDATE SET
                outcome = excluded.outcome, reason = excluded.reason,
                evidence_holding_id = excluded.evidence_holding_id,
                score = excluded.score, row_version = candidate_decisions.row_version + 1,
                updated_at = excluded.updated_at
            """,
            (
                str(uuid.uuid4()),
                school_id,
                workspace_id,
                recommendation_id,
                decision.outcome.value,
                decision.reason,
                decision.evidence_holding_id,
                decision.score,
                now,
                now,
            ),
        )
        self.connection.execute(
            """
            INSERT INTO comparison_row_results (
                id, school_id, workspace_id, source_document_id, source_row_id,
                recommendation_id, outcome, reason, evidence_holding_id,
                score, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (workspace_id, source_row_id) DO UPDATE SET
                recommendation_id = excluded.recommendation_id,
                outcome = excluded.outcome, reason = excluded.reason,
                evidence_holding_id = excluded.evidence_holding_id,
                score = excluded.score, updated_at = excluded.updated_at
            """,
            (
                str(uuid.uuid4()),
                school_id,
                workspace_id,
                document_id,
                row["id"],
                recommendation_id,
                decision.outcome.value,
                decision.reason,
                decision.evidence_holding_id,
                decision.score,
                now,
                now,
            ),
        )
        return decision.outcome.value

    def _upsert_file_result(
        self,
        *,
        school_id: str,
        workspace_id: str,
        document_id: str,
        status: str,
        counts: Counter,
        error: dict[str, Any] | None,
        now: str,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO comparison_file_results (
                id, school_id, workspace_id, source_document_id, status,
                total_rows, candidate_count, review_count, excluded_count,
                row_error_count, error_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (workspace_id, source_document_id) DO UPDATE SET
                status = excluded.status, total_rows = excluded.total_rows,
                candidate_count = excluded.candidate_count,
                review_count = excluded.review_count,
                excluded_count = excluded.excluded_count,
                row_error_count = excluded.row_error_count,
                error_json = excluded.error_json, updated_at = excluded.updated_at
            """,
            (
                str(uuid.uuid4()),
                school_id,
                workspace_id,
                document_id,
                status,
                sum(counts.values()),
                counts["CANDIDATE"],
                counts["NEEDS_REVIEW"],
                counts["EXCLUDED"],
                counts["ROW_ERROR"],
                json.dumps(error, ensure_ascii=False, sort_keys=True)
                if error
                else None,
                now,
                now,
            ),
        )

    def compare_documents(
        self,
        *,
        school_id: str,
        workspace_id: str,
        source_document_ids,
        job_id: str | None = None,
        claim_token: str | None = None,
        claim_generation: int | None = None,
        source_row_ids=None,
        now: datetime | None = None,
    ) -> ComparisonSummary:
        arguments = {
            "school_id": school_id,
            "workspace_id": workspace_id,
            "source_document_ids": source_document_ids,
            "job_id": job_id,
            "claim_token": claim_token,
            "claim_generation": claim_generation,
            "source_row_ids": source_row_ids,
            "now": now,
        }
        if job_id is None:
            return self._compare_documents_batch(**arguments)
        with _claim_batch_transaction(self.connection):
            return self._compare_documents_batch(**arguments)

    def _compare_documents_batch(
        self,
        *,
        school_id: str,
        workspace_id: str,
        source_document_ids,
        job_id: str | None = None,
        claim_token: str | None = None,
        claim_generation: int | None = None,
        source_row_ids=None,
        now: datetime | None = None,
    ) -> ComparisonSummary:
        workspace = self.connection.execute(
            "SELECT school_id FROM acquisition_workspaces WHERE id = ?",
            (workspace_id,),
        ).fetchone()
        if workspace is None or workspace["school_id"] != school_id:
            raise ValueError("workspace does not belong to the requested school")
        job_repository = JobRepository(self.connection) if job_id else None
        if job_repository is not None:
            job = job_repository.get(job_id)
            if (
                job is None
                or job.school_id != school_id
                or job.workspace_id != workspace_id
                or job.job_type != "COMPARE"
            ):
                raise ValueError(
                    "job must be a COMPARE job for the requested school and workspace"
                )
            if claim_token is None:
                raise ValueError("a COMPARE job claim token is required")
            job_repository.fence_claim(job_id, claim_token, claim_generation)
            current_claim = job_repository.assert_claim(
                job_id, claim_token, claim_generation
            )
            claim_generation = current_claim.claim_generation
            raw_snapshot = job.payload.get("source_snapshot")
            if job.payload.get("source_snapshot_version") != 1 or not isinstance(
                raw_snapshot, list
            ):
                raise ValueError("COMPARE job requires a versioned source snapshot")
            snapshot_ids = {
                str(item.get("id"))
                for item in raw_snapshot
                if isinstance(item, dict) and item.get("id") is not None
            }
            if not set(source_document_ids).issubset(snapshot_ids):
                raise ValueError("source document is outside the COMPARE job snapshot")
            catalog_version_id = job.payload.get("catalog_version_id")
            if catalog_version_id is not None:
                catalog_snapshot = self.connection.execute(
                    """
                    SELECT 1 FROM catalog_versions
                    WHERE id = ? AND school_id = ? AND status = 'ACTIVE'
                    """,
                    (catalog_version_id, school_id),
                ).fetchone()
                if catalog_snapshot is None:
                    raise ValueError("catalog is outside the COMPARE job snapshot")
        requested_row_ids = (
            None if source_row_ids is None else frozenset(source_row_ids)
        )
        timestamp = format_utc(now or utc_now())
        engine = MatchingEngine(CatalogRepository(self.connection), school_id)
        if not self.connection.in_transaction:
            self.connection.execute("BEGIN")
        totals: Counter = Counter({outcome: 0 for outcome in _OUTCOMES})
        file_statuses: dict[str, str] = {}
        for document_id in tuple(source_document_ids):
            document = self.connection.execute(
                """
                SELECT id, status FROM source_documents
                WHERE id = ? AND school_id = ?
                """,
                (document_id, school_id),
            ).fetchone()
            if document is None:
                raise ValueError(
                    "source document does not belong to the requested school"
                )
            all_rows = self.connection.execute(
                """
                SELECT * FROM source_rows
                WHERE source_document_id = ?
                ORDER BY source_row, id
                """,
                (document_id,),
            ).fetchall()
            rows = [
                row
                for row in all_rows
                if requested_row_ids is None or row["id"] in requested_row_ids
            ]
            counts: Counter = Counter({outcome: 0 for outcome in _OUTCOMES})
            file_error: dict[str, Any] | None = None
            for row in rows:
                existing_result = self.connection.execute(
                    """
                    SELECT outcome FROM comparison_row_results
                    WHERE workspace_id = ? AND source_row_id = ?
                    """,
                    (workspace_id, row["id"]),
                ).fetchone()
                # Completed decisions are idempotent.  ROW_ERROR remains
                # retryable because a parser/file retry can repair that row.
                if (
                    existing_result is not None
                    and existing_result["outcome"] != "ROW_ERROR"
                ):
                    counts[existing_result["outcome"]] += 1
                    continue
                document_failed = document["status"] in ("PENDING", "FAILED")
                if document_failed or row["status"] == "ROW_ERROR":
                    reason = row["error_code"] or (
                        "SOURCE_DOCUMENT_NOT_READY"
                        if document["status"] == "PENDING"
                        else "SOURCE_DOCUMENT_FAILED"
                        if document_failed
                        else "SOURCE_ROW_ERROR"
                    )
                    self._insert_row_error(
                        school_id=school_id,
                        workspace_id=workspace_id,
                        document_id=document_id,
                        row=row,
                        reason=reason,
                        now=timestamp,
                    )
                    counts["ROW_ERROR"] += 1
                    continue
                savepoint = "comparison_row_" + uuid.uuid4().hex
                self.connection.execute(f"SAVEPOINT {savepoint}")
                try:
                    outcome = self._compare_success_row(
                        school_id=school_id,
                        workspace_id=workspace_id,
                        document_id=document_id,
                        row=row,
                        engine=engine,
                        now=timestamp,
                    )
                    self.connection.execute(f"RELEASE {savepoint}")
                # Per-row containment is required so every logical row is accounted.
                except Exception:  # noqa: BLE001
                    self.connection.execute(f"ROLLBACK TO {savepoint}")
                    self.connection.execute(f"RELEASE {savepoint}")
                    outcome = "ROW_ERROR"
                    file_error = comparison_file_failure()
                    self._insert_row_error(
                        school_id=school_id,
                        workspace_id=workspace_id,
                        document_id=document_id,
                        row=row,
                        reason="COMPARISON_ROW_ERROR",
                        now=timestamp,
                    )
                counts[outcome] += 1
            persisted_rows = self.connection.execute(
                """
                SELECT outcome FROM comparison_row_results
                WHERE workspace_id = ? AND source_document_id = ?
                """,
                (workspace_id, document_id),
            ).fetchall()
            complete = len(persisted_rows) == len(all_rows)
            if complete:
                complete_counts: Counter = Counter(
                    {outcome: 0 for outcome in _OUTCOMES}
                )
                complete_counts.update(row["outcome"] for row in persisted_rows)
                successful = sum(
                    complete_counts[outcome]
                    for outcome in ("CANDIDATE", "NEEDS_REVIEW", "EXCLUDED")
                )
                if not all_rows:
                    status = "FAILED"
                    file_error = {"code": "NO_LOGICAL_ROWS"}
                elif document["status"] in ("PENDING", "FAILED"):
                    status = "FAILED"
                elif complete_counts["ROW_ERROR"] and successful == 0:
                    status = "FAILED"
                    file_error = {"code": "ALL_LOGICAL_ROWS_FAILED"}
                elif complete_counts["ROW_ERROR"]:
                    status = "PARTIAL"
                else:
                    status = "SUCCESS"
                if job_repository is not None:
                    job_repository.assert_claim(job_id, claim_token, claim_generation)
                self._upsert_file_result(
                    school_id=school_id,
                    workspace_id=workspace_id,
                    document_id=document_id,
                    status=status,
                    counts=complete_counts,
                    error=file_error,
                    now=timestamp,
                )
                if job_repository is not None:
                    job_repository.record_file_result(
                        job_id=job_id,
                        claim_token=claim_token,
                        claim_generation=claim_generation,
                        source_document_id=document_id,
                        status=status,
                        total_rows=len(all_rows),
                        processed_rows=len(all_rows),
                        error=file_error,
                        now=now,
                    )
            else:
                status = "PENDING"
            totals.update(counts)
            file_statuses[document_id] = status
        return ComparisonSummary(
            total_rows=sum(totals.values()),
            counts={outcome: totals[outcome] for outcome in _OUTCOMES},
            file_statuses=file_statuses,
        )
