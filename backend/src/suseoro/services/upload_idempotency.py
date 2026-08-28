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
_UNKNOWN_REJECTED_REPLAY_MAX_BYTES = 100 * 1024 * 1024


@dataclass(frozen=True)
class UploadClaimLease:
    id: str
    request_fingerprint: str
    generation: int
    owner: str


@dataclass(frozen=True)
class UploadReplayAllowance:
    """Bounded stream limits for probing one known terminal upload shape."""

    filenames: tuple[str, ...]
    file_max_bytes: tuple[int, ...]
    preserve_too_large: tuple[bool, ...]
    max_batch_bytes: int

    def matches(self, filenames: list[str]) -> bool:
        return tuple(filenames) == self.filenames

    def stage_limit(self, index: int, current_limit: int) -> int:
        if self.preserve_too_large[index]:
            return current_limit
        return max(current_limit, self.file_max_bytes[index])


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


def upload_scope_replay_allowance(
    database_path: Path,
    *,
    school_id: str,
    actor_id: str,
    route: str,
    key: str,
) -> UploadReplayAllowance | None:
    """Return only the historical shape and byte bounds needed for exact replay."""
    connection = connect(database_path)
    try:
        claim = connection.execute(
            """
            SELECT state, response_body, request_metadata_json
            FROM upload_idempotency_claims
            WHERE school_id = ? AND actor_id = ? AND route = ? AND key = ?
            """,
            (school_id, actor_id, route, key),
        ).fetchone()
        response_body: Any = None
        metadata: Any = None
        if claim is not None and claim["state"] == "COMPLETED":
            response_body = json.loads(claim["response_body"])
            if claim["request_metadata_json"]:
                metadata = json.loads(claim["request_metadata_json"])
        else:
            legacy = connection.execute(
                """
                SELECT response_status, response_body FROM idempotency_keys
                WHERE school_id = ? AND actor_id = ? AND route = ? AND key = ?
                """,
                (school_id, actor_id, route, key),
            ).fetchone()
            if (
                legacy is None
                or legacy["response_status"] is None
                or not legacy["response_body"]
            ):
                return None
            response_body = json.loads(legacy["response_body"])

        metadata_files = metadata.get("files") if isinstance(metadata, dict) else None
        if isinstance(metadata_files, list) and metadata_files:
            filenames: list[str] = []
            sizes: list[int] = []
            for item in metadata_files:
                if not isinstance(item, dict) or not isinstance(
                    item.get("filename"), str
                ):
                    return None
                size = item.get("size_bytes")
                if not isinstance(size, int) or size < 0:
                    return None
                filenames.append(item["filename"])
                sizes.append(size)
            return UploadReplayAllowance(
                filenames=tuple(filenames),
                file_max_bytes=tuple(sizes),
                preserve_too_large=tuple(False for _ in sizes),
                max_batch_bytes=sum(sizes),
            )

        stored_items = (
            response_body.get("items") if isinstance(response_body, dict) else None
        )
        if not isinstance(stored_items, list) or not stored_items:
            return None
        filenames = []
        sizes = []
        preserve_too_large = []
        for item in stored_items:
            if not isinstance(item, dict) or not isinstance(item.get("filename"), str):
                return None
            filenames.append(item["filename"])
            source_id = item.get("source_id")
            size_row = None
            if isinstance(source_id, str):
                size_row = connection.execute(
                    """
                    SELECT file.size_bytes
                    FROM source_documents AS source
                    JOIN source_files AS file ON file.id = source.source_file_id
                    WHERE source.id = ? AND source.school_id = ?
                    """,
                    (source_id, school_id),
                ).fetchone()
            sizes.append(
                int(size_row["size_bytes"])
                if size_row is not None
                else _UNKNOWN_REJECTED_REPLAY_MAX_BYTES
            )
            error = item.get("error")
            preserve_too_large.append(
                isinstance(error, dict) and error.get("code") == "FILE_TOO_LARGE"
            )
        return UploadReplayAllowance(
            filenames=tuple(filenames),
            file_max_bytes=tuple(sizes),
            preserve_too_large=tuple(preserve_too_large),
            max_batch_bytes=sum(sizes),
        )
    finally:
        connection.close()


def _existing_upload_idempotency_response(
    connection: sqlite3.Connection,
    *,
    school_id: str,
    actor_id: str,
    route: str,
    key: str,
    request_body: dict[str, Any],
    current_file_errors: list[str | None],
) -> StoredResponse | None:
    canonical_digest = request_hash(request_body)
    row = connection.execute(
        """
        SELECT request_hash, response_status, response_body
        FROM idempotency_keys
        WHERE school_id = ? AND actor_id = ? AND route = ? AND key = ?
        """,
        (school_id, actor_id, route, key),
    ).fetchone()
    if row is None:
        return None

    stored_body = json.loads(row["response_body"]) if row["response_body"] else None
    compatible_digests = {canonical_digest}
    client_files = request_body.get("files")
    stored_items = stored_body.get("items") if isinstance(stored_body, dict) else None
    if (
        isinstance(client_files, list)
        and isinstance(stored_items, list)
        and len(client_files) == len(stored_items)
        and len(client_files) == len(current_file_errors)
    ):
        legacy_files: list[dict[str, Any]] = []
        compatible = True
        for client_file, stored_item, current_error in zip(
            client_files,
            stored_items,
            current_file_errors,
            strict=True,
        ):
            if (
                not isinstance(client_file, dict)
                or not isinstance(stored_item, dict)
                or client_file.get("filename") != stored_item.get("filename")
            ):
                compatible = False
                break
            error = stored_item.get("error")
            error_code = error.get("code") if isinstance(error, dict) else None
            if error_code != current_error:
                compatible = False
                break
            legacy_files.append(
                {"filename": client_file["filename"], "error": error_code}
                if error_code
                else {
                    "filename": client_file["filename"],
                    "sha256": client_file["sha256"],
                }
            )
        if compatible:
            compatible_digests.add(
                request_hash({**request_body, "files": legacy_files})
            )

    if row["request_hash"] not in compatible_digests:
        raise IdempotencyConflict()
    if row["response_status"] is None or stored_body is None:
        raise IdempotencyConflict("IDEMPOTENCY_REQUEST_IN_PROGRESS")
    return StoredResponse(status=int(row["response_status"]), body=stored_body)


def validate_existing_upload_idempotency_key(
    connection: sqlite3.Connection,
    *,
    school_id: str,
    actor_id: str,
    route: str,
    key: str,
    request_body: dict[str, Any],
    current_file_errors: list[str | None],
) -> StoredResponse | None:
    """Validate a pre-existing ledger row before a canonical claim can be bound."""
    return _existing_upload_idempotency_response(
        connection,
        school_id=school_id,
        actor_id=actor_id,
        route=route,
        key=key,
        request_body=request_body,
        current_file_errors=current_file_errors,
    )


def reserve_upload_idempotency_key(
    connection: sqlite3.Connection,
    *,
    school_id: str,
    actor_id: str,
    route: str,
    key: str,
    request_body: dict[str, Any],
    current_file_errors: list[str | None],
) -> StoredResponse | None:
    """Reserve the legacy ledger and safely bridge pre-0006b upload fingerprints.

    Pre-0006b accepted entries contained only filename/digest, while rejected
    entries contained only filename/server error code.  Reconstruct that exact
    representation, and require the policy-exempt current classification to match
    the historical response before accepting the compatibility digest.
    """
    canonical_digest = request_hash(request_body)
    inserted = connection.execute(
        """
        INSERT OR IGNORE INTO idempotency_keys (
            id, school_id, actor_id, route, key, request_hash, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(uuid.uuid4()),
            school_id,
            actor_id,
            route,
            key,
            canonical_digest,
            format_utc(utc_now()),
        ),
    )
    if inserted.rowcount == 1:
        return None
    replay = _existing_upload_idempotency_response(
        connection,
        school_id=school_id,
        actor_id=actor_id,
        route=route,
        key=key,
        request_body=request_body,
        current_file_errors=current_file_errors,
    )
    if replay is None:
        raise IdempotencyConflict("IDEMPOTENCY_REQUEST_IN_PROGRESS")
    return replay


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
                        created_at, updated_at, request_metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, 'IN_PROGRESS', ?, ?, ?)
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
                        json.dumps(
                            {
                                "files": [
                                    {
                                        "filename": item.get("filename"),
                                        "size_bytes": item.get("size_bytes"),
                                    }
                                    for item in request_body.get("files", [])
                                    if isinstance(item, dict)
                                ]
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        if isinstance(request_body, dict)
                        else None,
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
    *,
    wait_deadline_seconds: float = 0.0,
) -> bool:
    """Best-effort fenced cleanup that never outlives its bounded deadline."""
    deadline = time.monotonic() + max(0.0, wait_deadline_seconds)
    delay = _INITIAL_BACKOFF_SECONDS
    connection: sqlite3.Connection | None = None
    try:
        while True:
            try:
                connection = sqlite3.connect(
                    Path(database_path).resolve(),
                    timeout=0,
                    check_same_thread=False,
                )
                connection.row_factory = sqlite3.Row
                connection.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MILLISECONDS}")
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("BEGIN IMMEDIATE")
                if time.monotonic() >= deadline:
                    connection.rollback()
                    return False
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
                    return True
                for path in paths:
                    if time.monotonic() >= deadline:
                        connection.rollback()
                        return False
                    referenced = connection.execute(
                        "SELECT 1 FROM source_files WHERE storage_path = ?",
                        (str(path),),
                    ).fetchone()
                    if referenced is None:
                        path.unlink(missing_ok=True)
                if time.monotonic() >= deadline:
                    connection.rollback()
                    return False
                connection.execute(
                    """
                    UPDATE upload_idempotency_claims
                    SET lease_owner = NULL, lease_expires_at = NULL, updated_at = ?
                    WHERE id = ? AND generation = ? AND lease_owner = ?
                      AND state = 'IN_PROGRESS'
                    """,
                    (format_utc(utc_now()), lease.id, lease.generation, lease.owner),
                )
                if time.monotonic() >= deadline:
                    connection.rollback()
                    return False
                connection.commit()
                return True
            except sqlite3.OperationalError as error:
                if connection is not None and connection.in_transaction:
                    connection.rollback()
                if not _is_locked(error) or time.monotonic() >= deadline:
                    return False
                remaining = deadline - time.monotonic()
                time.sleep(min(delay, max(0.0, remaining)))
                delay = min(delay * 2, _MAX_BACKOFF_SECONDS)
            except Exception:  # noqa: BLE001 -- cleanup must never mask the original request failure.
                if connection is not None and connection.in_transaction:
                    connection.rollback()
                return False
            finally:
                if connection is not None:
                    connection.close()
                    connection = None
    finally:
        if connection is not None:
            connection.close()
