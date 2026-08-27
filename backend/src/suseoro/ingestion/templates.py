"""Scoped mapping-template persistence and durable parse-result caching."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from suseoro.ingestion.contracts import CachedParseResult, DocumentRole
from suseoro.ingestion.mapping import canonical_field_for_header, normalize_header
from suseoro.security.sessions import format_utc, utc_now


def template_signature(
    headers: list[str],
    role: DocumentRole,
    *,
    required_fields: set[str],
    school_id: str | None = None,
    vendor_scope: str | None = None,
) -> str:
    """Fingerprint stable required semantics, independent of order/optional additions."""
    semantic_headers = {
        canonical_field_for_header(header) or f"unknown:{normalize_header(header)}"
        for header in headers
    }
    required_presence = [
        f"{field}:{'present' if field in semantic_headers else 'missing'}"
        for field in sorted(required_fields)
    ]
    payload = {
        "role": role.value,
        "required": required_presence,
        "school": school_id or "*",
        "vendor": vendor_scope or "*",
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class MappingTemplate:
    id: str
    school_id: str
    vendor_scope: str
    role: DocumentRole
    signature: str
    mapping: dict[str, str | None]
    required_fields: set[str]
    template_version: str


class MappingTemplateStore:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def save(
        self,
        *,
        school_id: str,
        vendor_scope: str | None,
        role: DocumentRole,
        headers: list[str],
        mapping: dict[str, str | None],
        required_fields: set[str],
        template_version: str,
    ) -> str:
        scope = vendor_scope or "*"
        signature = template_signature(
            headers,
            role,
            required_fields=required_fields,
            school_id=school_id,
            vendor_scope=scope,
        )
        existing = self.connection.execute(
            """
            SELECT id FROM mapping_templates
            WHERE school_id = ? AND vendor_scope = ? AND role = ? AND signature = ?
            """,
            (school_id, scope, role.value, signature),
        ).fetchone()
        if existing:
            return existing["id"]
        template_id = str(uuid.uuid4())
        now = format_utc(utc_now())
        self.connection.execute(
            """
            INSERT INTO mapping_templates (
                id, school_id, vendor_scope, role, signature,
                normalized_headers_json, mapping_json, required_fields_json,
                template_version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                template_id,
                school_id,
                scope,
                role.value,
                signature,
                json.dumps(sorted(normalize_header(header) for header in headers)),
                json.dumps(mapping, ensure_ascii=False, sort_keys=True),
                json.dumps(sorted(required_fields)),
                template_version,
                now,
                now,
            ),
        )
        return template_id

    def find(
        self,
        *,
        school_id: str,
        vendor_scope: str | None,
        role: DocumentRole,
        headers: list[str],
        required_fields: set[str],
    ) -> MappingTemplate | None:
        scope = vendor_scope or "*"
        signature = template_signature(
            headers,
            role,
            required_fields=required_fields,
            school_id=school_id,
            vendor_scope=scope,
        )
        row = self.connection.execute(
            """
            SELECT * FROM mapping_templates
            WHERE school_id = ? AND vendor_scope = ? AND role = ? AND signature = ?
            """,
            (school_id, scope, role.value, signature),
        ).fetchone()
        if row is None:
            return None
        return MappingTemplate(
            id=row["id"],
            school_id=row["school_id"],
            vendor_scope=row["vendor_scope"],
            role=DocumentRole(row["role"]),
            signature=row["signature"],
            mapping=json.loads(row["mapping_json"]),
            required_fields=set(json.loads(row["required_fields_json"])),
            template_version=row["template_version"],
        )


class ParserCache:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def get_or_parse(
        self,
        *,
        sha256: str,
        parser_version: str,
        role: DocumentRole,
        parse: Callable[[], Any],
    ) -> CachedParseResult:
        if not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ValueError("sha256 must be 64 lowercase hexadecimal characters")
        if not parser_version.strip():
            raise ValueError("parser_version is required")
        row = self.connection.execute(
            """
            SELECT result_json FROM parser_runs
            WHERE source_file_sha256 = ? AND parser_version = ? AND role = ?
              AND status = 'SUCCESS'
            """,
            (sha256, parser_version, role.value),
        ).fetchone()
        if row is not None:
            return CachedParseResult(json.loads(row["result_json"]), True)

        result = parse()
        encoded = json.dumps(
            result, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        run_id = str(uuid.uuid4())
        now = format_utc(utc_now())
        self.connection.execute(
            """
            INSERT OR IGNORE INTO parser_runs (
                id, source_file_sha256, parser_version, role, status,
                result_json, created_at, completed_at
            ) VALUES (?, ?, ?, ?, 'SUCCESS', ?, ?, ?)
            """,
            (run_id, sha256, parser_version, role.value, encoded, now, now),
        )
        stored = self.connection.execute(
            """
            SELECT id, result_json FROM parser_runs
            WHERE source_file_sha256 = ? AND parser_version = ? AND role = ?
              AND status = 'SUCCESS'
            """,
            (sha256, parser_version, role.value),
        ).fetchone()
        if stored is None:
            raise RuntimeError("parser cache write failed")
        if stored["id"] != run_id:
            return CachedParseResult(json.loads(stored["result_json"]), True)
        return CachedParseResult(result, False)
