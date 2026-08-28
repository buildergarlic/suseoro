"""Immutable approval snapshots, review decisions, and reapproval rules."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from typing import Any

from suseoro.api.dependencies import may_approve_own_change
from suseoro.security.sessions import format_utc, utc_now
from suseoro.services.audit import record_audit_event
from suseoro.services.concurrency import update_with_version
from suseoro.workflow._common import (
    WorkflowDomainError,
    idempotent_mutation,
    require_role,
)


class ApprovalError(WorkflowDomainError):
    pass


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _author(value: str) -> str:
    try:
        authors = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return value or ""
    if isinstance(authors, list):
        return ", ".join(str(item) for item in authors)
    return str(authors)


class ApprovalService:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def _require_operator(
        self, school_id: str, actor_id: str, roles: tuple[str, ...]
    ) -> None:
        require_role(
            self.connection,
            school_id=school_id,
            actor_id=actor_id,
            actor_roles=roles,
            role="OPERATOR",
            error_type=ApprovalError,
        )

    def _require_reviewer(
        self, school_id: str, actor_id: str, roles: tuple[str, ...]
    ) -> None:
        require_role(
            self.connection,
            school_id=school_id,
            actor_id=actor_id,
            actor_roles=roles,
            role="REVIEWER",
            error_type=ApprovalError,
        )

    def _workspace(self, school_id: str, workspace_id: str):
        row = self.connection.execute(
            "SELECT * FROM acquisition_workspaces WHERE id = ? AND school_id = ?",
            (workspace_id, school_id),
        ).fetchone()
        if row is None:
            raise ApprovalError("WORKSPACE_NOT_FOUND")
        return row

    def _candidate_rows(
        self, school_id: str, workspace_id: str
    ) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT cd.id AS candidate_id, cd.recommendation_id, cd.quantity,
                   cd.unit_price, r.isbn13, r.original_title,
                   r.original_authors_json, r.original_edition
            FROM candidate_decisions cd
            JOIN recommendations r ON r.id = cd.recommendation_id
            WHERE cd.school_id = ? AND cd.workspace_id = ?
              AND cd.outcome = 'CANDIDATE' AND cd.quantity > 0
            ORDER BY cd.id
            """,
            (school_id, workspace_id),
        ).fetchall()
        return [
            {
                "candidate_id": row["candidate_id"],
                "recommendation_id": row["recommendation_id"],
                "isbn13": row["isbn13"],
                "title": row["original_title"],
                "author": _author(row["original_authors_json"]),
                "edition": row["original_edition"],
                "quantity": row["quantity"],
                "unit_price": row["unit_price"],
            }
            for row in rows
        ]

    def _create_revision(
        self,
        *,
        school_id: str,
        workspace_id: str,
        actor_id: str,
        budget_won: int,
        reason: str,
    ) -> dict[str, object]:
        if (
            isinstance(budget_won, bool)
            or not isinstance(budget_won, int)
            or budget_won < 0
        ):
            raise ApprovalError("INVALID_BUDGET")
        rows = self._candidate_rows(school_id, workspace_id)
        if not rows:
            raise ApprovalError("NO_APPROVAL_CANDIDATES")
        expected_total = sum(row["quantity"] * row["unit_price"] for row in rows)
        canonical_rows = [
            {
                "author": row["author"],
                "candidate_id": row["candidate_id"],
                "isbn13": row["isbn13"],
                "quantity": row["quantity"],
                "title": row["title"],
                "unit_price": row["unit_price"],
            }
            for row in rows
        ]
        canonical_json = _canonical(
            {
                "budget_won": budget_won,
                "candidates": canonical_rows,
                "expected_total_won": expected_total,
            }
        )
        sha256 = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
        revision_number = self.connection.execute(
            """
            SELECT COALESCE(MAX(revision_number), 0) + 1 AS n
            FROM approval_revisions WHERE workspace_id = ?
            """,
            (workspace_id,),
        ).fetchone()["n"]
        revision_id = str(uuid.uuid4())
        now = format_utc(utc_now())
        self.connection.execute(
            """
            INSERT INTO approval_revisions (
                id, school_id, workspace_id, revision_number, canonical_json,
                sha256, budget_won, expected_total_won, created_by_user_id,
                reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                revision_id,
                school_id,
                workspace_id,
                revision_number,
                canonical_json,
                sha256,
                budget_won,
                expected_total,
                actor_id,
                reason,
                now,
            ),
        )
        for row in rows:
            self.connection.execute(
                """
                INSERT INTO approval_rows (
                    id, approval_revision_id, school_id, workspace_id,
                    candidate_id, recommendation_id, isbn13, title, author,
                    edition, quantity, unit_price, line_total_won, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    revision_id,
                    school_id,
                    workspace_id,
                    row["candidate_id"],
                    row["recommendation_id"],
                    row["isbn13"],
                    row["title"],
                    row["author"],
                    row["edition"],
                    row["quantity"],
                    row["unit_price"],
                    row["quantity"] * row["unit_price"],
                    now,
                ),
            )
        self.connection.execute(
            "UPDATE approval_revisions SET sealed_at = ? WHERE id = ?",
            (now, revision_id),
        )
        return {
            "revision_id": revision_id,
            "revision_number": revision_number,
            "sha256": sha256,
            "expected_total_won": expected_total,
            "budget_won": budget_won,
        }

    def request_approval(
        self,
        *,
        school_id: str,
        workspace_id: str,
        actor_id: str,
        actor_roles: tuple[str, ...],
        workspace_version: int,
        budget_won: int,
        reason: str,
        idempotency_key: str,
        request_id: str,
    ) -> dict[str, object]:
        body = {
            "workspace_id": workspace_id,
            "workspace_version": workspace_version,
            "budget_won": budget_won,
            "reason": reason,
        }

        def mutate() -> dict[str, object]:
            self._require_operator(school_id, actor_id, actor_roles)
            if not reason.strip():
                raise ApprovalError("MODIFICATION_REASON_REQUIRED")
            workspace = self._workspace(school_id, workspace_id)
            if workspace["status"] not in {"CANDIDATE_REVIEW", "CHANGES_REQUESTED"}:
                raise ApprovalError("APPROVAL_REQUEST_STATE_INVALID")
            unresolved = self.connection.execute(
                """
                SELECT COUNT(*) AS n FROM candidate_decisions
                WHERE school_id = ? AND workspace_id = ? AND outcome = 'NEEDS_REVIEW'
                """,
                (school_id, workspace_id),
            ).fetchone()["n"]
            if unresolved:
                raise ApprovalError("UNRESOLVED_CANDIDATES")
            revision = self._create_revision(
                school_id=school_id,
                workspace_id=workspace_id,
                actor_id=actor_id,
                budget_won=budget_won,
                reason=reason.strip(),
            )
            updated = update_with_version(
                self.connection,
                table="acquisition_workspaces",
                school_id=school_id,
                entity_id=workspace_id,
                submitted_version=workspace_version,
                changes={
                    "status": "APPROVAL_PENDING",
                    "updated_at": format_utc(utc_now()),
                },
            )
            result = {
                **revision,
                "state": updated["status"],
                "row_version": updated["row_version"],
            }
            record_audit_event(
                self.connection,
                actor_id=actor_id,
                school_id=school_id,
                action="APPROVAL_REQUESTED",
                entity_type="approval_revision",
                entity_id=str(revision["revision_id"]),
                before={
                    "state": workspace["status"],
                    "row_version": workspace["row_version"],
                },
                after={**result, "reason": reason.strip()},
                request_id=request_id,
            )
            return result

        return idempotent_mutation(
            self.connection,
            school_id=school_id,
            actor_id=actor_id,
            route="approvals.request",
            key=idempotency_key,
            request_body=body,
            operation=mutate,
        )

    def view_revision(
        self,
        *,
        school_id: str,
        revision_id: str,
        actor_id: str,
        actor_roles: tuple[str, ...],
    ) -> dict[str, object]:
        self._require_reviewer(school_id, actor_id, actor_roles)
        row = self.connection.execute(
            """
            SELECT * FROM approval_revisions
            WHERE id = ? AND school_id = ? AND sealed_at IS NOT NULL
            """,
            (revision_id, school_id),
        ).fetchone()
        if row is None:
            raise ApprovalError("APPROVAL_REVISION_NOT_FOUND")
        return {
            "revision_id": row["id"],
            "revision_number": row["revision_number"],
            "sha256": row["sha256"],
            "payload": json.loads(row["canonical_json"]),
        }

    def comment(
        self,
        *,
        school_id: str,
        workspace_id: str,
        revision_id: str,
        actor_id: str,
        actor_roles: tuple[str, ...],
        workspace_version: int,
        content: str,
        reason: str,
        idempotency_key: str,
        request_id: str,
    ) -> dict[str, object]:
        body = {
            "workspace_id": workspace_id,
            "revision_id": revision_id,
            "workspace_version": workspace_version,
            "content": content,
            "reason": reason,
        }

        def mutate() -> dict[str, object]:
            self._require_reviewer(school_id, actor_id, actor_roles)
            if not content.strip():
                raise ApprovalError("COMMENT_REQUIRED")
            if not reason.strip():
                raise ApprovalError("MODIFICATION_REASON_REQUIRED")
            workspace = self._workspace(school_id, workspace_id)
            if workspace["status"] != "APPROVAL_PENDING":
                raise ApprovalError("APPROVAL_COMMENT_STATE_INVALID")
            revision = self.connection.execute(
                """
                SELECT id FROM approval_revisions
                WHERE id = ? AND school_id = ? AND workspace_id = ?
                  AND sealed_at IS NOT NULL
                  AND revision_number = (
                      SELECT MAX(revision_number) FROM approval_revisions
                      WHERE workspace_id = ?
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM approval_decisions
                      WHERE approval_revision_id = approval_revisions.id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM approval_cancellations
                      WHERE approval_revision_id = approval_revisions.id
                  )
                """,
                (revision_id, school_id, workspace_id, workspace_id),
            ).fetchone()
            if revision is None:
                raise ApprovalError("APPROVAL_REVISION_NOT_CURRENT")
            comment_id = str(uuid.uuid4())
            now = format_utc(utc_now())
            self.connection.execute(
                """
                INSERT INTO approval_comments (
                    id, approval_revision_id, school_id, actor_id, content, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (comment_id, revision_id, school_id, actor_id, content.strip(), now),
            )
            updated = update_with_version(
                self.connection,
                table="acquisition_workspaces",
                school_id=school_id,
                entity_id=workspace_id,
                submitted_version=workspace_version,
                changes={"status": "APPROVAL_PENDING", "updated_at": now},
            )
            result = {
                "comment_id": comment_id,
                "content": content.strip(),
                "created_at": now,
                "state": updated["status"],
                "row_version": updated["row_version"],
            }
            record_audit_event(
                self.connection,
                actor_id=actor_id,
                school_id=school_id,
                action="APPROVAL_COMMENTED",
                entity_type="approval_revision",
                entity_id=revision_id,
                before={
                    "state": workspace["status"],
                    "row_version": workspace["row_version"],
                },
                after={**result, "reason": reason.strip()},
                request_id=request_id,
            )
            return result

        return idempotent_mutation(
            self.connection,
            school_id=school_id,
            actor_id=actor_id,
            route="approvals.comment",
            key=idempotency_key,
            request_body=body,
            operation=mutate,
        )

    def cancel_request(
        self,
        *,
        school_id: str,
        workspace_id: str,
        revision_id: str,
        actor_id: str,
        actor_roles: tuple[str, ...],
        workspace_version: int,
        reason: str,
        idempotency_key: str,
        request_id: str,
    ) -> dict[str, object]:
        body = {
            "workspace_id": workspace_id,
            "revision_id": revision_id,
            "workspace_version": workspace_version,
            "reason": reason,
        }

        def mutate() -> dict[str, object]:
            self._require_operator(school_id, actor_id, actor_roles)
            if not reason.strip():
                raise ApprovalError("MODIFICATION_REASON_REQUIRED")
            workspace = self._workspace(school_id, workspace_id)
            if workspace["status"] != "APPROVAL_PENDING":
                raise ApprovalError("APPROVAL_CANCEL_STATE_INVALID")
            revision = self.connection.execute(
                """
                SELECT ar.id FROM approval_revisions ar
                WHERE ar.id = ? AND ar.school_id = ? AND ar.workspace_id = ?
                  AND ar.revision_number = (
                      SELECT MAX(revision_number) FROM approval_revisions
                      WHERE workspace_id = ?
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM approval_decisions ad
                      WHERE ad.approval_revision_id = ar.id
                  )
                """,
                (revision_id, school_id, workspace_id, workspace_id),
            ).fetchone()
            if revision is None:
                raise ApprovalError("APPROVAL_REVISION_NOT_CURRENT")
            cancellation_id = str(uuid.uuid4())
            now = format_utc(utc_now())
            self.connection.execute(
                """
                INSERT INTO approval_cancellations (
                    id, approval_revision_id, school_id, workspace_id,
                    actor_id, reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cancellation_id,
                    revision_id,
                    school_id,
                    workspace_id,
                    actor_id,
                    reason.strip(),
                    now,
                ),
            )
            updated = update_with_version(
                self.connection,
                table="acquisition_workspaces",
                school_id=school_id,
                entity_id=workspace_id,
                submitted_version=workspace_version,
                changes={"status": "CANDIDATE_REVIEW", "updated_at": now},
            )
            result = {
                "cancellation_id": cancellation_id,
                "revision_id": revision_id,
                "state": updated["status"],
                "row_version": updated["row_version"],
            }
            record_audit_event(
                self.connection,
                actor_id=actor_id,
                school_id=school_id,
                action="APPROVAL_CANCELLED",
                entity_type="approval_revision",
                entity_id=revision_id,
                before={
                    "state": workspace["status"],
                    "row_version": workspace["row_version"],
                },
                after={**result, "reason": reason.strip()},
                request_id=request_id,
            )
            return result

        return idempotent_mutation(
            self.connection,
            school_id=school_id,
            actor_id=actor_id,
            route="approvals.cancel",
            key=idempotency_key,
            request_body=body,
            operation=mutate,
        )

    def _decide(
        self,
        *,
        decision: str,
        school_id: str,
        workspace_id: str,
        revision_id: str,
        actor_id: str,
        actor_roles: tuple[str, ...],
        workspace_version: int,
        reason: str,
        idempotency_key: str,
        request_id: str,
    ) -> dict[str, object]:
        body = {
            "decision": decision,
            "workspace_id": workspace_id,
            "revision_id": revision_id,
            "workspace_version": workspace_version,
            "reason": reason,
        }

        def mutate() -> dict[str, object]:
            self._require_reviewer(school_id, actor_id, actor_roles)
            if not reason.strip():
                raise ApprovalError("MODIFICATION_REASON_REQUIRED")
            workspace = self._workspace(school_id, workspace_id)
            revision = self.connection.execute(
                """
                SELECT ar.*, s.single_operator_mode
                FROM approval_revisions ar JOIN schools s ON s.id = ar.school_id
                WHERE ar.id = ? AND ar.school_id = ? AND ar.workspace_id = ?
                  AND ar.sealed_at IS NOT NULL
                """,
                (revision_id, school_id, workspace_id),
            ).fetchone()
            if revision is None:
                raise ApprovalError("APPROVAL_REVISION_NOT_FOUND")
            existing = self.connection.execute(
                "SELECT decision FROM approval_decisions WHERE approval_revision_id = ?",
                (revision_id,),
            ).fetchone()
            if existing is not None:
                raise ApprovalError("APPROVAL_ALREADY_DECIDED")
            if workspace["status"] != "APPROVAL_PENDING":
                raise ApprovalError("APPROVAL_DECISION_STATE_INVALID")
            if decision == "APPROVED" and not may_approve_own_change(
                actor_id,
                revision["created_by_user_id"],
                bool(revision["single_operator_mode"]),
            ):
                raise ApprovalError("SELF_APPROVAL_FORBIDDEN")
            now = format_utc(utc_now())
            self.connection.execute(
                """
                INSERT INTO approval_decisions (
                    id, approval_revision_id, school_id, workspace_id, actor_id,
                    decision, reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    revision_id,
                    school_id,
                    workspace_id,
                    actor_id,
                    decision,
                    reason.strip(),
                    now,
                ),
            )
            state = "APPROVED" if decision == "APPROVED" else "CHANGES_REQUESTED"
            updated = update_with_version(
                self.connection,
                table="acquisition_workspaces",
                school_id=school_id,
                entity_id=workspace_id,
                submitted_version=workspace_version,
                changes={"status": state, "updated_at": now},
            )
            result = {
                "revision_id": revision_id,
                "decision": decision,
                "state": state,
                "row_version": updated["row_version"],
            }
            record_audit_event(
                self.connection,
                actor_id=actor_id,
                school_id=school_id,
                action=f"APPROVAL_{decision}",
                entity_type="approval_revision",
                entity_id=revision_id,
                before={
                    "state": workspace["status"],
                    "row_version": workspace["row_version"],
                },
                after={**result, "reason": reason.strip()},
                request_id=request_id,
            )
            return result

        return idempotent_mutation(
            self.connection,
            school_id=school_id,
            actor_id=actor_id,
            route=f"approvals.{decision.casefold()}",
            key=idempotency_key,
            request_body=body,
            operation=mutate,
        )

    def approve(self, **kwargs) -> dict[str, object]:
        return self._decide(decision="APPROVED", **kwargs)

    def reject(self, **kwargs) -> dict[str, object]:
        return self._decide(decision="REJECTED", **kwargs)

    def adjust_approved_candidate(
        self,
        *,
        school_id: str,
        workspace_id: str,
        candidate_id: str,
        actor_id: str,
        actor_roles: tuple[str, ...],
        candidate_version: int,
        workspace_version: int,
        changes: dict[str, Any],
        budget_won: int,
        reason: str,
        idempotency_key: str,
        request_id: str,
    ) -> dict[str, object]:
        body = {
            "workspace_id": workspace_id,
            "candidate_id": candidate_id,
            "candidate_version": candidate_version,
            "workspace_version": workspace_version,
            "changes": changes,
            "budget_won": budget_won,
            "reason": reason,
        }

        def mutate() -> dict[str, object]:
            self._require_operator(school_id, actor_id, actor_roles)
            if not reason.strip():
                raise ApprovalError("MODIFICATION_REASON_REQUIRED")
            workspace = self._workspace(school_id, workspace_id)
            if workspace["status"] not in {
                "APPROVED",
                "QUOTE_REVIEW",
                "ORDER_READY",
                "ORDER_SENT",
                "RECEIVING",
            }:
                raise ApprovalError("APPROVED_ADJUSTMENT_STATE_INVALID")
            latest = self.connection.execute(
                """
                SELECT ar.* FROM approval_revisions ar
                JOIN approval_decisions ad ON ad.approval_revision_id = ar.id
                WHERE ar.school_id = ? AND ar.workspace_id = ?
                  AND ad.decision = 'APPROVED'
                ORDER BY ar.revision_number DESC LIMIT 1
                """,
                (school_id, workspace_id),
            ).fetchone()
            if latest is None:
                raise ApprovalError("APPROVED_REVISION_NOT_FOUND")
            current = self.connection.execute(
                """
                SELECT * FROM candidate_decisions
                WHERE id = ? AND school_id = ? AND workspace_id = ?
                """,
                (candidate_id, school_id, workspace_id),
            ).fetchone()
            if current is None:
                raise ApprovalError("CANDIDATE_NOT_FOUND")
            allowed = {"outcome", "quantity", "unit_price"}
            if not changes or set(changes) - allowed:
                raise ApprovalError("INVALID_CANDIDATE_CHANGE")
            if "outcome" in changes and changes["outcome"] not in {
                "CANDIDATE",
                "NEEDS_REVIEW",
                "EXCLUDED",
            }:
                raise ApprovalError("INVALID_CANDIDATE_OUTCOME")
            for field in ("quantity", "unit_price"):
                if field in changes:
                    value = changes[field]
                    if (
                        isinstance(value, bool)
                        or not isinstance(value, int)
                        or value < 0
                    ):
                        raise ApprovalError(f"INVALID_{field.upper()}")
            if (
                isinstance(budget_won, bool)
                or not isinstance(budget_won, int)
                or budget_won < 0
            ):
                raise ApprovalError("INVALID_BUDGET")
            updated_candidate = update_with_version(
                self.connection,
                table="candidate_decisions",
                school_id=school_id,
                entity_id=candidate_id,
                submitted_version=candidate_version,
                changes={
                    **changes,
                    "reason": reason.strip(),
                    "modified_by_user_id": actor_id,
                    "updated_at": format_utc(utc_now()),
                },
            )
            approved_row = self.connection.execute(
                """
                SELECT * FROM approval_rows
                WHERE approval_revision_id = ? AND candidate_id = ?
                """,
                (latest["id"], candidate_id),
            ).fetchone()
            included = (
                updated_candidate["outcome"] == "CANDIDATE"
                and updated_candidate["quantity"] > 0
            )
            reapproval = budget_won != latest["budget_won"]
            if included:
                reapproval = reapproval or approved_row is None
                if approved_row is not None:
                    reapproval = (
                        reapproval
                        or updated_candidate["quantity"] > approved_row["quantity"]
                    )
                    reapproval = (
                        reapproval
                        or updated_candidate["unit_price"] > approved_row["unit_price"]
                    )
            current_total = self.connection.execute(
                """
                SELECT COALESCE(SUM(quantity * unit_price), 0) AS total
                FROM candidate_decisions
                WHERE school_id = ? AND workspace_id = ?
                  AND outcome = 'CANDIDATE' AND quantity > 0
                """,
                (school_id, workspace_id),
            ).fetchone()["total"]
            reapproval = reapproval or current_total > budget_won
            now = format_utc(utc_now())
            revision: dict[str, object] | None = None
            state = workspace["status"]
            if reapproval:
                revision = self._create_revision(
                    school_id=school_id,
                    workspace_id=workspace_id,
                    actor_id=actor_id,
                    budget_won=budget_won,
                    reason=reason.strip(),
                )
                state = "APPROVAL_PENDING"
            updated_workspace = update_with_version(
                self.connection,
                table="acquisition_workspaces",
                school_id=school_id,
                entity_id=workspace_id,
                submitted_version=workspace_version,
                changes={"status": state, "updated_at": now},
            )
            result = {
                "candidate_id": candidate_id,
                "candidate_row_version": updated_candidate["row_version"],
                "reapproval_required": reapproval,
                "state": state,
                "row_version": updated_workspace["row_version"],
                "revision_id": revision["revision_id"] if revision else None,
            }
            record_audit_event(
                self.connection,
                actor_id=actor_id,
                school_id=school_id,
                action="APPROVED_SCOPE_ADJUSTED",
                entity_type="candidate_decision",
                entity_id=candidate_id,
                before={
                    "outcome": current["outcome"],
                    "quantity": current["quantity"],
                    "unit_price": current["unit_price"],
                    "row_version": current["row_version"],
                },
                after={**result, "changes": changes, "reason": reason.strip()},
                request_id=request_id,
            )
            return result

        return idempotent_mutation(
            self.connection,
            school_id=school_id,
            actor_id=actor_id,
            route="approvals.adjust-scope",
            key=idempotency_key,
            request_body=body,
            operation=mutate,
        )
