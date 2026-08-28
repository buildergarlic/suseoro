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
    mapping: dict[str, str | None] | None = None,
) -> str:
    """Fingerprint stable required semantics, independent of order/optional additions."""
    semantic_identities: list[str] = []
    present_fields: set[str] = set()
    for header in headers:
        semantic = mapping.get(header) if mapping is not None else None
        inferred = canonical_field_for_header(header)
        semantic = semantic or inferred
        if semantic not in required_fields:
            continue
        present_fields.add(semantic)
        semantic_identities.append(
            semantic
            if inferred == semantic
            else f"{semantic}:custom:{normalize_header(header)}"
        )
    required_presence = [
        f"{field}:{'present' if field in present_fields else 'missing'}"
        for field in sorted(required_fields)
    ]
    payload = {
        "role": role.value,
        "required": required_presence,
        "semantic_headers": sorted(semantic_identities),
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
            mapping=mapping,
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
                json.dumps(
                    {
                        normalize_header(header): mapping.get(header)
                        for header in headers
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
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
        rows = self.connection.execute(
            """
            SELECT * FROM mapping_templates
            WHERE school_id = ? AND vendor_scope = ? AND role = ?
            """,
            (school_id, scope, role.value),
        ).fetchall()
        compatible: list[tuple[sqlite3.Row, dict[str, str | None]]] = []
        for row in rows:
            if set(json.loads(row["required_fields_json"])) != required_fields:
                continue
            stored_mapping = json.loads(row["mapping_json"])
            stored_semantics = {
                semantic for semantic in stored_mapping.values() if semantic is not None
            }
            stored_by_identity = {
                normalize_header(header): semantic
                for header, semantic in stored_mapping.items()
            }
            applied_mapping: dict[str, str | None] = {}
            for header in headers:
                semantic = canonical_field_for_header(header)
                if semantic in stored_semantics:
                    applied_mapping[header] = semantic
                else:
                    applied_mapping[header] = stored_by_identity.get(
                        normalize_header(header)
                    )
            if required_fields <= {
                semantic
                for semantic in applied_mapping.values()
                if semantic is not None
            }:
                compatible.append((row, applied_mapping))
        if len(compatible) != 1:
            return None
        row, applied_mapping = compatible[0]
        return MappingTemplate(
            id=row["id"],
            school_id=row["school_id"],
            vendor_scope=row["vendor_scope"],
            role=DocumentRole(row["role"]),
            signature=row["signature"],
            mapping=applied_mapping,
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
        retry_failed: bool = True,
        claim_token: str | None = None,
        claim_generation: int = 1,
    ) -> CachedParseResult:
        if not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ValueError("sha256 must be 64 lowercase hexadecimal characters")
        if not parser_version.strip():
            raise ValueError("parser_version is required")
        source = self.connection.execute(
            "SELECT id FROM source_files WHERE sha256 = ?", (sha256,)
        ).fetchone()
        if source is None:
            raise ValueError("source file must exist before parser cache reservation")
        token = claim_token or str(uuid.uuid4())
        if claim_generation < 1:
            raise ValueError("claim_generation must be positive")
        claimed_pending = False
        row = self.connection.execute(
            """
            SELECT status, result_json, error_json, claim_token, claim_generation
            FROM parser_runs
            WHERE source_file_sha256 = ? AND parser_version = ? AND role = ?
            """,
            (sha256, parser_version, role.value),
        ).fetchone()
        if row is not None:
            if row["status"] == "SUCCESS":
                return CachedParseResult(json.loads(row["result_json"]), True)
            if row["status"] == "PENDING":
                takeover = self.connection.execute(
                    """
                    UPDATE parser_runs SET claim_token = ?, claim_generation = ?
                    WHERE source_file_sha256 = ? AND parser_version = ? AND role = ?
                      AND status = 'PENDING' AND claim_generation < ?
                    """,
                    (
                        token,
                        claim_generation,
                        sha256,
                        parser_version,
                        role.value,
                        claim_generation,
                    ),
                )
                if takeover.rowcount != 1:
                    raise ParserCacheInProgress(
                        "parser result is currently being produced"
                    )
                claimed_pending = True
            if row["status"] == "FAILED" and not retry_failed:
                raise ParserCacheFailed(row["error_json"] or "parser run failed")

        run_id = str(uuid.uuid4())
        now = format_utc(utc_now())
        try:
            if claimed_pending:
                claim = None
            elif row is not None and row["status"] == "FAILED":
                claim = self.connection.execute(
                    """
                    UPDATE parser_runs
                    SET status = 'PENDING', result_json = NULL, error_json = NULL,
                        completed_at = NULL, claim_token = ?,
                        claim_generation = ?
                    WHERE source_file_sha256 = ? AND parser_version = ? AND role = ?
                      AND status = 'FAILED'
                    """,
                    (token, claim_generation, sha256, parser_version, role.value),
                )
            else:
                claim = self.connection.execute(
                    """
                    INSERT OR IGNORE INTO parser_runs (
                        id, source_file_sha256, parser_version, role, status,
                        claim_token, claim_generation, created_at
                    ) VALUES (?, ?, ?, ?, 'PENDING', ?, ?, ?)
                    """,
                    (
                        run_id,
                        sha256,
                        parser_version,
                        role.value,
                        token,
                        claim_generation,
                        now,
                    ),
                )
        except sqlite3.OperationalError as error:
            if "locked" in str(error).casefold():
                raise ParserCacheInProgress(
                    "parser result is currently being produced"
                ) from error
            raise
        if claim is not None and claim.rowcount != 1:
            current = self.connection.execute(
                """
                SELECT status, result_json, error_json FROM parser_runs
                WHERE source_file_sha256 = ? AND parser_version = ? AND role = ?
                """,
                (sha256, parser_version, role.value),
            ).fetchone()
            if current is not None and current["status"] == "SUCCESS":
                return CachedParseResult(json.loads(current["result_json"]), True)
            if current is not None and current["status"] == "FAILED":
                raise ParserCacheFailed(current["error_json"] or "parser run failed")
            raise ParserCacheInProgress("parser result is currently being produced")

        try:
            result = parse()
            encoded = json.dumps(
                result, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
        except Exception as error:
            completed = format_utc(utc_now())
            self.connection.execute(
                """
                UPDATE parser_runs
                SET status = 'FAILED', error_json = ?, completed_at = ?
                WHERE source_file_sha256 = ? AND parser_version = ? AND role = ?
                  AND status = 'PENDING' AND claim_token = ?
                  AND claim_generation = ?
                """,
                (
                    json.dumps(
                        {"type": type(error).__name__, "message": str(error)},
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    completed,
                    sha256,
                    parser_version,
                    role.value,
                    token,
                    claim_generation,
                ),
            )
            raise
        completed = format_utc(utc_now())
        updated = self.connection.execute(
            """
            UPDATE parser_runs
            SET status = 'SUCCESS', result_json = ?, error_json = NULL,
                completed_at = ?
            WHERE source_file_sha256 = ? AND parser_version = ? AND role = ?
              AND status = 'PENDING' AND claim_token = ?
              AND claim_generation = ?
            """,
            (
                encoded,
                completed,
                sha256,
                parser_version,
                role.value,
                token,
                claim_generation,
            ),
        )
        if updated.rowcount != 1:
            raise RuntimeError("parser cache reservation was lost")
        return CachedParseResult(result, False)


class ParserCacheInProgress(RuntimeError):
    pass


class ParserCacheFailed(RuntimeError):
    pass
