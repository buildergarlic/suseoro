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
    require_idempotency_key,
    require_request_id,
)
from suseoro.config import Settings
from suseoro.repositories.auth import authenticate_user, issue_session, revoke_session
from suseoro.security.secrets import MachineSecretStore
from suseoro.security.sessions import CSRF_COOKIE_NAME, SESSION_COOKIE_NAME
from suseoro.services.audit import record_audit_event
from suseoro.services.idempotency import (
    complete_idempotent_request,
    reserve_idempotency_key,
)

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


def _secret_store(request: Request) -> MachineSecretStore:
    store = getattr(request.app.state, "secret_store", None)
    if store is None:
        store = MachineSecretStore()
        request.app.state.secret_store = store
    return store


def _set_session_cookies(
    response: Response,
    settings: Settings,
    session_token: str,
    csrf_token: str,
) -> None:
    response.set_cookie(
        SESSION_COOKIE_NAME,
        session_token,
        httponly=True,
        secure=settings.secure_cookies,
        samesite="lax",
        path="/",
        max_age=settings.session_ttl_seconds,
    )
    response.set_cookie(
        CSRF_COOKIE_NAME,
        csrf_token,
        httponly=False,
        secure=settings.secure_cookies,
        samesite="lax",
        path="/",
        max_age=settings.session_ttl_seconds,
    )


@router.post("/login", response_model=UserResponse)
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    connection: Annotated[sqlite3.Connection, Depends(database_connection)],
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
    request_id: Annotated[str, Depends(require_request_id)],
) -> UserResponse:
    user = authenticate_user(
        connection, payload.school_id, payload.username, payload.password
    )
    if user is None:
        raise HTTPException(status_code=401, detail={"code": "INVALID_CREDENTIALS"})
    settings: Settings = request.app.state.settings
    store = _secret_store(request)
    public_response = UserResponse(
        id=user.id,
        school_id=user.school_id,
        username=user.username,
        display_name=user.display_name,
        roles=list(user.roles),
    )
    try:
        replay = reserve_idempotency_key(
            connection,
            school_id=user.school_id,
            actor_id=user.id,
            route="POST /api/v2/auth/login",
            key=idempotency_key,
            request_body=payload.model_dump(),
        )
        if replay is not None:
            connection.rollback()
            session_token = store.decrypt(replay.body["session_ciphertext"])
            csrf_token = store.decrypt(replay.body["csrf_ciphertext"])
            _set_session_cookies(
                response, settings, session_token=session_token, csrf_token=csrf_token
            )
            return UserResponse.model_validate(replay.body["response"])

        issued = issue_session(connection, user, settings.session_ttl_seconds)
        record_audit_event(
            connection,
            actor_id=user.id,
            school_id=user.school_id,
            action="AUTH_LOGIN",
            entity_type="session",
            entity_id=issued.session_id,
            before=None,
            after={"expires_at": issued.expires_at, "roles": list(user.roles)},
            request_id=request_id,
        )
        complete_idempotent_request(
            connection,
            school_id=user.school_id,
            actor_id=user.id,
            route="POST /api/v2/auth/login",
            key=idempotency_key,
            status=200,
            body={
                "response": public_response.model_dump(),
                "session_ciphertext": store.encrypt(issued.session_token),
                "csrf_ciphertext": store.encrypt(issued.csrf_token),
                "expires_at": issued.expires_at,
            },
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    _set_session_cookies(
        response,
        settings,
        session_token=issued.session_token,
        csrf_token=issued.csrf_token,
    )
    return public_response


@router.post("/logout", status_code=204)
def logout(
    response: Response,
    user: Annotated[AuthenticatedUser, Depends(csrf_protected_user)],
    connection: Annotated[sqlite3.Connection, Depends(database_connection)],
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
    request_id: Annotated[str, Depends(require_request_id)],
) -> None:
    try:
        replay = reserve_idempotency_key(
            connection,
            school_id=user.school_id,
            actor_id=user.id,
            route="POST /api/v2/auth/logout",
            key=idempotency_key,
            request_body={"session_id": user.session_id},
        )
        if replay is not None:
            connection.rollback()
        else:
            if user.revoked_at is not None:
                raise HTTPException(status_code=401, detail={"code": "INVALID_SESSION"})
            revoked_at = revoke_session(connection, user.session_id)
            record_audit_event(
                connection,
                actor_id=user.id,
                school_id=user.school_id,
                action="AUTH_LOGOUT",
                entity_type="session",
                entity_id=user.session_id,
                before={"revoked_at": None},
                after={"revoked_at": revoked_at},
                request_id=request_id,
            )
            complete_idempotent_request(
                connection,
                school_id=user.school_id,
                actor_id=user.id,
                route="POST /api/v2/auth/logout",
                key=idempotency_key,
                status=204,
                body={},
            )
            connection.commit()
    except Exception:
        connection.rollback()
        raise
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    response.delete_cookie(CSRF_COOKIE_NAME, path="/")


@router.get("/me", response_model=UserResponse)
def me(user: Annotated[AuthenticatedUser, Depends(current_user)]) -> UserResponse:
    return _response(user)
