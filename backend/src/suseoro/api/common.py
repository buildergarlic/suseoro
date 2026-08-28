"""Small shared API serialization and cursor helpers."""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any

from fastapi import HTTPException


def encode_cursor(*values: object) -> str:
    raw = json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(value: str | None, size: int) -> tuple[Any, ...] | None:
    if value is None:
        return None
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded).decode())
        if not isinstance(decoded, list) or len(decoded) != size:
            raise ValueError
        return tuple(decoded)
    except (ValueError, UnicodeError, json.JSONDecodeError, binascii.Error) as error:
        raise HTTPException(
            status_code=400, detail={"code": "INVALID_CURSOR"}
        ) from error


def page(items: list[dict[str, Any]], *, limit: int, cursor_values) -> dict[str, Any]:
    has_more = len(items) > limit
    visible = items[:limit]
    next_cursor = (
        encode_cursor(*cursor_values(visible[-1])) if has_more and visible else None
    )
    return {"items": visible, "next_cursor": next_cursor}
