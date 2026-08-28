"""Accumulating deliveries, keyboard-ISBN scans, and completion rules."""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from suseoro.catalog.normalization import canonical_isbn13, normalize_key
from suseoro.security.sessions import format_utc, utc_now
from suseoro.services.audit import record_audit_event
from suseoro.services.concurrency import update_with_version
from suseoro.workflow._common import (
    WorkflowDomainError,
    idempotent_mutation,
    require_role,
)

_DISPOSITIONS = {
    "VENDOR_CONFIRM",
    "RETURN_PLANNED",
    "ADDITIONAL_DELIVERY_PLANNED",
    "ACCEPTED",
}


class ReceivingRuleError(WorkflowDomainError):
    pass


class ReceivingService:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def _authorize(self, school_id: str, actor_id: str, roles: tuple[str, ...]) -> None:
        require_role(
            self.connection,
            school_id=school_id,
            actor_id=actor_id,
            actor_roles=roles,
            role="OPERATOR",
            error_type=ReceivingRuleError,
        )

    def _workspace(self, school_id: str, workspace_id: str):
        row = self.connection.execute(
            "SELECT * FROM acquisition_workspaces WHERE id = ? AND school_id = ?",
            (workspace_id, school_id),
        ).fetchone()
        if row is None:
            raise ReceivingRuleError("WORKSPACE_NOT_FOUND")
        return row

    def _order(self, school_id: str, workspace_id: str, order_revision_id: str):
        row = self.connection.execute(
            """
            SELECT * FROM order_revisions
            WHERE id = ? AND school_id = ? AND workspace_id = ? AND sealed_at IS NOT NULL
            """,
            (order_revision_id, school_id, workspace_id),
        ).fetchone()
        if row is None:
            raise ReceivingRuleError("ORDER_NOT_FOUND")
        return row

    def _insert_difference(
        self,
        *,
        school_id: str,
        workspace_id: str,
        order_revision_id: str,
        order_row_id: str | None,
        kind: str,
        reference_key: str,
        details: dict[str, Any],
        now: str,
    ) -> dict[str, Any]:
        existing = self.connection.execute(
            """
            SELECT * FROM receiving_differences
            WHERE workspace_id = ? AND order_revision_id = ?
              AND kind = ? AND reference_key = ? AND active = 1
            ORDER BY created_at DESC LIMIT 1
            """,
            (workspace_id, order_revision_id, kind, reference_key),
        ).fetchone()
        encoded = json.dumps(
            details, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        if existing is not None:
            self.connection.execute(
                """
                UPDATE receiving_differences
                SET details_json = ?, row_version = row_version + 1, updated_at = ?
                WHERE id = ?
                """,
                (encoded, now, existing["id"]),
            )
            difference_id = existing["id"]
        else:
            difference_id = str(uuid.uuid4())
            self.connection.execute(
                """
                INSERT INTO receiving_differences (
                    id, school_id, workspace_id, order_revision_id, order_row_id,
                    kind, reference_key, details_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    difference_id,
                    school_id,
                    workspace_id,
                    order_revision_id,
                    order_row_id,
                    kind,
                    reference_key,
                    encoded,
                    now,
                    now,
                ),
            )
        return {"id": difference_id, "kind": kind, "details": details}

    def _delivery_differences(
        self,
        *,
        school_id: str,
        workspace_id: str,
        order_revision_id: str,
        now: str,
    ) -> tuple[list[dict[str, Any]], int]:
        self.connection.execute(
            """
            UPDATE receiving_differences SET active = 0, updated_at = ?
            WHERE workspace_id = ? AND order_revision_id = ?
              AND active = 1 AND reference_key LIKE 'delivery:%'
            """,
            (now, workspace_id, order_revision_id),
        )
        order_rows = self.connection.execute(
            "SELECT * FROM order_rows WHERE order_revision_id = ?",
            (order_revision_id,),
        ).fetchall()
        deliveries = self.connection.execute(
            """
            SELECT dr.* FROM delivery_rows dr
            JOIN delivery_batches db ON db.id = dr.delivery_batch_id
            WHERE db.workspace_id = ? AND db.order_revision_id = ?
              AND db.sealed_at IS NOT NULL
            ORDER BY db.delivery_number, dr.id
            """,
            (workspace_id, order_revision_id),
        ).fetchall()
        by_isbn = {row["isbn13"]: row for row in order_rows if row["isbn13"]}
        by_title: dict[str, list[sqlite3.Row]] = {}
        for row in order_rows:
            by_title.setdefault(normalize_key(row["title"]), []).append(row)
        received_by_order: dict[str, int] = {row["id"]: 0 for row in order_rows}
        differences: list[dict[str, Any]] = []
        for delivered in deliveries:
            ordered = by_isbn.get(delivered["isbn13"])
            if ordered is None:
                title_matches = by_title.get(normalize_key(delivered["title"]), [])
                if len(title_matches) == 1:
                    ordered = title_matches[0]
                    differences.append(
                        self._insert_difference(
                            school_id=school_id,
                            workspace_id=workspace_id,
                            order_revision_id=order_revision_id,
                            order_row_id=ordered["id"],
                            kind="ISBN",
                            reference_key=f"delivery:isbn:{delivered['id']}",
                            details={
                                "expected": ordered["isbn13"],
                                "received": delivered["isbn13"],
                            },
                            now=now,
                        )
                    )
                else:
                    differences.append(
                        self._insert_difference(
                            school_id=school_id,
                            workspace_id=workspace_id,
                            order_revision_id=order_revision_id,
                            order_row_id=None,
                            kind="UNORDERED",
                            reference_key=f"delivery:unordered:{delivered['id']}",
                            details={
                                "isbn13": delivered["isbn13"],
                                "title": delivered["title"],
                            },
                            now=now,
                        )
                    )
                    continue
            received_by_order[ordered["id"]] += delivered["quantity"]
            if (
                delivered["unit_price"] is not None
                and delivered["unit_price"] != ordered["unit_price"]
            ):
                differences.append(
                    self._insert_difference(
                        school_id=school_id,
                        workspace_id=workspace_id,
                        order_revision_id=order_revision_id,
                        order_row_id=ordered["id"],
                        kind="UNIT_PRICE",
                        reference_key=f"delivery:price:{delivered['id']}",
                        details={
                            "expected": ordered["unit_price"],
                            "received": delivered["unit_price"],
                        },
                        now=now,
                    )
                )
            if (
                delivered["edition"]
                and ordered["edition"]
                and delivered["edition"] != ordered["edition"]
            ):
                differences.append(
                    self._insert_difference(
                        school_id=school_id,
                        workspace_id=workspace_id,
                        order_revision_id=order_revision_id,
                        order_row_id=ordered["id"],
                        kind="EDITION",
                        reference_key=f"delivery:edition:{delivered['id']}",
                        details={
                            "expected": ordered["edition"],
                            "received": delivered["edition"],
                        },
                        now=now,
                    )
                )
        for ordered in order_rows:
            received = received_by_order[ordered["id"]]
            if received == 0:
                differences.append(
                    self._insert_difference(
                        school_id=school_id,
                        workspace_id=workspace_id,
                        order_revision_id=order_revision_id,
                        order_row_id=ordered["id"],
                        kind="MISSING",
                        reference_key=f"delivery:missing:{ordered['id']}",
                        details={"expected": ordered["quantity"], "received": received},
                        now=now,
                    )
                )
            elif received < ordered["quantity"]:
                differences.append(
                    self._insert_difference(
                        school_id=school_id,
                        workspace_id=workspace_id,
                        order_revision_id=order_revision_id,
                        order_row_id=ordered["id"],
                        kind="QUANTITY",
                        reference_key=f"delivery:quantity:{ordered['id']}",
                        details={
                            "expected": ordered["quantity"],
                            "received": received,
                        },
                        now=now,
                    )
                )
            elif received > ordered["quantity"]:
                differences.append(
                    self._insert_difference(
                        school_id=school_id,
                        workspace_id=workspace_id,
                        order_revision_id=order_revision_id,
                        order_row_id=ordered["id"],
                        kind="OVER",
                        reference_key=f"delivery:over:{ordered['id']}",
                        details={"expected": ordered["quantity"], "received": received},
                        now=now,
                    )
                )
        return differences, sum(row["quantity"] for row in deliveries)

    def record_delivery(
        self,
        *,
        school_id: str,
        workspace_id: str,
        order_revision_id: str,
        actor_id: str,
        actor_roles: tuple[str, ...],
        workspace_version: int,
        rows: list[dict[str, Any]],
        reason: str,
        idempotency_key: str,
        request_id: str,
    ) -> dict[str, Any]:
        body = {
            "workspace_id": workspace_id,
            "order_revision_id": order_revision_id,
            "workspace_version": workspace_version,
            "rows": rows,
            "reason": reason,
        }

        def mutate() -> dict[str, Any]:
            self._authorize(school_id, actor_id, actor_roles)
            if not rows:
                raise ReceivingRuleError("EMPTY_DELIVERY")
            if not reason.strip():
                raise ReceivingRuleError("MODIFICATION_REASON_REQUIRED")
            workspace = self._workspace(school_id, workspace_id)
            self._order(school_id, workspace_id, order_revision_id)
            if workspace["status"] not in {"AWAITING_DELIVERY", "RECEIVING"}:
                raise ReceivingRuleError("DELIVERY_STATE_INVALID")
            delivery_number = self.connection.execute(
                """
                SELECT COALESCE(MAX(delivery_number), 0) + 1 AS n
                FROM delivery_batches WHERE workspace_id = ?
                """,
                (workspace_id,),
            ).fetchone()["n"]
            batch_id = str(uuid.uuid4())
            now = format_utc(utc_now())
            self.connection.execute(
                """
                INSERT INTO delivery_batches (
                    id, school_id, workspace_id, order_revision_id,
                    delivery_number, reason, created_by_user_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch_id,
                    school_id,
                    workspace_id,
                    order_revision_id,
                    delivery_number,
                    reason.strip(),
                    actor_id,
                    now,
                ),
            )
            for row in rows:
                quantity = row.get("quantity", 1)
                unit_price = row.get("unit_price")
                if (
                    isinstance(quantity, bool)
                    or not isinstance(quantity, int)
                    or quantity < 1
                ):
                    raise ReceivingRuleError("INVALID_DELIVERY_QUANTITY")
                if unit_price is not None and (
                    isinstance(unit_price, bool)
                    or not isinstance(unit_price, int)
                    or unit_price < 0
                ):
                    raise ReceivingRuleError("INVALID_DELIVERY_UNIT_PRICE")
                self.connection.execute(
                    """
                    INSERT INTO delivery_rows (
                        id, delivery_batch_id, isbn13, title, author,
                        edition, quantity, unit_price, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        batch_id,
                        canonical_isbn13(row.get("isbn")),
                        str(row.get("title") or ""),
                        row.get("author"),
                        row.get("edition"),
                        quantity,
                        unit_price,
                        now,
                    ),
                )
            self.connection.execute(
                "UPDATE delivery_batches SET sealed_at = ? WHERE id = ?",
                (now, batch_id),
            )
            differences, received_quantity = self._delivery_differences(
                school_id=school_id,
                workspace_id=workspace_id,
                order_revision_id=order_revision_id,
                now=now,
            )
            updated = update_with_version(
                self.connection,
                table="acquisition_workspaces",
                school_id=school_id,
                entity_id=workspace_id,
                submitted_version=workspace_version,
                changes={"status": "RECEIVING", "updated_at": now},
            )
            result = {
                "delivery_batch_id": batch_id,
                "delivery_number": delivery_number,
                "received_quantity": received_quantity,
                "differences": differences,
                "state": "RECEIVING",
                "row_version": updated["row_version"],
            }
            record_audit_event(
                self.connection,
                actor_id=actor_id,
                school_id=school_id,
                action="DELIVERY_RECORDED",
                entity_type="delivery_batch",
                entity_id=batch_id,
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
            route="deliveries.record",
            key=idempotency_key,
            request_body=body,
            operation=mutate,
        )

    def _refresh_scan_shortages(
        self,
        *,
        school_id: str,
        workspace_id: str,
        order_revision_id: str,
        now: str,
    ) -> None:
        order_rows = self.connection.execute(
            "SELECT * FROM order_rows WHERE order_revision_id = ?",
            (order_revision_id,),
        ).fetchall()
        for ordered in order_rows:
            scanned = self.connection.execute(
                """
                SELECT COUNT(*) AS n FROM scan_events
                WHERE order_row_id = ? AND result_code IN ('NORMAL', 'OVER')
                """,
                (ordered["id"],),
            ).fetchone()["n"]
            reference = f"scan:missing:{ordered['id']}"
            if scanned < ordered["quantity"]:
                self._insert_difference(
                    school_id=school_id,
                    workspace_id=workspace_id,
                    order_revision_id=order_revision_id,
                    order_row_id=ordered["id"],
                    kind="MISSING",
                    reference_key=reference,
                    details={"expected": ordered["quantity"], "scanned": scanned},
                    now=now,
                )
            else:
                self.connection.execute(
                    """
                    UPDATE receiving_differences SET active = 0, updated_at = ?
                    WHERE workspace_id = ? AND reference_key = ? AND active = 1
                    """,
                    (now, workspace_id, reference),
                )

    def start_scan_session(
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
            self._authorize(school_id, actor_id, actor_roles)
            if not reason.strip():
                raise ReceivingRuleError("MODIFICATION_REASON_REQUIRED")
            workspace = self._workspace(school_id, workspace_id)
            self._order(school_id, workspace_id, order_revision_id)
            if workspace["status"] not in {"AWAITING_DELIVERY", "RECEIVING"}:
                raise ReceivingRuleError("SCAN_STATE_INVALID")
            if (
                self.connection.execute(
                    "SELECT 1 FROM scan_sessions WHERE workspace_id = ? AND ended_at IS NULL",
                    (workspace_id,),
                ).fetchone()
                is not None
            ):
                raise ReceivingRuleError("ACTIVE_SCAN_SESSION_EXISTS")
            session_id = str(uuid.uuid4())
            now = format_utc(utc_now())
            self.connection.execute(
                """
                INSERT INTO scan_sessions (
                    id, school_id, workspace_id, order_revision_id, actor_id, started_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (session_id, school_id, workspace_id, order_revision_id, actor_id, now),
            )
            self._refresh_scan_shortages(
                school_id=school_id,
                workspace_id=workspace_id,
                order_revision_id=order_revision_id,
                now=now,
            )
            updated = update_with_version(
                self.connection,
                table="acquisition_workspaces",
                school_id=school_id,
                entity_id=workspace_id,
                submitted_version=workspace_version,
                changes={"status": "RECEIVING", "updated_at": now},
            )
            result = {
                "session_id": session_id,
                "state": "RECEIVING",
                "row_version": updated["row_version"],
            }
            record_audit_event(
                self.connection,
                actor_id=actor_id,
                school_id=school_id,
                action="SCAN_SESSION_STARTED",
                entity_type="scan_session",
                entity_id=session_id,
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
            route="scans.start",
            key=idempotency_key,
            request_body=body,
            operation=mutate,
        )

    def scan(
        self,
        *,
        school_id: str,
        workspace_id: str,
        session_id: str,
        actor_id: str,
        actor_roles: tuple[str, ...],
        isbn: str,
        idempotency_key: str,
        request_id: str,
        expected_order_row_id: str | None = None,
    ) -> dict[str, object]:
        body = {
            "workspace_id": workspace_id,
            "session_id": session_id,
            "isbn": isbn,
            "expected_order_row_id": expected_order_row_id,
        }

        def mutate() -> dict[str, object]:
            self._authorize(school_id, actor_id, actor_roles)
            session = self.connection.execute(
                """
                SELECT * FROM scan_sessions
                WHERE id = ? AND school_id = ? AND workspace_id = ? AND ended_at IS NULL
                """,
                (session_id, school_id, workspace_id),
            ).fetchone()
            if session is None:
                raise ReceivingRuleError("ACTIVE_SCAN_SESSION_NOT_FOUND")
            isbn13 = canonical_isbn13(isbn)
            ordered = None
            code = "NORMAL"
            if expected_order_row_id is not None:
                ordered = self.connection.execute(
                    """
                    SELECT * FROM order_rows
                    WHERE id = ? AND order_revision_id = ?
                    """,
                    (expected_order_row_id, session["order_revision_id"]),
                ).fetchone()
                if ordered is None:
                    raise ReceivingRuleError("ORDER_ROW_NOT_FOUND")
                if isbn13 != ordered["isbn13"]:
                    code = "EDITION_MISMATCH"
            if ordered is None:
                ordered = self.connection.execute(
                    """
                    SELECT * FROM order_rows
                    WHERE order_revision_id = ? AND isbn13 = ? LIMIT 1
                    """,
                    (session["order_revision_id"], isbn13),
                ).fetchone()
                code = "UNORDERED" if ordered is None else "NORMAL"
            current = 0
            if ordered is not None and code != "EDITION_MISMATCH":
                current = (
                    self.connection.execute(
                        """
                    SELECT COUNT(*) AS n FROM scan_events
                    WHERE order_row_id = ? AND result_code IN ('NORMAL', 'OVER')
                    """,
                        (ordered["id"],),
                    ).fetchone()["n"]
                    + 1
                )
                code = "NORMAL" if current <= ordered["quantity"] else "OVER"
            event_id = str(uuid.uuid4())
            now = format_utc(utc_now())
            self.connection.execute(
                """
                INSERT INTO scan_events (
                    id, session_id, school_id, workspace_id, actor_id,
                    order_row_id, isbn13, result_code, scanned_quantity,
                    idempotency_key, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    session_id,
                    school_id,
                    workspace_id,
                    actor_id,
                    ordered["id"] if ordered else None,
                    isbn13,
                    code,
                    current,
                    idempotency_key,
                    now,
                ),
            )
            if code in {"OVER", "UNORDERED", "EDITION_MISMATCH"}:
                kind = {
                    "OVER": "OVER",
                    "UNORDERED": "UNORDERED",
                    "EDITION_MISMATCH": "EDITION",
                }[code]
                self._insert_difference(
                    school_id=school_id,
                    workspace_id=workspace_id,
                    order_revision_id=session["order_revision_id"],
                    order_row_id=ordered["id"] if ordered else None,
                    kind=kind,
                    reference_key=f"scan:{code.casefold()}:{event_id}",
                    details={"isbn13": isbn13, "scanned_quantity": current},
                    now=now,
                )
            self._refresh_scan_shortages(
                school_id=school_id,
                workspace_id=workspace_id,
                order_revision_id=session["order_revision_id"],
                now=now,
            )
            result = {
                "event_id": event_id,
                "code": code,
                "isbn13": isbn13,
                "scanned_quantity": current,
            }
            record_audit_event(
                self.connection,
                actor_id=actor_id,
                school_id=school_id,
                action="ISBN_SCANNED",
                entity_type="scan_event",
                entity_id=event_id,
                before=None,
                after=result,
                request_id=request_id,
            )
            return result

        return idempotent_mutation(
            self.connection,
            school_id=school_id,
            actor_id=actor_id,
            route=f"scans.event:{session_id}",
            key=idempotency_key,
            request_body=body,
            operation=mutate,
        )

    def set_disposition(
        self,
        *,
        school_id: str,
        workspace_id: str,
        difference_id: str,
        actor_id: str,
        actor_roles: tuple[str, ...],
        submitted_version: int,
        disposition: str,
        reason: str,
        idempotency_key: str,
        request_id: str,
    ) -> dict[str, object]:
        body = {
            "workspace_id": workspace_id,
            "difference_id": difference_id,
            "submitted_version": submitted_version,
            "disposition": disposition,
            "reason": reason,
        }

        def mutate() -> dict[str, object]:
            self._authorize(school_id, actor_id, actor_roles)
            if disposition not in _DISPOSITIONS:
                raise ReceivingRuleError("INVALID_DISPOSITION")
            if not reason.strip():
                raise ReceivingRuleError("MODIFICATION_REASON_REQUIRED")
            before = self.connection.execute(
                """
                SELECT * FROM receiving_differences
                WHERE id = ? AND school_id = ? AND workspace_id = ? AND active = 1
                """,
                (difference_id, school_id, workspace_id),
            ).fetchone()
            if before is None:
                raise ReceivingRuleError("DIFFERENCE_NOT_FOUND")
            updated = update_with_version(
                self.connection,
                table="receiving_differences",
                school_id=school_id,
                entity_id=difference_id,
                submitted_version=submitted_version,
                changes={
                    "disposition": disposition,
                    "updated_at": format_utc(utc_now()),
                },
            )
            result = {
                "difference_id": difference_id,
                "disposition": disposition,
                "row_version": updated["row_version"],
            }
            record_audit_event(
                self.connection,
                actor_id=actor_id,
                school_id=school_id,
                action="RECEIVING_DISPOSITION_SET",
                entity_type="receiving_difference",
                entity_id=difference_id,
                before={
                    "disposition": before["disposition"],
                    "row_version": before["row_version"],
                },
                after={**result, "reason": reason.strip()},
                request_id=request_id,
            )
            return result

        return idempotent_mutation(
            self.connection,
            school_id=school_id,
            actor_id=actor_id,
            route="receiving.disposition",
            key=idempotency_key,
            request_body=body,
            operation=mutate,
        )

    def complete(
        self,
        *,
        school_id: str,
        workspace_id: str,
        actor_id: str,
        actor_roles: tuple[str, ...],
        workspace_version: int,
        reason: str,
        idempotency_key: str,
        request_id: str,
    ) -> dict[str, object]:
        body = {
            "workspace_id": workspace_id,
            "workspace_version": workspace_version,
            "reason": reason,
        }

        def mutate() -> dict[str, object]:
            self._authorize(school_id, actor_id, actor_roles)
            if not reason.strip():
                raise ReceivingRuleError("MODIFICATION_REASON_REQUIRED")
            workspace = self._workspace(school_id, workspace_id)
            if workspace["status"] != "RECEIVING":
                raise ReceivingRuleError("RECEIVING_STATE_INVALID")
            unresolved = self.connection.execute(
                """
                SELECT COUNT(*) AS n FROM receiving_differences
                WHERE school_id = ? AND workspace_id = ?
                  AND active = 1 AND disposition IS NULL
                """,
                (school_id, workspace_id),
            ).fetchone()["n"]
            if unresolved:
                raise ReceivingRuleError("RECEIVING_INCOMPLETE")
            now = format_utc(utc_now())
            self.connection.execute(
                """
                UPDATE scan_sessions
                SET ended_at = ?, row_version = row_version + 1
                WHERE school_id = ? AND workspace_id = ? AND ended_at IS NULL
                """,
                (now, school_id, workspace_id),
            )
            updated = update_with_version(
                self.connection,
                table="acquisition_workspaces",
                school_id=school_id,
                entity_id=workspace_id,
                submitted_version=workspace_version,
                changes={"status": "COMPLETE", "updated_at": now},
            )
            result = {"state": "COMPLETE", "row_version": updated["row_version"]}
            record_audit_event(
                self.connection,
                actor_id=actor_id,
                school_id=school_id,
                action="RECEIVING_COMPLETED",
                entity_type="acquisition_workspace",
                entity_id=workspace_id,
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
            route="receiving.complete",
            key=idempotency_key,
            request_body=body,
            operation=mutate,
        )
