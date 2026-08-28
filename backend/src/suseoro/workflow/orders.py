"""Approval-scoped immutable order revisions and manual-send confirmation."""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

from suseoro.exports.orders import store_order_artifact
from suseoro.security.sessions import format_utc, utc_now
from suseoro.services.audit import record_audit_event
from suseoro.services.concurrency import update_with_version
from suseoro.workflow._common import (
    WorkflowDomainError,
    idempotent_mutation,
    require_role,
)


class OrderRuleError(WorkflowDomainError):
    pass


class OrderService:
    def __init__(self, connection: sqlite3.Connection, artifact_root: Path) -> None:
        self.connection = connection
        self.artifact_root = artifact_root

    def generate_revision(
        self,
        *,
        school_id: str,
        workspace_id: str,
        approval_revision_id: str,
        quote_id: str,
        actor_id: str,
        actor_roles: tuple[str, ...],
        workspace_version: int,
        reason: str,
        idempotency_key: str,
        request_id: str,
    ) -> dict[str, object]:
        body = {
            "workspace_id": workspace_id,
            "approval_revision_id": approval_revision_id,
            "quote_id": quote_id,
            "workspace_version": workspace_version,
            "reason": reason,
        }
        created_artifact_path: Path | None = None

        def mutate() -> dict[str, object]:
            nonlocal created_artifact_path
            require_role(
                self.connection,
                school_id=school_id,
                actor_id=actor_id,
                actor_roles=actor_roles,
                role="OPERATOR",
                error_type=OrderRuleError,
            )
            if not reason.strip():
                raise OrderRuleError("MODIFICATION_REASON_REQUIRED")
            workspace = self.connection.execute(
                "SELECT * FROM acquisition_workspaces WHERE id = ? AND school_id = ?",
                (workspace_id, school_id),
            ).fetchone()
            if workspace is None:
                raise OrderRuleError("WORKSPACE_NOT_FOUND")
            if workspace["status"] not in {
                "QUOTE_ADJUSTMENT",
                "ORDER_READY",
                "AWAITING_DELIVERY",
                "RECEIVING",
            }:
                raise OrderRuleError("ORDER_STATE_INVALID")
            current_approval = self.connection.execute(
                """
                SELECT ar.id
                FROM approval_revisions ar
                JOIN approval_decisions ad ON ad.approval_revision_id = ar.id
                WHERE ar.school_id = ? AND ar.workspace_id = ?
                  AND ad.decision = 'APPROVED'
                ORDER BY ar.revision_number DESC LIMIT 1
                """,
                (school_id, workspace_id),
            ).fetchone()
            if (
                current_approval is None
                or current_approval["id"] != approval_revision_id
            ):
                raise OrderRuleError("APPROVAL_REVISION_NOT_CURRENT")
            approval = self.connection.execute(
                """
                SELECT * FROM approval_revisions
                WHERE id = ? AND school_id = ? AND workspace_id = ?
                  AND sealed_at IS NOT NULL
                """,
                (approval_revision_id, school_id, workspace_id),
            ).fetchone()
            quote = self.connection.execute(
                """
                SELECT * FROM vendor_quotes
                WHERE id = ? AND school_id = ? AND workspace_id = ?
                  AND approval_revision_id = ? AND sealed_at IS NOT NULL
                """,
                (quote_id, school_id, workspace_id, approval_revision_id),
            ).fetchone()
            if approval is None or quote is None:
                raise OrderRuleError("QUOTE_OR_APPROVAL_NOT_FOUND")
            quote_rows = self.connection.execute(
                "SELECT * FROM vendor_quote_rows WHERE quote_id = ? ORDER BY id",
                (quote_id,),
            ).fetchall()
            if not quote_rows:
                raise OrderRuleError("EMPTY_ORDER")
            approved_rows = {
                row["id"]: row
                for row in self.connection.execute(
                    "SELECT * FROM approval_rows WHERE approval_revision_id = ?",
                    (approval_revision_id,),
                ).fetchall()
            }
            current_candidates = {
                row["id"]: row
                for row in self.connection.execute(
                    """
                    SELECT * FROM candidate_decisions
                    WHERE school_id = ? AND workspace_id = ?
                    """,
                    (school_id, workspace_id),
                ).fetchall()
            }
            export_rows: list[dict[str, object]] = []
            order_rows: list[tuple[sqlite3.Row, sqlite3.Row]] = []
            total = 0
            for row in quote_rows:
                approved = approved_rows.get(row["approval_row_id"])
                current = (
                    current_candidates.get(approved["candidate_id"])
                    if approved is not None
                    else None
                )
                if (
                    approved is None
                    or current is None
                    or current["outcome"] != "CANDIDATE"
                    or row["match_status"] != "MATCHED_ISBN"
                    or row["unit_price"] is None
                    or row["out_of_stock"]
                    or row["quantity"] > approved["quantity"]
                    or row["quantity"] > current["quantity"]
                    or row["unit_price"] > approved["unit_price"]
                    or row["unit_price"] > current["unit_price"]
                ):
                    raise OrderRuleError("APPROVAL_SCOPE_EXCEEDED")
                total += row["quantity"] * row["unit_price"]
                order_rows.append((row, approved))
                export_rows.append(
                    {
                        "isbn": row["isbn13"] or "",
                        "title": row["title"],
                        "author": row["author"],
                        "publisher": row["publisher"] or "",
                        "quantity": row["quantity"],
                        "unit_price": row["unit_price"],
                        "line_total_won": row["quantity"] * row["unit_price"],
                    }
                )
            if total > approval["budget_won"] or quote["requires_reapproval"]:
                raise OrderRuleError("APPROVAL_SCOPE_EXCEEDED")
            stored = store_order_artifact(
                self.artifact_root / school_id / workspace_id,
                export_rows,
            )
            created_artifact_path = stored.path
            try:
                now = format_utc(utc_now())
                artifact_id = str(uuid.uuid4())
                self.connection.execute(
                    """
                    INSERT INTO generated_artifacts (
                        id, school_id, workspace_id, artifact_type, storage_path,
                        sha256, size_bytes, content_bytes, created_by_user_id, created_at
                    ) VALUES (?, ?, ?, 'ORDER_XLSX', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        artifact_id,
                        school_id,
                        workspace_id,
                        str(stored.path),
                        stored.sha256,
                        stored.size_bytes,
                        stored.content,
                        actor_id,
                        now,
                    ),
                )
                revision_number = self.connection.execute(
                    """
                    SELECT COALESCE(MAX(revision_number), 0) + 1 AS n
                    FROM order_revisions WHERE workspace_id = ?
                    """,
                    (workspace_id,),
                ).fetchone()["n"]
                revision_id = str(uuid.uuid4())
                self.connection.execute(
                    """
                    INSERT INTO order_revisions (
                        id, school_id, workspace_id, approval_revision_id,
                        quote_id, artifact_id, revision_number, reason,
                        created_by_user_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        revision_id,
                        school_id,
                        workspace_id,
                        approval_revision_id,
                        quote_id,
                        artifact_id,
                        revision_number,
                        reason.strip(),
                        actor_id,
                        now,
                    ),
                )
                for row, approved in order_rows:
                    self.connection.execute(
                        """
                        INSERT INTO order_rows (
                            id, order_revision_id, approval_row_id, isbn13,
                            title, author, publisher, edition, quantity,
                            unit_price, line_total_won, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(uuid.uuid4()),
                            revision_id,
                            approved["id"],
                            row["isbn13"],
                            row["title"],
                            row["author"],
                            row["publisher"],
                            row["edition"],
                            row["quantity"],
                            row["unit_price"],
                            row["quantity"] * row["unit_price"],
                            now,
                        ),
                    )
                self.connection.execute(
                    "UPDATE order_revisions SET sealed_at = ? WHERE id = ?",
                    (now, revision_id),
                )
                target_state = (
                    "ORDER_READY"
                    if workspace["status"] in {"QUOTE_ADJUSTMENT", "ORDER_READY"}
                    else workspace["status"]
                )
                updated = update_with_version(
                    self.connection,
                    table="acquisition_workspaces",
                    school_id=school_id,
                    entity_id=workspace_id,
                    submitted_version=workspace_version,
                    changes={"status": target_state, "updated_at": now},
                )
                result = {
                    "revision_id": revision_id,
                    "revision_number": revision_number,
                    "artifact_id": artifact_id,
                    "path": str(stored.path),
                    "sha256": stored.sha256,
                    "size_bytes": stored.size_bytes,
                    "state": target_state,
                    "row_version": updated["row_version"],
                    "external_send_performed": False,
                }
                record_audit_event(
                    self.connection,
                    actor_id=actor_id,
                    school_id=school_id,
                    action="ORDER_REVISION_GENERATED",
                    entity_type="order_revision",
                    entity_id=revision_id,
                    before={
                        "state": workspace["status"],
                        "row_version": workspace["row_version"],
                    },
                    after={**result, "reason": reason.strip()},
                    request_id=request_id,
                )
                return result
            except BaseException:
                stored.path.unlink(missing_ok=True)
                raise

        try:
            result = idempotent_mutation(
                self.connection,
                school_id=school_id,
                actor_id=actor_id,
                route="orders.generate",
                key=idempotency_key,
                request_body=body,
                operation=mutate,
            )
        except BaseException:
            if created_artifact_path is not None:
                created_artifact_path.unlink(missing_ok=True)
            raise
        return {**result, "path": Path(result["path"])}

    def mark_sent(
        self,
        *,
        school_id: str,
        workspace_id: str,
        order_revision_id: str,
        actor_id: str,
        actor_roles: tuple[str, ...],
        workspace_version: int,
        reason: str,
        idempotency_key: str,
        request_id: str,
    ) -> dict[str, object]:
        body = {
            "workspace_id": workspace_id,
            "order_revision_id": order_revision_id,
            "workspace_version": workspace_version,
            "reason": reason,
        }

        def mutate() -> dict[str, object]:
            require_role(
                self.connection,
                school_id=school_id,
                actor_id=actor_id,
                actor_roles=actor_roles,
                role="OPERATOR",
                error_type=OrderRuleError,
            )
            if not reason.strip():
                raise OrderRuleError("MODIFICATION_REASON_REQUIRED")
            workspace = self.connection.execute(
                "SELECT * FROM acquisition_workspaces WHERE id = ? AND school_id = ?",
                (workspace_id, school_id),
            ).fetchone()
            order = self.connection.execute(
                """
                SELECT id FROM order_revisions
                WHERE id = ? AND school_id = ? AND workspace_id = ? AND sealed_at IS NOT NULL
                """,
                (order_revision_id, school_id, workspace_id),
            ).fetchone()
            if workspace is None or order is None:
                raise OrderRuleError("ORDER_NOT_FOUND")
            if workspace["status"] != "ORDER_READY":
                raise OrderRuleError("ORDER_SEND_STATE_INVALID")
            now = format_utc(utc_now())
            transmission_id = str(uuid.uuid4())
            self.connection.execute(
                """
                INSERT INTO order_transmissions (
                    id, order_revision_id, school_id, workspace_id,
                    actor_id, reason, sent_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    transmission_id,
                    order_revision_id,
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
                changes={"status": "AWAITING_DELIVERY", "updated_at": now},
            )
            result = {
                "transmission_id": transmission_id,
                "state": "AWAITING_DELIVERY",
                "row_version": updated["row_version"],
                "external_send_performed": False,
            }
            record_audit_event(
                self.connection,
                actor_id=actor_id,
                school_id=school_id,
                action="ORDER_MARKED_SENT",
                entity_type="order_revision",
                entity_id=order_revision_id,
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
            route="orders.mark-sent",
            key=idempotency_key,
            request_body=body,
            operation=mutate,
        )
