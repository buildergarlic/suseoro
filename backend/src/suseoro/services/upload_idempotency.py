"""Durable, generation-fenced idempotency claims for streamed uploads."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from suseoro.db.connection import connect
from suseoro.security.sessions import format_utc, parse_utc, utc_now
from suseoro.services.idempotency import (
    IdempotencyConflict,
    StoredResponse,
    request_hash,
)

_BUSY_TIMEOUT_MILLISECONDS = 25
_INITIAL_BACKOFF_SECONDS = 0.01
_MAX_BACKOFF_SECONDS = 0.1
_RETRY_HEADERS = {"Retry-After": "1"}


@dataclass(frozen=True)
class UploadClaimLease:
    id: str
    request_fingerprint: str
    generation: int
    owner: str


def _is_locked(error: sqlite3.OperationalError) -> bool:
    return "locked" in str(error).casefold() or "busy" in str(error).casefold()


def _claim_row(connection: sqlite3.Connection, scope: dict[str, str]):
    return connection.execute(
        """
        SELECT * FROM upload_idempotency_claims
        WHERE school_id = ? AND actor_id = ? AND route = ? AND key = ?
        """,
        (scope["school_id"], scope["actor_id"], scope["route"], scope["key"]),
    ).fetchone()


def _active_lease(row, now) -> bool:
    return bool(
        row["lease_owner"]
        and row["lease_expires_at"]
        and parse_utc(row["lease_expires_at"]) > now
    )


def _replay_or_conflict(row, fingerprint: str) -> StoredResponse | None:
    if row["request_fingerprint"] != fingerprint:
        raise IdempotencyConflict()
    if row["state"] == "COMPLETED":
        return StoredResponse(
            status=int(row["response_status"]),
            body=json.loads(row["response_body"]),
        )
    return None


def _retry_or_raise(
    *, deadline: float, delay: float, matching_in_progress: bool
) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        if matching_in_progress:
            raise IdempotencyConflict(
                "IDEMPOTENCY_REQUEST_IN_PROGRESS", headers=_RETRY_HEADERS
            )
        raise HTTPException(
            status_code=503,
            detail={"code": "UPLOAD_RESERVATION_BUSY"},
            headers=_RETRY_HEADERS,
        )
    time.sleep(min(delay, remaining))
    return min(delay * 2, _MAX_BACKOFF_SECONDS)


def acquire_upload_claim(
    database_path: Path,
    *,
    school_id: str,
    actor_id: str,
    route: str,
    key: str,
    request_body: Any,
    wait_deadline_seconds: float,
    lease_seconds: float,
) -> UploadClaimLease | StoredResponse:
    """Bind a key permanently, then acquire or replay its fenced generation."""
    fingerprint = request_hash(request_body)
    owner = str(uuid.uuid4())
    scope = {
        "school_id": school_id,
        "actor_id": actor_id,
        "route": route,
        "key": key,
    }
    deadline = time.monotonic() + max(0.0, wait_deadline_seconds)
    delay = _INITIAL_BACKOFF_SECONDS
    matching_in_progress = False

    while True:
        connection: sqlite3.Connection | None = None
        try:
            connection = connect(database_path)
            connection.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MILLISECONDS}")
            observed = _claim_row(connection, scope)
            now = utc_now()
            if observed is not None:
                replay = _replay_or_conflict(observed, fingerprint)
                if replay is not None:
                    return replay
                if _active_lease(observed, now):
                    matching_in_progress = True
                else:
                    connection.execute("BEGIN IMMEDIATE")
                    current = _claim_row(connection, scope)
                    if current is None:
                        connection.rollback()
                        continue
                    replay = _replay_or_conflict(current, fingerprint)
                    if replay is not None:
                        connection.rollback()
                        return replay
                    now = utc_now()
                    if _active_lease(current, now):
                        matching_in_progress = True
                        connection.rollback()
                    else:
                        generation = int(current["generation"]) + 1
                        expires_at = format_utc(now + timedelta(seconds=lease_seconds))
                        updated = connection.execute(
                            """
                            UPDATE upload_idempotency_claims
                            SET generation = ?, lease_owner = ?, lease_expires_at = ?,
                                updated_at = ?
                            WHERE id = ? AND generation = ? AND state = 'IN_PROGRESS'
                              AND request_fingerprint = ?
                            """,
                            (
                                generation,
                                owner,
                                expires_at,
                                format_utc(now),
                                current["id"],
                                current["generation"],
                                fingerprint,
                            ),
                        )
                        if updated.rowcount == 1:
                            connection.commit()
                            return UploadClaimLease(
                                id=current["id"],
                                request_fingerprint=fingerprint,
                                generation=generation,
                                owner=owner,
                            )
                        connection.rollback()
            else:
                connection.execute("BEGIN IMMEDIATE")
                current = _claim_row(connection, scope)
                if current is not None:
                    connection.rollback()
                    continue
                now = utc_now()
                claim_id = str(uuid.uuid4())
                connection.execute(
                    """
                    INSERT INTO upload_idempotency_claims (
                        id, school_id, actor_id, route, key, request_fingerprint,
                        generation, lease_owner, lease_expires_at, state,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, 'IN_PROGRESS', ?, ?)
                    """,
                    (
                        claim_id,
                        school_id,
                        actor_id,
                        route,
                        key,
                        fingerprint,
                        owner,
                        format_utc(now + timedelta(seconds=lease_seconds)),
                        format_utc(now),
                        format_utc(now),
                    ),
                )
                connection.commit()
                return UploadClaimLease(
                    id=claim_id,
                    request_fingerprint=fingerprint,
                    generation=1,
                    owner=owner,
                )
        except sqlite3.OperationalError as error:
            if connection is not None and connection.in_transaction:
                connection.rollback()
            if not _is_locked(error):
                raise
        finally:
            if connection is not None:
                connection.close()

        delay = _retry_or_raise(
            deadline=deadline,
            delay=delay,
            matching_in_progress=matching_in_progress,
        )


def begin_upload_mutation(
    connection: sqlite3.Connection,
    lease: UploadClaimLease,
    *,
    wait_deadline_seconds: float,
) -> None:
    """Acquire the mutation writer lock and verify the current claim generation."""
    deadline = time.monotonic() + max(0.0, wait_deadline_seconds)
    delay = _INITIAL_BACKOFF_SECONDS
    connection.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MILLISECONDS}")
    while True:
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM upload_idempotency_claims WHERE id = ?", (lease.id,)
            ).fetchone()
            if (
                row is None
                or row["state"] != "IN_PROGRESS"
                or row["request_fingerprint"] != lease.request_fingerprint
                or int(row["generation"]) != lease.generation
                or row["lease_owner"] != lease.owner
            ):
                connection.rollback()
                raise IdempotencyConflict(
                    "IDEMPOTENCY_REQUEST_IN_PROGRESS", headers=_RETRY_HEADERS
                )
            return
        except sqlite3.OperationalError as error:
            if connection.in_transaction:
                connection.rollback()
            if not _is_locked(error):
                raise
        delay = _retry_or_raise(
            deadline=deadline,
            delay=delay,
            matching_in_progress=False,
        )


def complete_upload_claim(
    connection: sqlite3.Connection,
    lease: UploadClaimLease,
    *,
    status: int,
    body: Any,
) -> None:
    """Store the terminal response only for the still-current generation."""
    completed = connection.execute(
        """
        UPDATE upload_idempotency_claims
        SET state = 'COMPLETED', response_status = ?, response_body = ?,
            lease_owner = NULL, lease_expires_at = NULL, updated_at = ?
        WHERE id = ? AND request_fingerprint = ? AND generation = ?
          AND lease_owner = ? AND state = 'IN_PROGRESS'
        """,
        (
            status,
            json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            format_utc(utc_now()),
            lease.id,
            lease.request_fingerprint,
            lease.generation,
            lease.owner,
        ),
    )
    if completed.rowcount != 1:
        raise IdempotencyConflict(
            "IDEMPOTENCY_REQUEST_IN_PROGRESS", headers=_RETRY_HEADERS
        )


def cleanup_and_release_upload_claim(
    database_path: Path,
    lease: UploadClaimLease,
    paths: list[Path],
) -> None:
    """Fence cleanup with the claim generation, then release only that generation."""
    connection = connect(database_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = connection.execute(
            """
            SELECT generation, lease_owner, state
            FROM upload_idempotency_claims WHERE id = ?
            """,
            (lease.id,),
        ).fetchone()
        if (
            current is None
            or current["state"] != "IN_PROGRESS"
            or int(current["generation"]) != lease.generation
            or current["lease_owner"] != lease.owner
        ):
            connection.rollback()
            return
        for path in paths:
            referenced = connection.execute(
                "SELECT 1 FROM source_files WHERE storage_path = ?", (str(path),)
            ).fetchone()
            if referenced is None:
                path.unlink(missing_ok=True)
        connection.execute(
            """
            UPDATE upload_idempotency_claims
            SET lease_owner = NULL, lease_expires_at = NULL, updated_at = ?
            WHERE id = ? AND generation = ? AND lease_owner = ?
              AND state = 'IN_PROGRESS'
            """,
            (format_utc(utc_now()), lease.id, lease.generation, lease.owner),
        )
        connection.commit()
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()
