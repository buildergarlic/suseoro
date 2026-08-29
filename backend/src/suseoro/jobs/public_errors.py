"""Stable public job errors that never expose worker or database internals."""

from __future__ import annotations

from typing import Any

_PUBLIC_ERROR_ENVELOPES: dict[str, dict[str, str]] = {
    "JOB_FAILED": {
        "type": "JobFailure",
        "code": "JOB_FAILED",
        "message": "작업을 처리하지 못했습니다. 다시 시도해 주세요.",
    },
    "PARSER_FAILURE": {
        "type": "ParserFailure",
        "code": "PARSER_FAILURE",
        "message": "파일 내용을 읽지 못했습니다. 다시 읽어 주세요.",
    },
    "COMPARISON_FILE_FAILED": {
        "type": "ComparisonFileFailure",
        "code": "COMPARISON_FILE_FAILED",
        "message": "일부 책을 비교하지 못했습니다. 다시 시도해 주세요.",
    },
    "MAPPING_REQUIRED": {
        "type": "MappingRequired",
        "code": "MAPPING_REQUIRED",
        "message": "열 이름과 자료 내용을 확인해 연결해 주세요.",
    },
    "NO_LOGICAL_ROWS": {
        "type": "NoLogicalRows",
        "code": "NO_LOGICAL_ROWS",
        "message": "읽을 수 있는 책 정보가 없습니다. 파일 내용을 확인해 주세요.",
    },
    "ALL_LOGICAL_ROWS_FAILED": {
        "type": "AllLogicalRowsFailed",
        "code": "ALL_LOGICAL_ROWS_FAILED",
        "message": "이 자료의 책을 처리하지 못했습니다. 다시 확인해 주세요.",
    },
}


def sanitize_public_job_error(
    error: object,
    *,
    fallback_code: str = "JOB_FAILED",
) -> dict[str, str] | None:
    """Return only canonical public fields; stored strings are never echoed."""
    if error is None:
        return None
    code = error.get("code") if isinstance(error, dict) else None
    envelope = _PUBLIC_ERROR_ENVELOPES.get(
        code if isinstance(code, str) else "",
        _PUBLIC_ERROR_ENVELOPES[fallback_code],
    )
    return dict(envelope)


def mapping_required_error(mapping_required: dict[str, Any]) -> dict[str, Any]:
    """Persist a current server-produced mapping payload with explicit provenance."""
    return {
        **_PUBLIC_ERROR_ENVELOPES["MAPPING_REQUIRED"],
        "mapping_required": mapping_required,
    }


def sanitize_public_mapping_required(
    error: object,
    mapping_required: object,
    public_mapping_payload_version: object,
) -> dict[str, Any] | None:
    """Suppress unmarked legacy details before schema validation at an API boundary."""
    if (
        not isinstance(error, dict)
        or error.get("code") != "MAPPING_REQUIRED"
        or public_mapping_payload_version != 1
        or not isinstance(mapping_required, dict)
    ):
        return None
    return dict(mapping_required)


def job_failure() -> dict[str, Any]:
    return dict(_PUBLIC_ERROR_ENVELOPES["JOB_FAILED"])


def parser_failure() -> dict[str, Any]:
    return dict(_PUBLIC_ERROR_ENVELOPES["PARSER_FAILURE"])


def comparison_file_failure() -> dict[str, Any]:
    return dict(_PUBLIC_ERROR_ENVELOPES["COMPARISON_FILE_FAILED"])
