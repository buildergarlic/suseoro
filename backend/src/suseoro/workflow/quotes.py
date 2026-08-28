"""Normalized vendor-quote comparison with explicit budget outcomes."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterable
from typing import Any

from suseoro.catalog.normalization import (
    canonical_isbn13,
    normalize_author,
    normalize_key,
)
from suseoro.security.sessions import format_utc, utc_now
from suseoro.services.audit import record_audit_event
from suseoro.services.concurrency import update_with_version
from suseoro.workflow._common import (
    WorkflowDomainError,
    idempotent_mutation,
    require_role,
)


class QuoteRuleError(WorkflowDomainError):
    pass


def select_vendor_plan(
    rows: Iterable[dict[str, Any]],
    *,
    split_order: bool,
    advanced_split_enabled: bool,
) -> list[str]:
    vendors = list(dict.fromkeys(str(row["vendor"]) for row in rows))
    if split_order and not advanced_split_enabled:
        raise QuoteRuleError("ADVANCED_SPLIT_ORDER_DISABLED")
    return vendors if split_order else vendors[:1]


class QuoteService:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def ingest(
        self,
        *,
        school_id: str,
        workspace_id: str,
        approval_revision_id: str,
        actor_id: str,
        actor_roles: tuple[str, ...],
        workspace_version: int,
        vendor_name: str,
        rows: list[dict[str, Any]],
        reason: str,
        idempotency_key: str,
        request_id: str,
    ) -> dict[str, Any]:
        body = {
            "workspace_id": workspace_id,
            "approval_revision_id": approval_revision_id,
            "workspace_version": workspace_version,
            "vendor_name": vendor_name,
            "rows": rows,
            "reason": reason,
        }

        def mutate() -> dict[str, Any]:
            require_role(
                self.connection,
                school_id=school_id,
                actor_id=actor_id,
                actor_roles=actor_roles,
                role="OPERATOR",
                error_type=QuoteRuleError,
            )
            if not vendor_name.strip():
                raise QuoteRuleError("VENDOR_NAME_REQUIRED")
            if not reason.strip():
                raise QuoteRuleError("MODIFICATION_REASON_REQUIRED")
            workspace = self.connection.execute(
                "SELECT * FROM acquisition_workspaces WHERE id = ? AND school_id = ?",
                (workspace_id, school_id),
            ).fetchone()
            if workspace is None:
                raise QuoteRuleError("WORKSPACE_NOT_FOUND")
            if workspace["status"] not in {"APPROVED", "QUOTE_REVIEW"}:
                raise QuoteRuleError("QUOTE_STATE_INVALID")
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
                raise QuoteRuleError("APPROVAL_REVISION_NOT_CURRENT")
            revision = self.connection.execute(
                """
                SELECT * FROM approval_revisions
                WHERE id = ? AND school_id = ? AND workspace_id = ?
                  AND sealed_at IS NOT NULL
                """,
                (approval_revision_id, school_id, workspace_id),
            ).fetchone()
            if revision is None:
                raise QuoteRuleError("APPROVAL_REVISION_NOT_FOUND")
            approval_rows = self.connection.execute(
                "SELECT * FROM approval_rows WHERE approval_revision_id = ?",
                (approval_revision_id,),
            ).fetchall()
            by_isbn = {row["isbn13"]: row for row in approval_rows if row["isbn13"]}
            by_title_author = {
                (normalize_key(row["title"]), normalize_author(row["author"])): row
                for row in approval_rows
                if not row["isbn13"]
            }
            normalized_rows: list[dict[str, Any]] = []
            total = 0
            list_total = 0
            out_of_stock_count = 0
            missing_price_count = 0
            list_mismatch_count = 0
            needs_review_count = 0
            unmatched_count = 0
            scope_increase = False
            for input_row in rows:
                title = str(input_row.get("title") or "")
                author = str(input_row.get("author") or "")
                isbn13 = canonical_isbn13(input_row.get("isbn"))
                approval_row = by_isbn.get(isbn13) if isbn13 else None
                if approval_row is not None:
                    match_status = "MATCHED_ISBN"
                elif isbn13 is None:
                    approval_row = by_title_author.get(
                        (normalize_key(title), normalize_author(author))
                    )
                    match_status = (
                        "NEEDS_REVIEW" if approval_row is not None else "UNMATCHED"
                    )
                else:
                    match_status = "UNMATCHED"
                quantity = input_row.get("quantity", 1)
                if (
                    isinstance(quantity, bool)
                    or not isinstance(quantity, int)
                    or quantity <= 0
                ):
                    raise QuoteRuleError("INVALID_QUOTE_QUANTITY")
                unit_price = input_row.get("unit_price")
                list_price = input_row.get("list_price")
                for name, value in (
                    ("unit_price", unit_price),
                    ("list_price", list_price),
                ):
                    if value is not None and (
                        isinstance(value, bool)
                        or not isinstance(value, int)
                        or value < 0
                    ):
                        raise QuoteRuleError(f"INVALID_QUOTE_{name.upper()}")
                line_total = quantity * unit_price if unit_price is not None else 0
                total += line_total
                list_total += quantity * (
                    list_price if list_price is not None else (unit_price or 0)
                )
                out_of_stock = bool(input_row.get("out_of_stock", False))
                out_of_stock_count += int(out_of_stock)
                missing_price_count += int(unit_price is None)
                needs_review_count += int(match_status == "NEEDS_REVIEW")
                unmatched_count += int(match_status == "UNMATCHED")
                if approval_row is not None:
                    list_mismatch_count += int(
                        list_price is not None
                        and list_price != approval_row["unit_price"]
                    )
                    scope_increase = (
                        scope_increase or quantity > approval_row["quantity"]
                    )
                    scope_increase = scope_increase or (
                        unit_price is not None
                        and unit_price > approval_row["unit_price"]
                    )
                normalized_rows.append(
                    {
                        "id": str(uuid.uuid4()),
                        "approval_row_id": approval_row["id"] if approval_row else None,
                        "match_status": match_status,
                        "isbn13": isbn13,
                        "title": title,
                        "author": author,
                        "publisher": input_row.get("publisher"),
                        "edition": input_row.get("edition"),
                        "quantity": quantity,
                        "unit_price": unit_price,
                        "list_price": list_price,
                        "out_of_stock": out_of_stock,
                        "line_total_won": line_total,
                    }
                )
            rows_by_isbn: dict[str, list[dict[str, Any]]] = {}
            for row in normalized_rows:
                if row["isbn13"]:
                    rows_by_isbn.setdefault(row["isbn13"], []).append(row)
            duplicate_isbns = sorted(
                isbn for isbn, matches in rows_by_isbn.items() if len(matches) > 1
            )
            price_conflicts = sorted(
                isbn
                for isbn, matches in rows_by_isbn.items()
                if len({row["unit_price"] for row in matches}) > 1
            )
            reconciliation = {
                "duplicate_isbns": duplicate_isbns,
                "price_conflicts": price_conflicts,
            }
            budget_overrun = max(0, total - revision["budget_won"])
            requires_reapproval = bool(
                scope_increase
                or budget_overrun
                or unmatched_count
                or duplicate_isbns
                or price_conflicts
            )
            quote_id = str(uuid.uuid4())
            now = format_utc(utc_now())
            revision_number = self.connection.execute(
                """
                SELECT COALESCE(MAX(revision_number), 0) + 1
                FROM vendor_quotes
                WHERE workspace_id = ? AND vendor_name = ?
                """,
                (workspace_id, vendor_name.strip()),
            ).fetchone()[0]
            self.connection.execute(
                """
                INSERT INTO vendor_quotes (
                    id, school_id, workspace_id, approval_revision_id,
                    vendor_name, total_won, list_total_won, discount_won,
                    budget_overrun_won, out_of_stock_count, missing_price_count,
                    list_mismatch_count, needs_review_count, unmatched_count,
                    requires_reapproval, reason, created_by_user_id, created_at,
                    revision_number, reconciliation_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    quote_id,
                    school_id,
                    workspace_id,
                    approval_revision_id,
                    vendor_name.strip(),
                    total,
                    list_total,
                    list_total - total,
                    budget_overrun,
                    out_of_stock_count,
                    missing_price_count,
                    list_mismatch_count,
                    needs_review_count,
                    unmatched_count,
                    int(requires_reapproval),
                    reason.strip(),
                    actor_id,
                    now,
                    revision_number,
                    json.dumps(reconciliation, ensure_ascii=False, sort_keys=True),
                ),
            )
            for row in normalized_rows:
                self.connection.execute(
                    """
                    INSERT INTO vendor_quote_rows (
                        id, quote_id, approval_row_id, match_status, isbn13,
                        title, author, publisher, edition, quantity, unit_price,
                        list_price, out_of_stock, line_total_won, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["id"],
                        quote_id,
                        row["approval_row_id"],
                        row["match_status"],
                        row["isbn13"],
                        row["title"],
                        row["author"],
                        row["publisher"],
                        row["edition"],
                        row["quantity"],
                        row["unit_price"],
                        row["list_price"],
                        int(row["out_of_stock"]),
                        row["line_total_won"],
                        now,
                    ),
                )
            self.connection.execute(
                "UPDATE vendor_quotes SET sealed_at = ? WHERE id = ?", (now, quote_id)
            )
            updated = update_with_version(
                self.connection,
                table="acquisition_workspaces",
                school_id=school_id,
                entity_id=workspace_id,
                submitted_version=workspace_version,
                changes={"status": "QUOTE_REVIEW", "updated_at": now},
            )
            public_rows = [
                {
                    **{key: value for key, value in row.items() if key != "id"},
                    "quote_row_id": row["id"],
                }
                for row in normalized_rows
            ]
            result = {
                "quote_id": quote_id,
                "revision_number": revision_number,
                "state": updated["status"],
                "row_version": updated["row_version"],
                "total_won": total,
                "list_total_won": list_total,
                "discount_won": list_total - total,
                "budget_overrun_won": budget_overrun,
                "out_of_stock_count": out_of_stock_count,
                "missing_price_count": missing_price_count,
                "list_mismatch_count": list_mismatch_count,
                "needs_review_count": needs_review_count,
                "unmatched_count": unmatched_count,
                "requires_reapproval": requires_reapproval,
                "reconciliation": reconciliation,
                "rows": public_rows,
            }
            record_audit_event(
                self.connection,
                actor_id=actor_id,
                school_id=school_id,
                action="VENDOR_QUOTE_INGESTED",
                entity_type="vendor_quote",
                entity_id=quote_id,
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
            route="quotes.ingest",
            key=idempotency_key,
            request_body=body,
            operation=mutate,
        )

    def confirm_manual_match(
        self,
        *,
        school_id: str,
        workspace_id: str,
        quote_id: str,
        quote_row_id: str,
        approval_row_id: str,
        actor_id: str,
        actor_roles: tuple[str, ...],
        workspace_version: int,
        reason: str,
        idempotency_key: str,
        request_id: str,
    ) -> dict[str, Any]:
        body = {
            "workspace_id": workspace_id,
            "quote_id": quote_id,
            "quote_row_id": quote_row_id,
            "approval_row_id": approval_row_id,
            "workspace_version": workspace_version,
            "reason": reason,
        }

        def mutate() -> dict[str, Any]:
            require_role(
                self.connection,
                school_id=school_id,
                actor_id=actor_id,
                actor_roles=actor_roles,
                role="OPERATOR",
                error_type=QuoteRuleError,
            )
            if not reason.strip():
                raise QuoteRuleError("MODIFICATION_REASON_REQUIRED")
            workspace = self.connection.execute(
                "SELECT * FROM acquisition_workspaces WHERE id = ? AND school_id = ?",
                (workspace_id, school_id),
            ).fetchone()
            if workspace is None:
                raise QuoteRuleError("WORKSPACE_NOT_FOUND")
            if workspace["status"] != "QUOTE_REVIEW":
                raise QuoteRuleError("QUOTE_STATE_INVALID")
            source = self.connection.execute(
                """
                SELECT * FROM vendor_quotes
                WHERE id = ? AND school_id = ? AND workspace_id = ?
                  AND sealed_at IS NOT NULL
                """,
                (quote_id, school_id, workspace_id),
            ).fetchone()
            if source is None:
                raise QuoteRuleError("QUOTE_NOT_FOUND")
            source_row = self.connection.execute(
                """
                SELECT * FROM vendor_quote_rows
                WHERE id = ? AND quote_id = ? AND match_status = 'NEEDS_REVIEW'
                """,
                (quote_row_id, quote_id),
            ).fetchone()
            target = self.connection.execute(
                """
                SELECT * FROM approval_rows
                WHERE id = ? AND approval_revision_id = ?
                """,
                (approval_row_id, source["approval_revision_id"]),
            ).fetchone()
            if source_row is None or target is None:
                raise QuoteRuleError("MANUAL_MATCH_SCOPE_INVALID")

            result_quote_id = str(uuid.uuid4())
            revision_number = self.connection.execute(
                """
                SELECT COALESCE(MAX(revision_number), 0) + 1
                FROM vendor_quotes
                WHERE workspace_id = ? AND vendor_name = ?
                """,
                (workspace_id, source["vendor_name"]),
            ).fetchone()[0]
            now = format_utc(utc_now())
            needs_review_count = max(0, source["needs_review_count"] - 1)
            self.connection.execute(
                """
                INSERT INTO vendor_quotes (
                    id, school_id, workspace_id, approval_revision_id,
                    vendor_name, total_won, list_total_won, discount_won,
                    budget_overrun_won, out_of_stock_count, missing_price_count,
                    list_mismatch_count, needs_review_count, unmatched_count,
                    requires_reapproval, reason, created_by_user_id, created_at,
                    revision_number, parent_quote_id, reconciliation_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result_quote_id,
                    school_id,
                    workspace_id,
                    source["approval_revision_id"],
                    source["vendor_name"],
                    source["total_won"],
                    source["list_total_won"],
                    source["discount_won"],
                    source["budget_overrun_won"],
                    source["out_of_stock_count"],
                    source["missing_price_count"],
                    source["list_mismatch_count"],
                    needs_review_count,
                    source["unmatched_count"],
                    source["requires_reapproval"],
                    reason.strip(),
                    actor_id,
                    now,
                    revision_number,
                    quote_id,
                    source["reconciliation_json"],
                ),
            )
            public_rows: list[dict[str, Any]] = []
            for row in self.connection.execute(
                "SELECT * FROM vendor_quote_rows WHERE quote_id = ? ORDER BY rowid",
                (quote_id,),
            ).fetchall():
                new_id = str(uuid.uuid4())
                is_target = row["id"] == quote_row_id
                match_status = "MATCHED_MANUAL" if is_target else row["match_status"]
                matched_approval_id = (
                    approval_row_id if is_target else row["approval_row_id"]
                )
                self.connection.execute(
                    """
                    INSERT INTO vendor_quote_rows (
                        id, quote_id, approval_row_id, match_status, isbn13,
                        title, author, publisher, edition, quantity, unit_price,
                        list_price, out_of_stock, line_total_won, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        new_id,
                        result_quote_id,
                        matched_approval_id,
                        match_status,
                        row["isbn13"],
                        row["title"],
                        row["author"],
                        row["publisher"],
                        row["edition"],
                        row["quantity"],
                        row["unit_price"],
                        row["list_price"],
                        row["out_of_stock"],
                        row["line_total_won"],
                        now,
                    ),
                )
                public_rows.append(
                    {
                        "quote_row_id": new_id,
                        "approval_row_id": matched_approval_id,
                        "match_status": match_status,
                        "isbn13": row["isbn13"],
                        "title": row["title"],
                        "author": row["author"],
                        "publisher": row["publisher"],
                        "edition": row["edition"],
                        "quantity": row["quantity"],
                        "unit_price": row["unit_price"],
                        "list_price": row["list_price"],
                        "out_of_stock": bool(row["out_of_stock"]),
                        "line_total_won": row["line_total_won"],
                    }
                )
            self.connection.execute(
                "UPDATE vendor_quotes SET sealed_at = ? WHERE id = ?",
                (now, result_quote_id),
            )
            self.connection.execute(
                """
                INSERT INTO vendor_quote_manual_mappings (
                    id, source_quote_id, result_quote_id, source_quote_row_id,
                    approval_row_id, school_id, workspace_id, actor_id, reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    quote_id,
                    result_quote_id,
                    quote_row_id,
                    approval_row_id,
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
                changes={"updated_at": now},
            )
            result = {
                "quote_id": result_quote_id,
                "parent_quote_id": quote_id,
                "revision_number": revision_number,
                "state": updated["status"],
                "row_version": updated["row_version"],
                "rows": public_rows,
            }
            record_audit_event(
                self.connection,
                actor_id=actor_id,
                school_id=school_id,
                action="VENDOR_QUOTE_MANUAL_MATCH_CONFIRMED",
                entity_type="vendor_quote",
                entity_id=result_quote_id,
                before={
                    "quote_id": quote_id,
                    "quote_row_id": quote_row_id,
                    "state": workspace["status"],
                    "row_version": workspace["row_version"],
                },
                after={
                    **result,
                    "approval_row_id": approval_row_id,
                    "reason": reason.strip(),
                },
                request_id=request_id,
            )
            return result

        return idempotent_mutation(
            self.connection,
            school_id=school_id,
            actor_id=actor_id,
            route="quotes.confirm_manual_match",
            key=idempotency_key,
            request_body=body,
            operation=mutate,
        )
