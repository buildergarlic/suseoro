"""Session-bound double-submit CSRF validation."""

from __future__ import annotations

import hmac

from suseoro.security.sessions import digest_token


def validate_csrf_token(
    cookie_token: str | None, header_token: str | None, digest: str
) -> bool:
    """Require matching cookie/header values whose digest belongs to the session."""
    if not cookie_token or not header_token:
        return False
    return hmac.compare_digest(cookie_token, header_token) and hmac.compare_digest(
        digest_token(header_token), digest
    )
