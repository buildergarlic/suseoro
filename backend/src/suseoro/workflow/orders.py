"""Approval-scoped immutable order revisions and manual-send confirmation."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from suseoro.exports.orders import DEFAULT_ORDER_COLUMNS, store_order_artifact
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


_ORDER_FIELDS = {field for field, _ in DEFAULT_ORDER_COLUMNS}
_ORDER_TEMPLATE_STATES = {"QUOTE_REVIEW", "ORDER_READY", "ORDER_SENT", "RECEIVING"}


class OrderTemplateService:
    """Remember immutable, school/vendor-scoped order workbook mappings."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def save(
        self,
        *,
        school_id: str,
        workspace_id: str,
        vendor_name: str,
        columns: Sequence[tuple[str, str]],
        actor_id: str,
        actor_roles: tuple[str, ...],
        workspace_version: int,
        reason: str,
        idempotency_key: str,
        request_id: str,
    ) -> dict[str, object]:
        normalized = tuple((str(field), str(header)) for field, header in columns)
        body = {
            "workspace_id": workspace_id,
            "vendor_name": vendor_name,
            "columns": normalized,
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
            if not vendor_name.strip():
                raise OrderRuleError("VENDOR_NAME_REQUIRED")
            if not reason.strip():
                raise OrderRuleError("MODIFICATION_REASON_REQUIRED")
            fields = [field for field, _ in normalized]
            if (
                not normalized
                or len(fields) != len(set(fields))
                or any(
                    field not in _ORDER_FIELDS or not header
                    for field, header in normalized
                )
            ):
                raise OrderRuleError("ORDER_TEMPLATE_COLUMNS_INVALID")
            workspace = self.connection.execute(
                "SELECT * FROM acquisition_workspaces WHERE id = ? AND school_id = ?",
                (workspace_id, school_id),
            ).fetchone()
            if workspace is None:
                raise OrderRuleError("WORKSPACE_NOT_FOUND")
            if workspace["status"] not in _ORDER_TEMPLATE_STATES:
                raise OrderRuleError("ORDER_TEMPLATE_STATE_INVALID")
            template_version = self.connection.execute(
                """
                SELECT COALESCE(MAX(template_version), 0) + 1
                FROM workflow_order_templates
                WHERE school_id = ? AND vendor_name = ?
                """,
                (school_id, vendor_name.strip()),
            ).fetchone()[0]
            now = format_utc(utc_now())
            template_id = str(uuid.uuid4())
            self.connection.execute(
                """
                INSERT INTO workflow_order_templates (
                    id, school_id, vendor_name, template_version, columns_json,
                    created_by_user_id, reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    template_id,
                    school_id,
                    vendor_name.strip(),
                    template_version,
                    json.dumps(normalized, ensure_ascii=False, separators=(",", ":")),
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
                changes={"updated_at": now},
            )
            result = {
                "template_id": template_id,
                "template_version": template_version,
                "vendor_name": vendor_name.strip(),
                "columns": [list(item) for item in normalized],
                "state": updated["status"],
                "row_version": updated["row_version"],
            }
            record_audit_event(
                self.connection,
                actor_id=actor_id,
                school_id=school_id,
                action="ORDER_TEMPLATE_SAVED",
                entity_type="workflow_order_template",
                entity_id=template_id,
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
            route="orders.templates.save",
            key=idempotency_key,
            request_body=body,
            operation=mutate,
        )


class OrderService:
    def __init__(self, connection: sqlite3.Connection, artifact_root: Path) -> None:
        self.connection = connection
        self.artifact_root = artifact_root

    def _template_columns(
        self, school_id: str, vendor_name: str
    ) -> tuple[tuple[str, str], ...]:
        row = self.connection.execute(
            """
            SELECT columns_json FROM workflow_order_templates
            WHERE school_id = ? AND vendor_name = ?
            ORDER BY template_version DESC LIMIT 1
            """,
            (school_id, vendor_name),
        ).fetchone()
        if row is None:
            return DEFAULT_ORDER_COLUMNS
        return tuple(tuple(item) for item in json.loads(row["columns_json"]))

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
        allocations: list[dict[str, object]] | None = None,
        advanced_split_enabled: bool = False,
    ) -> dict[str, object]:
        body = {
            "workspace_id": workspace_id,
            "approval_revision_id": approval_revision_id,
            "quote_id": quote_id,
            "allocations": allocations,
            "advanced_split_enabled": advanced_split_enabled,
            "workspace_version": workspace_version,
            "reason": reason,
        }
        created_paths: list[Path] = []

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
            if workspace is None:
                raise OrderRuleError("WORKSPACE_NOT_FOUND")
            if workspace["status"] not in {
                "QUOTE_REVIEW",
                "ORDER_READY",
                "ORDER_SENT",
                "RECEIVING",
            }:
                raise OrderRuleError("ORDER_STATE_INVALID")
            current_approval = self.connection.execute(
                """
                SELECT ar.* FROM approval_revisions ar
                JOIN approval_decisions ad ON ad.approval_revision_id = ar.id
                WHERE ar.school_id = ? AND ar.workspace_id = ? AND ad.decision = 'APPROVED'
                ORDER BY ar.revision_number DESC LIMIT 1
                """,
                (school_id, workspace_id),
            ).fetchone()
            if (
                current_approval is None
                or current_approval["id"] != approval_revision_id
            ):
                raise OrderRuleError("APPROVAL_REVISION_NOT_CURRENT")
            approved_rows = {
                row["id"]: row
                for row in self.connection.execute(
                    "SELECT * FROM approval_rows WHERE approval_revision_id = ?",
                    (approval_revision_id,),
                ).fetchall()
            }
            candidates = {
                row["id"]: row
                for row in self.connection.execute(
                    """
                    SELECT * FROM candidate_decisions
                    WHERE school_id = ? AND workspace_id = ?
                      AND outcome = 'CANDIDATE' AND quantity > 0
                    """,
                    (school_id, workspace_id),
                ).fetchall()
            }
            selected: list[dict[str, Any]] = []
            unmapped_ids: list[str] = []
            if allocations is None:
                allocation_inputs = [
                    {
                        "quote_id": quote_id,
                        "quote_row_id": row["id"],
                        "quantity": row["quantity"],
                    }
                    for row in self.connection.execute(
                        "SELECT * FROM vendor_quote_rows WHERE quote_id = ? ORDER BY id",
                        (quote_id,),
                    ).fetchall()
                ]
            else:
                quote_ids = {str(item.get("quote_id", "")) for item in allocations}
                if len(quote_ids) > 1 and not advanced_split_enabled:
                    raise OrderRuleError("ADVANCED_SPLIT_ORDER_DISABLED")
                allocation_inputs = allocations
            if not allocation_inputs:
                raise OrderRuleError("EMPTY_ORDER")
            for item in allocation_inputs:
                selected_quote_id = str(item.get("quote_id") or "")
                quote_row_id = str(item.get("quote_row_id") or "")
                quantity = item.get("quantity")
                if (
                    isinstance(quantity, bool)
                    or not isinstance(quantity, int)
                    or quantity <= 0
                ):
                    unmapped_ids.append(quote_row_id)
                    continue
                row = self.connection.execute(
                    """
                    SELECT vqr.*, vq.vendor_name, vq.approval_revision_id,
                           vq.school_id AS quote_school_id, vq.workspace_id AS quote_workspace_id
                    FROM vendor_quote_rows vqr
                    JOIN vendor_quotes vq ON vq.id = vqr.quote_id
                    WHERE vqr.id = ? AND vqr.quote_id = ? AND vq.sealed_at IS NOT NULL
                    """,
                    (quote_row_id, selected_quote_id),
                ).fetchone()
                approved = (
                    approved_rows.get(row["approval_row_id"])
                    if row is not None
                    else None
                )
                candidate = (
                    candidates.get(approved["candidate_id"])
                    if approved is not None
                    else None
                )
                if (
                    row is None
                    or row["quote_school_id"] != school_id
                    or row["quote_workspace_id"] != workspace_id
                    or row["approval_revision_id"] != approval_revision_id
                    or approved is None
                    or candidate is None
                    or row["match_status"] not in {"MATCHED_ISBN", "MATCHED_MANUAL"}
                    or row["unit_price"] is None
                    or row["out_of_stock"]
                ):
                    unmapped_ids.append(quote_row_id)
                    continue
                selected.append(
                    {
                        "quote_id": selected_quote_id,
                        "quote_row_id": quote_row_id,
                        "candidate_id": candidate["id"],
                        "vendor_name": row["vendor_name"],
                        "quantity": quantity,
                        "unit_price": row["unit_price"],
                        "quote_row": row,
                        "approved": approved,
                        "candidate": candidate,
                    }
                )
            grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for item in selected:
                grouped[item["candidate_id"]].append(item)
            totals = {
                candidate_id: sum(item["quantity"] for item in items)
                for candidate_id, items in grouped.items()
            }
            missing = sorted(
                candidate_id
                for candidate_id, candidate in candidates.items()
                if totals.get(candidate_id, 0) < candidate["quantity"]
            )
            duplicate = sorted(
                candidate_id
                for candidate_id, items in grouped.items()
                if len(items) > 1
            )
            over = [
                {
                    "candidate_id": candidate_id,
                    "expected": candidates[candidate_id]["quantity"],
                    "allocated": allocated,
                }
                for candidate_id, allocated in sorted(totals.items())
                if allocated > candidates[candidate_id]["quantity"]
            ]
            price_conflicts = sorted(
                candidate_id
                for candidate_id, items in grouped.items()
                if len({item["unit_price"] for item in items}) > 1
            )
            diagnostics = {
                "missing_candidate_ids": missing,
                "duplicate_candidate_ids": duplicate,
                "over_allocations": over,
                "price_conflict_candidate_ids": price_conflicts,
                "unmapped_quote_row_ids": sorted(set(unmapped_ids)),
            }
            now = format_utc(utc_now())
            if any(diagnostics.values()):
                return self._persist_invalid(
                    school_id=school_id,
                    workspace_id=workspace_id,
                    approval_revision_id=approval_revision_id,
                    actor_id=actor_id,
                    workspace_version=workspace_version,
                    reason=reason,
                    request_id=request_id,
                    workspace=workspace,
                    diagnostics=diagnostics,
                    now=now,
                )
            total = sum(item["quantity"] * item["unit_price"] for item in selected)
            if total > current_approval["budget_won"] or any(
                item["unit_price"] > item["approved"]["unit_price"]
                or item["unit_price"] > item["candidate"]["unit_price"]
                for item in selected
            ):
                raise OrderRuleError("APPROVAL_SCOPE_EXCEEDED")
            return self._seal_revision(
                school_id=school_id,
                workspace_id=workspace_id,
                approval_revision_id=approval_revision_id,
                quote_id=quote_id,
                actor_id=actor_id,
                workspace_version=workspace_version,
                reason=reason,
                request_id=request_id,
                workspace=workspace,
                selected=selected,
                now=now,
                created_paths=created_paths,
            )

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
            for path in created_paths:
                path.unlink(missing_ok=True)
            raise
        if result.get("status") != "READY":
            return result
        return {
            **result,
            "path": Path(str(result["path"])),
            "artifacts": [
                {**item, "path": Path(str(item["path"]))}
                for item in result["artifacts"]
            ],
        }

    def _persist_invalid(
        self,
        *,
        school_id: str,
        workspace_id: str,
        approval_revision_id: str,
        actor_id: str,
        workspace_version: int,
        reason: str,
        request_id: str,
        workspace: sqlite3.Row,
        diagnostics: dict[str, Any],
        now: str,
    ) -> dict[str, object]:
        validation_id = str(uuid.uuid4())
        self.connection.execute(
            """
            INSERT INTO order_validation_results (
                id, school_id, workspace_id, approval_revision_id,
                diagnostics_json, actor_id, reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                validation_id,
                school_id,
                workspace_id,
                approval_revision_id,
                json.dumps(diagnostics, ensure_ascii=False, sort_keys=True),
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
            changes={"updated_at": now},
        )
        result = {
            "status": "INVALID",
            "validation_id": validation_id,
            "diagnostics": diagnostics,
            "state": updated["status"],
            "row_version": updated["row_version"],
        }
        record_audit_event(
            self.connection,
            actor_id=actor_id,
            school_id=school_id,
            action="ORDER_REVISION_VALIDATION_FAILED",
            entity_type="order_validation_result",
            entity_id=validation_id,
            before={
                "state": workspace["status"],
                "row_version": workspace["row_version"],
            },
            after={**result, "reason": reason.strip()},
            request_id=request_id,
        )
        return result

    def _seal_revision(
        self,
        *,
        school_id: str,
        workspace_id: str,
        approval_revision_id: str,
        quote_id: str,
        actor_id: str,
        workspace_version: int,
        reason: str,
        request_id: str,
        workspace: sqlite3.Row,
        selected: list[dict[str, Any]],
        now: str,
        created_paths: list[Path],
    ) -> dict[str, object]:
        by_vendor_quote: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for item in selected:
            by_vendor_quote[(item["vendor_name"], item["quote_id"])].append(item)
        stored_artifacts: list[tuple[str, str, Any]] = []
        for (vendor_name, selected_quote_id), items in sorted(by_vendor_quote.items()):
            export_rows = [
                {
                    "isbn": item["quote_row"]["isbn13"] or "",
                    "title": item["quote_row"]["title"],
                    "author": item["quote_row"]["author"],
                    "publisher": item["quote_row"]["publisher"] or "",
                    "quantity": item["quantity"],
                    "unit_price": item["unit_price"],
                    "line_total_won": item["quantity"] * item["unit_price"],
                }
                for item in items
            ]
            stored = store_order_artifact(
                self.artifact_root / school_id / workspace_id / selected_quote_id,
                export_rows,
                columns=self._template_columns(school_id, vendor_name),
            )
            created_paths.append(stored.path)
            stored_artifacts.append((vendor_name, selected_quote_id, stored))

        revision_number = self.connection.execute(
            "SELECT COALESCE(MAX(revision_number), 0) + 1 FROM order_revisions WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchone()[0]
        revision_id = str(uuid.uuid4())
        artifact_results: list[dict[str, object]] = []
        artifact_ids: list[tuple[str, str, str]] = []
        for vendor_name, selected_quote_id, stored in stored_artifacts:
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
            artifact_ids.append((vendor_name, selected_quote_id, artifact_id))
            artifact_results.append(
                {
                    "vendor_name": vendor_name,
                    "quote_id": selected_quote_id,
                    "artifact_id": artifact_id,
                    "path": str(stored.path),
                    "sha256": stored.sha256,
                    "size_bytes": stored.size_bytes,
                }
            )
        primary_artifact_id = artifact_ids[0][2]
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
                primary_artifact_id,
                revision_number,
                reason.strip(),
                actor_id,
                now,
            ),
        )
        for vendor_name, selected_quote_id, artifact_id in artifact_ids:
            self.connection.execute(
                """
                INSERT INTO order_vendor_artifacts (
                    id, order_revision_id, quote_id, vendor_name, artifact_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    revision_id,
                    selected_quote_id,
                    vendor_name,
                    artifact_id,
                    now,
                ),
            )
        for item in selected:
            row = item["quote_row"]
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
                    item["approved"]["id"],
                    row["isbn13"],
                    row["title"],
                    row["author"],
                    row["publisher"],
                    row["edition"],
                    item["quantity"],
                    item["unit_price"],
                    item["quantity"] * item["unit_price"],
                    now,
                ),
            )
            self.connection.execute(
                """
                INSERT INTO order_allocations (
                    id, order_revision_id, quote_id, quote_row_id, candidate_id,
                    vendor_name, quantity, unit_price, line_total_won, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    revision_id,
                    item["quote_id"],
                    item["quote_row_id"],
                    item["candidate_id"],
                    item["vendor_name"],
                    item["quantity"],
                    item["unit_price"],
                    item["quantity"] * item["unit_price"],
                    now,
                ),
            )
        self.connection.execute(
            "UPDATE order_revisions SET sealed_at = ? WHERE id = ?", (now, revision_id)
        )
        self.connection.execute(
            """
            INSERT INTO workspace_current_orders (
                workspace_id, school_id, order_revision_id, transmission_id, updated_at
            ) VALUES (?, ?, ?, NULL, ?)
            ON CONFLICT(workspace_id) DO UPDATE SET
                school_id = excluded.school_id,
                order_revision_id = excluded.order_revision_id,
                transmission_id = NULL,
                row_version = workspace_current_orders.row_version + 1,
                updated_at = excluded.updated_at
            """,
            (workspace_id, school_id, revision_id, now),
        )
        updated = update_with_version(
            self.connection,
            table="acquisition_workspaces",
            school_id=school_id,
            entity_id=workspace_id,
            submitted_version=workspace_version,
            changes={"status": "ORDER_READY", "updated_at": now},
        )
        primary = artifact_results[0]
        result = {
            "status": "READY",
            "revision_id": revision_id,
            "revision_number": revision_number,
            "artifact_id": primary["artifact_id"],
            "path": primary["path"],
            "sha256": primary["sha256"],
            "size_bytes": primary["size_bytes"],
            "artifacts": artifact_results,
            "allocations": [
                {
                    "candidate_id": item["candidate_id"],
                    "quote_id": item["quote_id"],
                    "quote_row_id": item["quote_row_id"],
                    "vendor_name": item["vendor_name"],
                    "quantity": item["quantity"],
                    "unit_price": item["unit_price"],
                }
                for item in selected
            ],
            "state": updated["status"],
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
            current = self.connection.execute(
                """
                SELECT current.order_revision_id, current.transmission_id
                FROM workspace_current_orders current
                JOIN order_revisions ors ON ors.id = current.order_revision_id
                WHERE current.workspace_id = ? AND current.school_id = ? AND ors.sealed_at IS NOT NULL
                """,
                (workspace_id, school_id),
            ).fetchone()
            if workspace is None or current is None:
                raise OrderRuleError("ORDER_NOT_FOUND")
            if current["order_revision_id"] != order_revision_id:
                raise OrderRuleError("ORDER_REVISION_NOT_CURRENT")
            if (
                workspace["status"] != "ORDER_READY"
                or current["transmission_id"] is not None
            ):
                raise OrderRuleError("ORDER_SEND_STATE_INVALID")
            now = format_utc(utc_now())
            transmission_id = str(uuid.uuid4())
            self.connection.execute(
                """
                INSERT INTO order_transmissions (
                    id, order_revision_id, school_id, workspace_id, actor_id, reason, sent_at
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
            self.connection.execute(
                """
                UPDATE workspace_current_orders
                SET transmission_id = ?, row_version = row_version + 1, updated_at = ?
                WHERE workspace_id = ? AND school_id = ? AND order_revision_id = ?
                """,
                (transmission_id, now, workspace_id, school_id, order_revision_id),
            )
            updated = update_with_version(
                self.connection,
                table="acquisition_workspaces",
                school_id=school_id,
                entity_id=workspace_id,
                submitted_version=workspace_version,
                changes={"status": "ORDER_SENT", "updated_at": now},
            )
            result = {
                "transmission_id": transmission_id,
                "state": "ORDER_SENT",
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
