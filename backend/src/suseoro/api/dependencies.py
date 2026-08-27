"""Reusable authentication, CSRF, role, and approval policy boundaries."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Cookie, Depends, Header, HTTPException, Request

from suseoro.db.connection import connect
from suseoro.repositories.auth import SessionRecord, find_active_session
from suseoro.security.csrf import validate_csrf_token
from suseoro.security.sessions import CSRF_COOKIE_NAME, SESSION_COOKIE_NAME


@dataclass(frozen=True)
class AuthenticatedUser:
    id: str
    school_id: str
    username: str
    display_name: str
    roles: tuple[str, ...]
    session_id: str
    csrf_token_digest: str


def authenticated_user_from_session(record: SessionRecord) -> AuthenticatedUser:
    return AuthenticatedUser(
        id=record.id,
        school_id=record.school_id,
        username=record.username,
        display_name=record.display_name,
        roles=record.roles,
        session_id=record.session_id,
        csrf_token_digest=record.csrf_token_digest,
    )


def database_connection(request: Request) -> Iterator[sqlite3.Connection]:
    connection = connect(request.app.state.settings.database_path)
    try:
        yield connection
    finally:
        connection.close()


def current_user(
    request: Request,
    connection: Annotated[sqlite3.Connection, Depends(database_connection)],
    session_token: Annotated[str | None, Cookie(alias=SESSION_COOKIE_NAME)] = None,
) -> AuthenticatedUser:
    cached = getattr(request.state, "authenticated_user", None)
    if cached is not None:
        return cached
    if not session_token:
        raise HTTPException(status_code=401, detail={"code": "AUTHENTICATION_REQUIRED"})
    record = find_active_session(connection, session_token)
    if record is None:
        raise HTTPException(status_code=401, detail={"code": "INVALID_SESSION"})
    return authenticated_user_from_session(record)


def csrf_protected_user(
    user: Annotated[AuthenticatedUser, Depends(current_user)],
    csrf_cookie: Annotated[str | None, Cookie(alias=CSRF_COOKIE_NAME)] = None,
    csrf_header: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> AuthenticatedUser:
    if not validate_csrf_token(csrf_cookie, csrf_header, user.csrf_token_digest):
        raise HTTPException(status_code=403, detail={"code": "CSRF_VALIDATION_FAILED"})
    return user


def enforce_role(user: AuthenticatedUser, required_role: str) -> AuthenticatedUser:
    """Enforce a role inside the authenticated user's school scope."""
    if required_role not in {"OPERATOR", "REVIEWER"}:
        raise ValueError(f"unknown role: {required_role}")
    if required_role not in user.roles:
        raise HTTPException(status_code=403, detail={"code": "ROLE_REQUIRED", "role": required_role})
    return user


def require_role(required_role: str) -> Callable[[AuthenticatedUser], AuthenticatedUser]:
    def dependency(
        user: Annotated[AuthenticatedUser, Depends(current_user)],
    ) -> AuthenticatedUser:
        return enforce_role(user, required_role)

    return dependency


def may_approve_own_change(
    actor_id: str, change_author_id: str, single_operator_mode: bool
) -> bool:
    """Permit self-approval only under the school's explicit single-operator policy."""
    return actor_id != change_author_id or single_operator_mode
