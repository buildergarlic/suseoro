"""FastAPI application factory for Suseoro v2."""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from suseoro.api.dependencies import authenticated_user_from_session
from suseoro.api.errors import error_response, install_error_handlers, request_id_for
from suseoro.api.routes.approvals import router as approvals_router
from suseoro.api.routes.audit import router as audit_router
from suseoro.api.routes.auth import router as auth_router
from suseoro.api.routes.candidates import router as candidates_router
from suseoro.api.routes.deliveries import router as deliveries_router
from suseoro.api.routes.events import router as events_router
from suseoro.api.routes.procurement import router as procurement_router
from suseoro.api.routes.sources import router as sources_router
from suseoro.api.routes.workspaces import router as workspaces_router
from suseoro.config import Settings
from suseoro.db.connection import connect
from suseoro.db.migrations import apply_migrations
from suseoro.repositories.auth import find_session
from suseoro.security.csrf import validate_csrf_token
from suseoro.security.sessions import (
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    SESSION_COOKIE_NAME,
)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create the API and run schema migrations during application startup."""
    application_settings = settings or Settings.from_environment()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        connection = connect(application_settings.database_path)
        try:
            apply_migrations(connection)
        finally:
            connection.close()
        app.state.database_ready = True
        app.state.worker_state = "not_started"
        yield

    app = FastAPI(
        title="Suseoro v2", version=application_settings.version, lifespan=lifespan
    )
    app.state.settings = application_settings
    app.state.database_ready = False
    app.state.worker_state = "not_started"
    app.state.local_admin_confirmation_token = secrets.token_urlsafe(32)
    install_error_handlers(app)
    for router in (
        auth_router,
        workspaces_router,
        sources_router,
        candidates_router,
        approvals_router,
        procurement_router,
        deliveries_router,
        events_router,
        audit_router,
    ):
        app.include_router(router)

    @app.middleware("http")
    async def csrf_boundary(request: Request, call_next):
        request_id_for(request)
        if (
            request.method not in {"GET", "HEAD", "OPTIONS", "TRACE"}
            and request.url.path != "/api/v2/auth/login"
        ):
            session_token = request.cookies.get(SESSION_COOKIE_NAME)
            if not session_token:
                return error_response(
                    request, status_code=401, code="AUTHENTICATION_REQUIRED"
                )
            connection = connect(application_settings.database_path)
            try:
                record = find_session(
                    connection,
                    session_token,
                    include_revoked=request.url.path == "/api/v2/auth/logout",
                )
            finally:
                connection.close()
            if record is None:
                return error_response(request, status_code=401, code="INVALID_SESSION")
            if not validate_csrf_token(
                request.cookies.get(CSRF_COOKIE_NAME),
                request.headers.get(CSRF_HEADER_NAME),
                record.csrf_token_digest,
            ):
                return error_response(
                    request, status_code=403, code="CSRF_VALIDATION_FAILED"
                )
            request.state.authenticated_user = authenticated_user_from_session(record)
        return await call_next(request)

    @app.get(
        "/api/v2/health",
        summary="서버 상태 확인하기",
        operation_id="getHealth",
    )
    def health() -> dict[str, str]:
        return {
            "version": application_settings.version,
            "database": "ready" if app.state.database_ready else "not_ready",
            "worker": app.state.worker_state,
        }

    return app


app = create_app()
