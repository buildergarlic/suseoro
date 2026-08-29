"""Canonical comparison-source snapshots shared by enqueue and execution."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any


def authoritative_comparison_sources(
    connection: sqlite3.Connection,
    *,
    school_id: str,
    workspace_id: str,
) -> list[dict[str, Any]]:
    """Return every current purchase source and its immutable parse snapshot."""
    rows = connection.execute(
        """
        SELECT document.id, document.status, document.parser_version,
               document.completed_at, document.parsed_config_version,
               file.sha256,
               COALESCE(config.role, document.role) AS role,
               COALESCE(config.row_version, 1) AS config_version,
               COALESCE(config.mapping_json, '{}') AS mapping_json
        FROM source_documents AS document
        JOIN source_files AS file ON file.id = document.source_file_id
        JOIN workspace_sources AS link ON link.source_document_id = document.id
        LEFT JOIN source_configurations AS config
          ON config.source_document_id = document.id
        WHERE link.workspace_id = ? AND link.school_id = ?
          AND COALESCE(config.role, document.role) = 'PURCHASE_REQUEST'
        ORDER BY link.created_at, document.id
        """,
        (workspace_id, school_id),
    ).fetchall()
    snapshot: list[dict[str, Any]] = []
    for row in rows:
        row_count, row_digest = source_rows_snapshot(connection, row["id"])
        snapshot.append(
            {
                "id": row["id"],
                "role": row["role"],
                "status": row["status"],
                "sha256": row["sha256"],
                "config_version": row["config_version"],
                "parsed_config_version": row["parsed_config_version"],
                "mapping_json": row["mapping_json"],
                "parser_version": row["parser_version"],
                "completed_at": row["completed_at"],
                "row_count": row_count,
                "row_digest": row_digest,
            }
        )
    return snapshot


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
