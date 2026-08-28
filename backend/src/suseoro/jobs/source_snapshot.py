"""Canonical comparison-source snapshots shared by enqueue and execution."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any


def source_rows_snapshot(
    connection: sqlite3.Connection, source_document_id: str
) -> tuple[int, str]:
    """Return a stable count/digest over every persisted logical source row."""
    rows = connection.execute(
        """
        SELECT id, source_row, status, raw_json, fields_json,
               error_code, error_message
        FROM source_rows
        WHERE source_document_id = ?
        ORDER BY source_row, id
        """,
        (source_document_id,),
    ).fetchall()
    canonical: list[dict[str, Any]] = [
        {
            "id": row["id"],
            "source_row": row["source_row"],
            "status": row["status"],
            "raw_json": row["raw_json"],
            "fields_json": row["fields_json"],
            "error_code": row["error_code"],
            "error_message": row["error_message"],
        }
        for row in rows
    ]
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return len(rows), hashlib.sha256(encoded).hexdigest()
