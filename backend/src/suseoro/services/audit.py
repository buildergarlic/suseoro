"""Transaction-scoped audit event recording."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from suseoro.security.sessions import format_utc, utc_now

_SENSITIVE_MARKERS = (
    "password",
    "token",
    "secret",
    "authorization",
    "cookie",
    "api_key",
    "credential",
)


def _is_sensitive(key: object) -> bool:
    normalized = str(key).lower().replace("-", "_")
    return any(marker in normalized for marker in _SENSITIVE_MARKERS)


def _scrub(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _scrub(item) for key, item in value.items() if not _is_sensitive(key)}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_scrub(item) for item in value]
    return value


def _json(value: Any | None) -> str | None:
    if value is None:
        return None
    return json.dumps(_scrub(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def record_audit_event(
    connection: sqlite3.Connection,
    *,
    actor_id: str | None,
    school_id: str | None,
    action: str,
    entity_type: str,
    entity_id: str | None,
    before: Any | None,
    after: Any | None,
    request_id: str | None,
) -> str:
    """Insert a scrubbed audit event without committing the caller's transaction."""
    event_id = str(uuid.uuid4())
    connection.execute(
        """
        INSERT INTO audit_events (
            id, school_id, actor_id, action, entity_type, entity_id,
            before_json, after_json, request_id, occurred_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            school_id,
            actor_id,
            action,
            entity_type,
            entity_id,
            _json(before),
            _json(after),
            request_id,
            format_utc(utc_now()),
        ),
    )
    return event_id
