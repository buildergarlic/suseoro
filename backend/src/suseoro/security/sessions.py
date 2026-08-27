"""Opaque session-token generation, digesting, and UTC time helpers."""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime

SESSION_COOKIE_NAME = "suseoro_session"
CSRF_COOKIE_NAME = "suseoro_csrf"
CSRF_HEADER_NAME = "X-CSRF-Token"


def generate_token() -> str:
    """Generate an opaque token with 256 bits of entropy."""
    return secrets.token_urlsafe(32)


def digest_token(token: str) -> str:
    """Return the storage-safe SHA-256 digest of a bearer token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def utc_now() -> datetime:
    return datetime.now(UTC)


def format_utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("UTC timestamp must be timezone-aware")
    return (
        value.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)
