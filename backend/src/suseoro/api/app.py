"""FastAPI application factory for Suseoro v2."""

from __future__ import annotations

import secrets
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute

from suseoro.api.dependencies import (
    authenticated_user_from_session,
    current_user,
    database_connection,
    require_idempotency_key,
    require_if_match,
    require_request_id,
)
from suseoro.api.errors import error_response, install_error_handlers, request_id_for
from suseoro.api.routes.approvals import router as approvals_router
from suseoro.api.routes.audit import router as audit_router
from suseoro.api.routes.auth import router as auth_router
from suseoro.api.routes.candidates import router as candidates_router
from suseoro.api.routes.deliveries import router as deliveries_router
from suseoro.api.routes.events import router as events_router
from suseoro.api.routes.procurement import router as procurement_router
from suseoro.api.routes.procurement_imports import router as procurement_imports_router
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

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
_CSRF_EXEMPT_PATHS = frozenset({"/api/v2/auth/login"})
_DEPENDENCY_HEADERS = {
    require_idempotency_key: "Idempotency-Key",
    require_if_match: "If-Match",
    require_request_id: "X-Request-ID",
}


def _requires_csrf(method: str, path: str) -> bool:
    """Return the shared runtime/schema CSRF decision for one request."""
    return method.upper() not in _SAFE_METHODS and path not in _CSRF_EXEMPT_PATHS


def _dependency_calls(route: APIRoute) -> set[object]:
    calls: set[object] = set()
    pending = [route.dependant]
    while pending:
        dependant = pending.pop()
        if dependant.call is not None:
            calls.add(dependant.call)
        pending.extend(dependant.dependencies)
    return calls


def _require_openapi_header(
    parameters: list[dict[str, object]],
    name: str,
    description: str | None = None,
) -> None:
    created = False
    parameter = next(
        (
            item
            for item in parameters
            if item.get("in") == "header" and item.get("name") == name
        ),
        None,
    )
    if parameter is None:
        parameter = {"name": name, "in": "header"}
        parameters.append(parameter)
        created = True
    parameter["required"] = True
    parameter["schema"] = {"type": "string"}
    if description is not None and created:
        parameter["description"] = description


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
        procurement_imports_router,
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
        component_root = schema.setdefault("components", {})
        components = component_root.setdefault("schemas", {})
        component_root["securitySchemes"] = {
            "SessionCookie": {
                "type": "apiKey",
                "in": "cookie",
                "name": SESSION_COOKIE_NAME,
            },
            "CsrfCookie": {
                "type": "apiKey",
                "in": "cookie",
                "name": CSRF_COOKIE_NAME,
            },
            "CsrfHeader": {
                "type": "apiKey",
                "in": "header",
                "name": CSRF_HEADER_NAME,
            },
        }

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
        routes_by_operation_id = {
            route.operation_id: route
            for route in app.routes
            if isinstance(route, APIRoute) and route.operation_id is not None
        }
        methods = {"get", "post", "put", "patch", "delete"}
        for path, path_item in schema["paths"].items():
            for method, operation in path_item.items():
                if method not in methods:
                    continue
                operation_id = operation["operationId"]
                route = routes_by_operation_id[operation_id]
                dependency_calls = _dependency_calls(route)
                csrf_required = _requires_csrf(method, path)
                session_required = csrf_required or current_user in dependency_calls
                role_required = any(
                    getattr(call, "suseoro_required_role", None) is not None
                    for call in dependency_calls
                )
                if csrf_required:
                    operation["security"] = [
                        {
                            "SessionCookie": [],
                            "CsrfCookie": [],
                            "CsrfHeader": [],
                        }
                    ]
                elif session_required:
                    operation["security"] = [{"SessionCookie": []}]
                else:
                    operation["security"] = []
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
                common_errors: set[str] = set()
                if any(
                    dependency in dependency_calls for dependency in _DEPENDENCY_HEADERS
                ):
                    common_errors.add("400")
                if session_required:
                    common_errors.add("401")
                if csrf_required or role_required:
                    common_errors.add("403")
                if require_idempotency_key in dependency_calls:
                    common_errors.add("409")
                if require_if_match in dependency_calls:
                    common_errors.update({"412", "428"})
                if csrf_required or database_connection in dependency_calls:
                    common_errors.add("503")
                for status in common_errors:
                    operation["responses"].setdefault(
                        status, {"description": "구조화된 오류"}
                    )
                operation["responses"].setdefault(
                    "default", {"description": "구조화된 오류"}
                )
                for status, error in operation["responses"].items():
                    if status.startswith("2"):
                        continue
                    error["content"] = {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/ApiErrorResponse"}
                        }
                    }
                parameters = operation.get("parameters", [])
                if csrf_required:
                    _require_openapi_header(
                        parameters, "X-CSRF-Token", "CSRF 방지 토큰"
                    )
                for dependency, header in _DEPENDENCY_HEADERS.items():
                    if dependency in dependency_calls:
                        _require_openapi_header(parameters, header)
                if any(
                    item.get("in") == "header"
                    and item.get("name") == "X-Local-Admin-Confirmation"
                    for item in parameters
                ):
                    _require_openapi_header(parameters, "X-Local-Admin-Confirmation")
                if parameters:
                    operation["parameters"] = parameters
        app.openapi_schema = schema
        return schema

    app.openapi = hardened_openapi

    @app.middleware("http")
    async def csrf_boundary(request: Request, call_next):
        request_id_for(request)
        try:
            if _requires_csrf(request.method, request.url.path):
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
                    return error_response(
                        request, status_code=401, code="INVALID_SESSION"
                    )
                if not validate_csrf_token(
                    request.cookies.get(CSRF_COOKIE_NAME),
                    request.headers.get(CSRF_HEADER_NAME),
                    record.csrf_token_digest,
                ):
                    return error_response(
                        request, status_code=403, code="CSRF_VALIDATION_FAILED"
                    )
                request.state.authenticated_user = authenticated_user_from_session(
                    record
                )
            return await call_next(request)
        except (sqlite3.OperationalError, sqlite3.ProgrammingError):
            return error_response(
                request,
                status_code=503,
                code="DATABASE_UNAVAILABLE",
            )

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
