"""Durable request idempotency reservations and response replay."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException

from suseoro.security.sessions import format_utc, utc_now


@dataclass(frozen=True)
class StoredResponse:
    status: int
    body: Any


class IdempotencyConflict(HTTPException):
    def __init__(self, code: str = "IDEMPOTENCY_KEY_REUSED") -> None:
        super().__init__(status_code=409, detail={"code": code})


def request_hash(request_body: Any) -> str:
    encoded = json.dumps(
        request_body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def reserve_idempotency_key(
    connection: sqlite3.Connection,
    *,
    school_id: str,
    actor_id: str,
    route: str,
    key: str,
    request_body: Any,
) -> StoredResponse | None:
    """Reserve a scoped key, replay its result, or reject conflicting reuse."""
    digest = request_hash(request_body)
    row = connection.execute(
        """
        SELECT request_hash, response_status, response_body
        FROM idempotency_keys
        WHERE school_id = ? AND actor_id = ? AND route = ? AND key = ?
        """,
        (school_id, actor_id, route, key),
    ).fetchone()
    if row is None:
        try:
            connection.execute(
                """
                INSERT INTO idempotency_keys (
                    id, school_id, actor_id, route, key, request_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    school_id,
                    actor_id,
                    route,
                    key,
                    digest,
                    format_utc(utc_now()),
                ),
            )
            return None
        except sqlite3.IntegrityError:
            row = connection.execute(
                """
                SELECT request_hash, response_status, response_body
                FROM idempotency_keys
                WHERE school_id = ? AND actor_id = ? AND route = ? AND key = ?
                """,
                (school_id, actor_id, route, key),
            ).fetchone()
            if row is None:
                raise
    if row["request_hash"] != digest:
        raise IdempotencyConflict()
    if row["response_status"] is None:
        raise IdempotencyConflict("IDEMPOTENCY_REQUEST_IN_PROGRESS")
    return StoredResponse(row["response_status"], json.loads(row["response_body"]))


def complete_idempotent_request(
    connection: sqlite3.Connection,
    *,
    school_id: str,
    actor_id: str,
    route: str,
    key: str,
    status: int,
    body: Any,
) -> None:
    result = connection.execute(
        """
        UPDATE idempotency_keys
        SET response_status = ?, response_body = ?
        WHERE school_id = ? AND actor_id = ? AND route = ? AND key = ?
          AND response_status IS NULL
        """,
        (
            status,
            json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            school_id,
            actor_id,
            route,
            key,
        ),
    )
    if result.rowcount != 1:
        raise RuntimeError("idempotency key is absent or already completed")
