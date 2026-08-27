"""Session authentication routes."""

from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from suseoro.api.dependencies import (
    AuthenticatedUser,
    csrf_protected_user,
    current_user,
    database_connection,
)
from suseoro.config import Settings
from suseoro.repositories.auth import authenticate_user, issue_session, revoke_session
from suseoro.security.sessions import CSRF_COOKIE_NAME, SESSION_COOKIE_NAME

router = APIRouter(prefix="/api/v2/auth", tags=["authentication"])


class LoginRequest(BaseModel):
    school_id: str
    username: str
    password: str


class UserResponse(BaseModel):
    id: str
    school_id: str
    username: str
    display_name: str
    roles: list[str]


def _response(user: AuthenticatedUser) -> UserResponse:
    return UserResponse(
        id=user.id,
        school_id=user.school_id,
        username=user.username,
        display_name=user.display_name,
        roles=list(user.roles),
    )


@router.post("/login", response_model=UserResponse)
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    connection: Annotated[sqlite3.Connection, Depends(database_connection)],
) -> UserResponse:
    user = authenticate_user(
        connection, payload.school_id, payload.username, payload.password
    )
    if user is None:
        raise HTTPException(status_code=401, detail={"code": "INVALID_CREDENTIALS"})
    settings: Settings = request.app.state.settings
    issued = issue_session(connection, user, settings.session_ttl_seconds)
    connection.commit()
    response.set_cookie(
        SESSION_COOKIE_NAME,
        issued.session_token,
        httponly=True,
        secure=settings.secure_cookies,
        samesite="lax",
        path="/",
        max_age=settings.session_ttl_seconds,
    )
    response.set_cookie(
        CSRF_COOKIE_NAME,
        issued.csrf_token,
        httponly=False,
        secure=settings.secure_cookies,
        samesite="lax",
        path="/",
        max_age=settings.session_ttl_seconds,
    )
    return UserResponse(
        id=user.id,
        school_id=user.school_id,
        username=user.username,
        display_name=user.display_name,
        roles=list(user.roles),
    )


@router.post("/logout", status_code=204)
def logout(
    response: Response,
    user: Annotated[AuthenticatedUser, Depends(csrf_protected_user)],
    connection: Annotated[sqlite3.Connection, Depends(database_connection)],
) -> None:
    revoke_session(connection, user.session_id)
    connection.commit()
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    response.delete_cookie(CSRF_COOKIE_NAME, path="/")


@router.get("/me", response_model=UserResponse)
def me(user: Annotated[AuthenticatedUser, Depends(current_user)]) -> UserResponse:
    return _response(user)
