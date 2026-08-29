"""Stable Korean API error envelopes."""

from __future__ import annotations

import sqlite3
import uuid
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from suseoro.workflow._common import WorkflowDomainError

_MESSAGES = {
    "AUTHENTICATION_REQUIRED": "로그인이 필요합니다.",
    "INVALID_SESSION": "로그인 시간이 만료되었습니다. 다시 로그인해 주세요.",
    "CSRF_VALIDATION_FAILED": "요청을 확인할 수 없습니다. 새로고침 후 다시 시도해 주세요.",
    "ROLE_REQUIRED": "담당자 권한이 필요합니다.",
    "IDEMPOTENCY_KEY_REQUIRED": "중복 요청 방지 키가 필요합니다.",
    "INVALID_IDEMPOTENCY_KEY": "중복 요청 방지 키가 올바르지 않습니다.",
    "IDEMPOTENCY_KEY_REUSED": "같은 중복 요청 방지 키가 다른 요청에 사용되었습니다.",
    "IDEMPOTENCY_REQUEST_IN_PROGRESS": "같은 요청이 처리 중입니다.",
    "UPLOAD_RESERVATION_BUSY": "다른 저장 작업이 진행 중입니다. 잠시 후 다시 시도해 주세요.",
    "LEGACY_UPLOAD_IDENTITY_UNVERIFIABLE": "이전 업로드는 파일 내용을 안전하게 확인할 수 없습니다. 새 작업으로 다시 올려 주세요.",
    "REQUEST_ID_REQUIRED": "요청 식별자가 필요합니다.",
    "INVALID_REQUEST_ID": "요청 식별자가 올바르지 않습니다.",
    "IF_MATCH_REQUIRED": "현재 버전 정보가 필요합니다.",
    "INVALID_IF_MATCH": "현재 버전 정보가 올바르지 않습니다.",
    "ROW_VERSION_CONFLICT": "다른 사용자가 먼저 수정했습니다. 현재 내용과 변경 내용을 확인해 주세요.",
    "CANDIDATE_COLLECTION_CHANGED": "후보 목록이 바뀌었습니다. 최신 목록을 다시 확인해 주세요.",
    "SOURCE_COUNTS_UNVERIFIED": "일부 자료의 처리 건수를 확인할 수 없어 승인할 수 없습니다.",
    "HISTORICAL_REPLAY_INVALID": "이전 요청 결과를 안전하게 확인할 수 없습니다. 새 요청으로 다시 시도해 주세요.",
    "EDIT_LOCK_REQUIRED": "편집 잠금을 다시 확인해 주세요.",
    "EDIT_LOCKED": "다른 담당자가 편집 중입니다. 잠시 후 다시 확인해 주세요.",
    "VALIDATION_ERROR": "입력 내용을 확인해 주세요.",
    "ENTITY_NOT_FOUND": "요청한 항목을 찾을 수 없습니다.",
    "WORKSPACE_NOT_FOUND": "요청한 수서 작업을 찾을 수 없습니다.",
    "SOURCE_NOT_FOUND": "요청한 자료를 찾을 수 없습니다.",
    "SOURCE_COMPARISON_IN_PROGRESS": "도서 비교가 진행 중이어서 자료를 바꿀 수 없습니다.",
    "SOURCE_PROCESSING_IN_PROGRESS": "자료 읽기가 끝난 뒤 도서 비교를 시작해 주세요.",
    "SOURCE_ALREADY_IMPORTED": "이미 견적·납품 결과에 반영한 원본은 바꿀 수 없습니다.",
    "PROCUREMENT_IMPORT_NOT_FOUND": "요청한 견적·납품 파일 처리 내역을 찾을 수 없습니다.",
    "PROCUREMENT_MAPPING_REQUIRED": "파일의 열 이름을 연결한 뒤 다시 반영해 주세요.",
    "PROCUREMENT_SOURCE_PROCESSING": "서버에서 파일을 읽고 있습니다. 잠시 뒤 다시 확인해 주세요.",
    "PROCUREMENT_SOURCE_FAILED": "파일을 읽지 못했습니다. 원본 형식과 필수 열을 확인해 주세요.",
    "PROCUREMENT_SOURCE_PARTIAL": "일부 행에 오류가 있어 파일을 반영하지 않았습니다.",
    "PROCUREMENT_FORMAT_NOT_COMPOSABLE": "원본을 보관했습니다. 표 형식으로 변환한 뒤 다시 올려 주세요.",
    "PROCUREMENT_SOURCE_ROWS_INVALID": "수량·가격·품절 열의 값을 확인해 주세요.",
    "COMPARISON_SOURCE_SET_CHANGED": "추천자료 구성이 바뀌었습니다. 최신 자료를 다시 확인해 주세요.",
    "UPLOAD_REPAIR_REQUIRED": "읽지 못한 파일을 다시 올린 뒤 계속해 주세요.",
    "UPLOAD_REPAIR_SUPERSEDED": "더 최근에 선택한 교체 파일이 있어 이 요청을 반영하지 않았습니다.",
    "UPLOAD_REPAIR_ROLE_MISMATCH": "교체 파일의 자료 역할이 원래 파일과 다릅니다.",
    "UPLOAD_REPAIR_CONFIG_MISMATCH": "교체 파일의 자료 범위가 원래 파일과 다릅니다.",
    "SOURCE_REPLACEMENT_SUPERSEDED": "이미 더 최근의 수정 파일이 반영되었습니다.",
    "SOURCE_REPLACEMENT_SHARED": "여러 작업에서 함께 쓰는 자료는 이 화면에서 교체할 수 없습니다.",
    "SOURCE_REPLACEMENT_FILE_REJECTED": "수정 파일을 읽을 수 없습니다. 다른 파일을 골라 주세요.",
    "JOB_NOT_FOUND": "요청한 작업을 찾을 수 없습니다.",
    "UNSUPPORTED_FILE_TYPE": "지원하지 않는 파일 형식입니다.",
    "FILE_TOO_LARGE": "파일 크기 제한을 초과했습니다.",
    "UPLOAD_ITEM_FAILED": "파일을 안전하게 확인할 수 없습니다. 다시 올려 주세요.",
    "LOCAL_ADMIN_CONFIRMATION_REQUIRED": "서버에서 관리자 확인이 필요합니다.",
    "BACKUP_NOT_FOUND": "백업을 찾을 수 없습니다.",
    "RESTORE_VERIFICATION_FAILED": "백업 검증에 실패해 복원하지 않았습니다.",
    "INTEGRITY_CONFLICT": "요청이 현재 데이터와 충돌합니다.",
    "DATABASE_UNAVAILABLE": "데이터베이스 작업을 완료할 수 없습니다. 잠시 후 다시 시도해 주세요.",
}


def public_error_message(code: str) -> str:
    """Return a stable, non-technical message for a public error code."""
    return _MESSAGES.get(code, "요청을 처리할 수 없습니다.")


def request_id_for(request: Request) -> str:
    value = getattr(request.state, "request_id", None)
    if value:
        return value
    header = request.headers.get("X-Request-ID")
    try:
        value = str(uuid.UUID(header)) if header else str(uuid.uuid4())
    except ValueError:
        value = str(uuid.uuid4())
    request.state.request_id = value
    return value


def error_detail(
    code: str,
    *,
    request_id: str,
    fields: list[dict[str, Any]] | None = None,
    message: str | None = None,
) -> dict[str, Any]:
    return {
        "code": code,
        "message": message or public_error_message(code),
        "request_id": request_id,
        "fields": fields or [],
    }


def error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    fields: list[dict[str, Any]] | None = None,
    message: str | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "detail": error_detail(
                code,
                request_id=request_id_for(request),
                fields=fields,
                message=message,
            )
        },
        headers=headers,
    )


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(HTTPException)
    async def http_error(request: Request, error: HTTPException) -> JSONResponse:
        detail = error.detail if isinstance(error.detail, dict) else {}
        code = str(detail.get("code", "HTTP_ERROR"))
        fields = [
            {"field": key, "value": value}
            for key, value in detail.items()
            if key not in {"code", "message", "request_id", "fields", "role"}
        ]
        if detail.get("fields"):
            fields = list(detail["fields"])
        message = detail.get("message")
        if code == "ROLE_REQUIRED" and detail.get("role") == "REVIEWER":
            message = "검토자 권한이 필요합니다."
        return error_response(
            request,
            status_code=error.status_code,
            code=code,
            fields=fields,
            message=message,
            headers=error.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(
        request: Request, error: RequestValidationError
    ) -> JSONResponse:
        fields = []
        for item in error.errors():
            location = ".".join(str(part) for part in item["loc"])
            fields.append({"field": location, "message": item["msg"]})
        return error_response(
            request, status_code=422, code="VALIDATION_ERROR", fields=fields
        )

    @app.exception_handler(WorkflowDomainError)
    async def workflow_error(
        request: Request, error: WorkflowDomainError
    ) -> JSONResponse:
        message = str(error)
        status_code = 403 if error.code.endswith("_ROLE_REQUIRED") else 400
        if error.code.endswith("_NOT_FOUND"):
            status_code = 404
        return error_response(
            request,
            status_code=status_code,
            code=error.code,
            message=None if message == error.code else message,
        )

    @app.exception_handler(RuntimeError)
    async def runtime_error(request: Request, error: RuntimeError) -> JSONResponse:
        message = str(error).casefold()
        if "retried" in message:
            code, status = "JOB_RETRY_NOT_ALLOWED", 409
        elif "cancel" in message:
            code, status = "JOB_CANCEL_NOT_ALLOWED", 409
        elif "claim" in message:
            code, status = "JOB_CLAIM_CONFLICT", 409
        else:
            code, status = "OPERATION_FAILED", 500
        return error_response(
            request, status_code=status, code=code, message=str(error)
        )

    @app.exception_handler(sqlite3.IntegrityError)
    async def integrity_error(
        request: Request, error: sqlite3.IntegrityError
    ) -> JSONResponse:
        if "audit" in str(error).casefold():
            return error_response(
                request,
                status_code=500,
                code="OPERATION_FAILED",
            )
        return error_response(
            request,
            status_code=409,
            code="INTEGRITY_CONFLICT",
        )

    @app.exception_handler(sqlite3.OperationalError)
    @app.exception_handler(sqlite3.ProgrammingError)
    async def database_unavailable(
        request: Request, error: sqlite3.Error
    ) -> JSONResponse:
        return error_response(
            request,
            status_code=503,
            code="DATABASE_UNAVAILABLE",
        )

    @app.exception_handler(OSError)
    async def filesystem_error(request: Request, error: OSError) -> JSONResponse:
        return error_response(
            request,
            status_code=500,
            code="FILESYSTEM_OPERATION_FAILED",
            message=str(error),
        )

    @app.exception_handler(ValueError)
    async def operation_validation_error(
        request: Request, error: ValueError
    ) -> JSONResponse:
        return error_response(
            request,
            status_code=400,
            code="OPERATION_VALIDATION_FAILED",
            message=str(error),
        )


def domain_not_found(code: str) -> HTTPException:
    return HTTPException(status_code=404, detail={"code": code})
