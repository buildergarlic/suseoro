"""FastAPI application factory for Suseoro v2."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from suseoro.config import Settings
from suseoro.db.connection import connect
from suseoro.db.migrations import apply_migrations


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

    @app.get("/api/v2/health")
    def health() -> dict[str, str]:
        return {
            "version": application_settings.version,
            "database": "ready" if app.state.database_ready else "not_ready",
            "worker": app.state.worker_state,
        }

    return app


app = create_app()
