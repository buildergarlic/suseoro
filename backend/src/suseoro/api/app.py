"""FastAPI application factory for Suseoro v2."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from suseoro.api.dependencies import authenticated_user_from_session
from suseoro.api.routes.auth import router as auth_router
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
        with connect(application_settings.database_path) as connection:
            apply_migrations(connection)
        app.state.database_ready = True
        app.state.worker_state = "not_started"
        yield

    app = FastAPI(title="Suseoro v2", version=application_settings.version, lifespan=lifespan)
    app.state.settings = application_settings
    app.include_router(auth_router)

    @app.middleware("http")
    async def csrf_boundary(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS", "TRACE"} and request.url.path != "/api/v2/auth/login":
            session_token = request.cookies.get(SESSION_COOKIE_NAME)
            if not session_token:
                return JSONResponse(
                    status_code=401, content={"detail": {"code": "AUTHENTICATION_REQUIRED"}}
                )
            with connect(application_settings.database_path) as connection:
                record = find_session(
                    connection,
                    session_token,
                    include_revoked=request.url.path == "/api/v2/auth/logout",
                )
            if record is None:
                return JSONResponse(
                    status_code=401, content={"detail": {"code": "INVALID_SESSION"}}
                )
            if not validate_csrf_token(
                request.cookies.get(CSRF_COOKIE_NAME),
                request.headers.get(CSRF_HEADER_NAME),
                record.csrf_token_digest,
            ):
                return JSONResponse(
                    status_code=403, content={"detail": {"code": "CSRF_VALIDATION_FAILED"}}
                )
            request.state.authenticated_user = authenticated_user_from_session(record)
        return await call_next(request)

    @app.get("/api/v2/health")
    def health() -> dict[str, str]:
        return {
            "version": application_settings.version,
            "database": "ready" if app.state.database_ready else "not_ready",
            "worker": app.state.worker_state,
        }

    return app


app = create_app()
