"""FastAPI application factory for Suseoro v2."""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.openapi.utils import get_openapi

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
from suseoro.api.schemas import (
    OPERATION_RESPONSE_MODELS,
    ApiErrorResponse,
)
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

    def hardened_openapi():
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            routes=app.routes,
        )
        components = schema.setdefault("components", {}).setdefault("schemas", {})

        def install_model(model) -> None:
            generated = model.model_json_schema(
                ref_template="#/components/schemas/{model}"
            )
            definitions = generated.pop("$defs", {})
            components.update(definitions)
            components[model.__name__] = generated

        install_model(ApiErrorResponse)
        for response_model in set(OPERATION_RESPONSE_MODELS.values()):
            install_model(response_model)
        methods = {"get", "post", "put", "patch", "delete"}
        for path, path_item in schema["paths"].items():
            for method, operation in path_item.items():
                if method not in methods:
                    continue
                operation_id = operation["operationId"]
                if operation_id == "streamEvents":
                    for item in operation.get("parameters", []):
                        if item.get("name") == "Last-Event-ID":
                            item["schema"] = {"type": "integer", "minimum": 0}
                for status, response in list(operation["responses"].items()):
                    content = response.get("content", {})
                    json_content = content.get("application/json")
                    if status.startswith("2") and json_content is not None:
                        if operation_id in {"downloadOrder", "streamEvents"}:
                            continue
                        response_model = OPERATION_RESPONSE_MODELS.get(operation_id)
                        if response_model is None:
                            raise RuntimeError(
                                f"missing concrete response model for {operation_id}"
                            )
                        json_content["schema"] = {
                            "$ref": (f"#/components/schemas/{response_model.__name__}")
                        }
                for status in (
                    "400",
                    "401",
                    "403",
                    "404",
                    "409",
                    "412",
                    "422",
                    "428",
                    "500",
                    "503",
                ):
                    operation["responses"][status] = {
                        "description": "구조화된 오류",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": "#/components/schemas/ApiErrorResponse"
                                }
                            }
                        },
                    }
                if (
                    method in {"post", "put", "patch", "delete"}
                    and operation_id != "login"
                ):
                    parameters = operation.setdefault("parameters", [])
                    for item in parameters:
                        if (
                            item.get("in") == "header"
                            and item.get("name") == "If-Match"
                        ):
                            item["required"] = True
                    existing = {
                        (item.get("in"), item.get("name")): item for item in parameters
                    }
                    for header, description in (
                        ("X-CSRF-Token", "CSRF 방지 토큰"),
                        ("X-Request-ID", "요청 UUID"),
                        ("Idempotency-Key", "중복 요청 방지 키"),
                    ):
                        item = existing.get(("header", header))
                        if item is None:
                            parameters.append(
                                {
                                    "name": header,
                                    "in": "header",
                                    "required": True,
                                    "description": description,
                                    "schema": {"type": "string"},
                                }
                            )
                        else:
                            item["required"] = True
                if (
                    path.startswith("/api/v2/admin/v1-migration")
                    or path == "/api/v2/admin/restores"
                ):
                    parameters = operation.setdefault("parameters", [])
                    confirmation = next(
                        (
                            item
                            for item in parameters
                            if item.get("in") == "header"
                            and item.get("name") == "X-Local-Admin-Confirmation"
                        ),
                        None,
                    )
                    if confirmation is None:
                        parameters.append(
                            {
                                "name": "X-Local-Admin-Confirmation",
                                "in": "header",
                                "required": True,
                                "schema": {"type": "string"},
                            }
                        )
                    else:
                        confirmation["required"] = True
        app.openapi_schema = schema
        return schema

    app.openapi = hardened_openapi

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
