"""Database operations for users and opaque sessions."""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import timedelta

from suseoro.security.passwords import hash_password, verify_password
from suseoro.security.sessions import (
    digest_token,
    format_utc,
    generate_token,
    parse_utc,
    utc_now,
)

_DUMMY_PASSWORD_HASH = hash_password("Invalid login 42!")


@dataclass(frozen=True)
class UserRecord:
    id: str
    school_id: str
    username: str
    display_name: str
    roles: tuple[str, ...]


@dataclass(frozen=True)
class SessionRecord(UserRecord):
    session_id: str
    csrf_token_digest: str
    revoked_at: str | None


@dataclass(frozen=True)
class IssuedSession:
    user: UserRecord
    session_id: str
    session_token: str
    csrf_token: str
    expires_at: str


def _roles(
    connection: sqlite3.Connection, school_id: str, user_id: str
) -> tuple[str, ...]:
    rows = connection.execute(
        """
        SELECT role FROM user_roles
        WHERE school_id = ? AND user_id = ?
        ORDER BY role
        """,
        (school_id, user_id),
    ).fetchall()
    return tuple(row["role"] for row in rows)


def authenticate_user(
    connection: sqlite3.Connection, school_id: str, username: str, password: str
) -> UserRecord | None:
    row = connection.execute(
        """
        SELECT id, school_id, username, password_hash, display_name
        FROM users
        WHERE school_id = ? AND username = ? AND is_active = 1
        """,
        (school_id, username),
    ).fetchone()
    encoded_hash = row["password_hash"] if row is not None else _DUMMY_PASSWORD_HASH
    if not verify_password(password, encoded_hash) or row is None:
        return None
    return UserRecord(
        id=row["id"],
        school_id=row["school_id"],
        username=row["username"],
        display_name=row["display_name"],
        roles=_roles(connection, row["school_id"], row["id"]),
    )


def issue_session(
    connection: sqlite3.Connection, user: UserRecord, ttl_seconds: int
) -> IssuedSession:
    now = utc_now()
    expires_at = format_utc(now + timedelta(seconds=ttl_seconds))
    session_token = generate_token()
    csrf_token = generate_token()
    session_id = str(uuid.uuid4())
    connection.execute(
        """
        INSERT INTO sessions (
            id, school_id, user_id, token_digest, csrf_token_digest,
            expires_at, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            user.school_id,
            user.id,
            digest_token(session_token),
            digest_token(csrf_token),
            expires_at,
            format_utc(now),
        ),
    )
    return IssuedSession(user, session_id, session_token, csrf_token, expires_at)


def find_session(
    connection: sqlite3.Connection,
    session_token: str,
    *,
    include_revoked: bool = False,
) -> SessionRecord | None:
    row = connection.execute(
        """
        SELECT
            s.id AS session_id, s.school_id, s.user_id AS id,
            s.csrf_token_digest, s.expires_at, s.revoked_at,
            u.username, u.display_name
        FROM sessions AS s
        JOIN users AS u ON u.id = s.user_id AND u.school_id = s.school_id
        WHERE s.token_digest = ?
          AND s.csrf_token_digest IS NOT NULL
          AND u.is_active = 1
        """,
        (digest_token(session_token),),
    ).fetchone()
    if (
        row is None
        or parse_utc(row["expires_at"]) <= utc_now()
        or (row["revoked_at"] is not None and not include_revoked)
    ):
        return None
    return SessionRecord(
        id=row["id"],
        school_id=row["school_id"],
        username=row["username"],
        display_name=row["display_name"],
        roles=_roles(connection, row["school_id"], row["id"]),
        session_id=row["session_id"],
        csrf_token_digest=row["csrf_token_digest"],
        revoked_at=row["revoked_at"],
    )


def find_active_session(
    connection: sqlite3.Connection, session_token: str
) -> SessionRecord | None:
    return find_session(connection, session_token)


def revoke_session(connection: sqlite3.Connection, session_id: str) -> str:
    revoked_at = format_utc(utc_now())
    connection.execute(
        "UPDATE sessions SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
        (revoked_at, session_id),
    )
    return revoked_at
