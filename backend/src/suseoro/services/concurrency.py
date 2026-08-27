"""Optimistic row-version updates and two-minute edit leases."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from fastapi import HTTPException

from suseoro.security.sessions import format_utc, utc_now

EDIT_LOCK_TTL = timedelta(minutes=2)
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class VersionConflict(HTTPException):
    def __init__(self, current: int, submitted: int) -> None:
        super().__init__(
            status_code=412,
            detail={
                "code": "ROW_VERSION_CONFLICT",
                "current": current,
                "submitted": submitted,
            },
        )


class EntityNotFound(HTTPException):
    def __init__(self) -> None:
        super().__init__(status_code=404, detail={"code": "ENTITY_NOT_FOUND"})


class EditLockConflict(HTTPException):
    def __init__(self, actor_id: str, expires_at: str) -> None:
        super().__init__(
            status_code=409,
            detail={"code": "EDIT_LOCKED", "actor_id": actor_id, "expires_at": expires_at},
        )


class EditLockAuthorizationError(HTTPException):
    def __init__(self) -> None:
        super().__init__(
            status_code=403, detail={"code": "EDIT_LOCK_ROLE_REQUIRED"}
        )


@dataclass(frozen=True)
class EditLock:
    school_id: str
    entity_type: str
    entity_id: str
    actor_id: str
    acquired_at: datetime
    expires_at: datetime


def _safe_identifier(value: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"invalid SQL identifier: {value!r}")
    return value


def update_with_version(
    connection: sqlite3.Connection,
    *,
    table: str,
    school_id: str,
    entity_id: str,
    submitted_version: int,
    changes: dict[str, Any],
) -> sqlite3.Row:
    """Atomically apply changes only when the submitted row version is current."""
    if not changes:
        raise ValueError("versioned update requires at least one change")
    table_name = _safe_identifier(table)
    columns = [_safe_identifier(column) for column in changes]
    immutable_columns = {"id", "school_id", "row_version"}
    rejected = immutable_columns.intersection(columns)
    if rejected:
        names = ", ".join(sorted(rejected))
        raise ValueError(f"immutable columns cannot be directly changed: {names}")
    assignments = ", ".join(f'"{column}" = ?' for column in columns)
    parameters = [changes[column] for column in changes]
    result = connection.execute(
        f'UPDATE "{table_name}" SET {assignments}, row_version = row_version + 1 '
        "WHERE id = ? AND school_id = ? AND row_version = ?",
        (*parameters, entity_id, school_id, submitted_version),
    )
    if result.rowcount != 1:
        current = connection.execute(
            f'SELECT row_version FROM "{table_name}" WHERE id = ? AND school_id = ?',
            (entity_id, school_id),
        ).fetchone()
        if current is None:
            raise EntityNotFound()
        raise VersionConflict(current["row_version"], submitted_version)
    return connection.execute(
        f'SELECT * FROM "{table_name}" WHERE id = ? AND school_id = ?',
        (entity_id, school_id),
    ).fetchone()


def acquire_edit_lock(
    connection: sqlite3.Connection,
    *,
    school_id: str,
    entity_type: str,
    entity_id: str,
    actor_id: str,
    now: datetime | None = None,
) -> EditLock:
    """Acquire or renew a lease, replacing another editor only after expiry."""
    acquired_at = now or utc_now()
    expires_at = acquired_at + EDIT_LOCK_TTL
    result = connection.execute(
        """
        INSERT INTO edit_locks (
            school_id, entity_type, entity_id, actor_id, acquired_at, expires_at
        )
        SELECT ?, ?, ?, ?, ?, ?
        WHERE EXISTS (
            SELECT 1 FROM user_roles
            WHERE school_id = ? AND user_id = ?
        )
        ON CONFLICT (school_id, entity_type, entity_id) DO UPDATE SET
            actor_id = excluded.actor_id,
            acquired_at = excluded.acquired_at,
            expires_at = excluded.expires_at
        WHERE edit_locks.actor_id = excluded.actor_id
           OR julianday(edit_locks.expires_at) <= julianday(excluded.acquired_at)
        """,
        (
            school_id,
            entity_type,
            entity_id,
            actor_id,
            format_utc(acquired_at),
            format_utc(expires_at),
            school_id,
            actor_id,
        ),
    )
    if result.rowcount != 1:
        membership = connection.execute(
            """
            SELECT 1 FROM user_roles
            WHERE school_id = ? AND user_id = ?
            """,
            (school_id, actor_id),
        ).fetchone()
        if membership is None:
            raise EditLockAuthorizationError()
        existing = connection.execute(
            """
            SELECT actor_id, expires_at FROM edit_locks
            WHERE school_id = ? AND entity_type = ? AND entity_id = ?
            """,
            (school_id, entity_type, entity_id),
        ).fetchone()
        raise EditLockConflict(existing["actor_id"], existing["expires_at"])
    return EditLock(
        school_id=school_id,
        entity_type=entity_type,
        entity_id=entity_id,
        actor_id=actor_id,
        acquired_at=acquired_at,
        expires_at=expires_at,
    )
