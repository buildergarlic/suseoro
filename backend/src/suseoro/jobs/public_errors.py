"""Stable public job errors that never expose worker or database internals."""

from __future__ import annotations

from typing import Any


def job_failure() -> dict[str, Any]:
    return {
        "type": "JobFailure",
        "code": "JOB_FAILED",
        "message": "작업을 처리하지 못했습니다. 다시 시도해 주세요.",
    }


def parser_failure() -> dict[str, Any]:
    return {
        "type": "ParserFailure",
        "code": "PARSER_FAILURE",
        "message": "파일 내용을 읽지 못했습니다. 다시 읽어 주세요.",
    }
