"""Fail-closed public boundaries for response bodies loaded from persistence."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, RootModel, ValidationError

from suseoro.api import schemas
from suseoro.jobs.public_errors import sanitize_public_job_error

_UPLOAD_ERROR_MESSAGES = {
    "UNSUPPORTED_FILE_TYPE": "지원하지 않는 파일 형식입니다.",
    "FILE_TOO_LARGE": "파일 크기 제한을 초과했습니다.",
    "UPLOAD_ITEM_FAILED": "파일을 안전하게 확인할 수 없습니다. 다시 올려 주세요.",
}


class _EmptyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _LoginStoredResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    response: schemas.UserResponse
    session_ciphertext: str
    csrf_ciphertext: str
    expires_at: str


class _ApprovedScopeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str
    candidate_row_version: int
    reapproval_required: bool
    state: str
    row_version: int
    revision_id: str | None


class _CandidateBulkStoredResponse(RootModel[list[schemas.CandidateBulkItem]]):
    """Typed service payload wrapped as ``items`` only at the HTTP boundary."""


class _OrderTemplateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_id: str
    template_version: int
    vendor_name: str
    columns: list[list[str]]
    state: str
    row_version: int


class _ExportArtifactResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: str
    artifact_type: str
    path: str
    sha256: str
    size_bytes: int
    state: str
    row_version: int


_FIXED_ROUTES: dict[str, type[BaseModel]] = {
    "POST /api/v2/workspaces": schemas.WorkspaceResponse,
    "POST /api/v2/auth/login": _LoginStoredResponse,
    "POST /api/v2/auth/logout": _EmptyResponse,
    "POST /api/v2/catalog/deltas": schemas.CatalogDeltaResponse,
    "POST /api/v2/candidates/lock": schemas.CandidateLockResponse,
    "POST /api/v2/admin/v1-migration/run": schemas.V1MigrationReport,
    "POST /api/v2/admin/backups": schemas.BackupManifestResponse,
    "workflow.transition": schemas.WorkspaceStateResponse,
    "candidates.autosave": schemas.CandidateMutationResponse,
    "candidates.bulk": _CandidateBulkStoredResponse,
    "approvals.request": schemas.ApprovalRequestResponse,
    "approvals.comment": schemas.ApprovalCommentResponse,
    "approvals.cancel": schemas.ApprovalCancellationResponse,
    "approvals.approved": schemas.ApprovalDecisionResponse,
    "approvals.rejected": schemas.ApprovalDecisionResponse,
    "approvals.adjust-scope": _ApprovedScopeResponse,
    "quotes.ingest": schemas.QuoteResponse,
    "quotes.confirm_manual_match": schemas.QuoteMatchResponse,
    "orders.templates.save": _OrderTemplateResponse,
    "orders.generate": schemas.OrderResponse,
    "orders.mark-sent": schemas.OrderSentResponse,
    "deliveries.record": schemas.DeliveryResponse,
    "scans.start": schemas.ScanSessionResponse,
    "receiving.disposition": schemas.DifferenceDispositionResponse,
    "receiving.complete": schemas.WorkspaceStateResponse,
    "exports.dls-title.create": _ExportArtifactResponse,
    "exports.dls-isbn.create": _ExportArtifactResponse,
}


def _response_model(route: str) -> type[BaseModel] | None:
    fixed = _FIXED_ROUTES.get(route)
    if fixed is not None:
        return fixed
    if route.startswith("scans.event:"):
        return schemas.ScanResponse
    if route.startswith("POST /api/v2/workspaces/"):
        if route.endswith("/sources"):
            return schemas.UploadResponse
        if route.endswith("/comparison-jobs"):
            return schemas.ComparisonJobResponse
    if route.startswith("PATCH /api/v2/sources/") and route.endswith("/mapping"):
        return schemas.SourceResponse
    if route.startswith("POST /api/v2/sources/"):
        if route.endswith("/parse"):
            return schemas.QueuedJobResponse
        if route.endswith("/catalog/staging"):
            return schemas.CatalogVersionResponse
    if route.startswith("POST /api/v2/catalog/versions/") and route.endswith(
        "/activate"
    ):
        return schemas.CatalogVersionResponse
    if route.startswith("POST /api/v2/jobs/") and route.endswith(("/retry", "/cancel")):
        return schemas.JobCommandResponse
    if route.startswith(
        "POST /api/v2/admin/v1-migration/catalog-candidates/"
    ) and route.endswith("/activate"):
        return schemas.V1ActivationResponse
    return None


def _sanitize_nested(value: object, *, parent: dict[str, Any] | None = None) -> object:
    if isinstance(value, list):
        return [_sanitize_nested(item) for item in value]
    if not isinstance(value, dict):
        return value

    sanitized: dict[str, Any] = {}
    for key, item in value.items():
        if key == "mapping_required":
            # Historical mapping data has no trustworthy provenance marker.
            sanitized[key] = None
            continue
        if key == "error" and item is not None:
            if "filename" in value and "repair_obligation_id" in value:
                code = item.get("code") if isinstance(item, dict) else None
                safe_code = (
                    code
                    if code in {"UNSUPPORTED_FILE_TYPE", "FILE_TOO_LARGE"}
                    else "UPLOAD_ITEM_FAILED"
                )
                sanitized[key] = {
                    "code": safe_code,
                    "message": _UPLOAD_ERROR_MESSAGES[safe_code],
                }
            else:
                sanitized[key] = sanitize_public_job_error(item)
            continue
        sanitized[key] = _sanitize_nested(item, parent=value)
    return sanitized


def _sanitize_upload(body: object) -> dict[str, Any]:
    if not isinstance(body, dict) or not isinstance(body.get("items"), list):
        raise HTTPException(
            status_code=409, detail={"code": "HISTORICAL_REPLAY_INVALID"}
        )
    job_id = body.get("job_id")
    if job_id is not None and not isinstance(job_id, str):
        raise HTTPException(
            status_code=409, detail={"code": "HISTORICAL_REPLAY_INVALID"}
        )
    items: list[dict[str, Any]] = []
    for item in body["items"]:
        if not isinstance(item, dict) or not isinstance(item.get("filename"), str):
            raise HTTPException(
                status_code=409, detail={"code": "HISTORICAL_REPLAY_INVALID"}
            )
        source_id = item.get("source_id")
        if source_id is not None and not isinstance(source_id, str):
            source_id = None
        raw_error = item.get("error")
        if raw_error is None:
            error = None
            status = "ACCEPTED"
        else:
            code = raw_error.get("code") if isinstance(raw_error, dict) else None
            safe_code = (
                code
                if code in {"UNSUPPORTED_FILE_TYPE", "FILE_TOO_LARGE"}
                else "UPLOAD_ITEM_FAILED"
            )
            error = {
                "code": safe_code,
                "message": _UPLOAD_ERROR_MESSAGES[safe_code],
            }
            status = "FAILED"
        repair_id = item.get("repair_obligation_id")
        repair_generation = item.get("repair_generation")
        procurement_import_id = item.get("procurement_import_id")
        items.append(
            {
                "filename": item["filename"],
                "status": status,
                "source_id": source_id,
                "error": error,
                "repair_obligation_id": (
                    repair_id if isinstance(repair_id, str) else None
                ),
                "repair_generation": (
                    repair_generation
                    if isinstance(repair_generation, int)
                    and not isinstance(repair_generation, bool)
                    else None
                ),
                "procurement_import_id": (
                    procurement_import_id
                    if isinstance(procurement_import_id, str)
                    else None
                ),
            }
        )
    return schemas.UploadResponse.model_validate(
        {"job_id": job_id, "items": items}
    ).model_dump(mode="json")


def sanitize_persisted_response(route: str, body: object) -> object:
    """Return only a current, exactly typed, recursively sanitized response."""
    model = _response_model(route)
    if model is None:
        raise HTTPException(
            status_code=409, detail={"code": "HISTORICAL_REPLAY_INVALID"}
        )
    if model is schemas.UploadResponse:
        return _sanitize_upload(body)
    try:
        parsed = model.model_validate(_sanitize_nested(body), extra="ignore")
    except (TypeError, ValueError, ValidationError) as error:
        raise HTTPException(
            status_code=409, detail={"code": "HISTORICAL_REPLAY_INVALID"}
        ) from error
    if model is _CandidateBulkStoredResponse:
        return [
            {
                key: value
                for key, value in item.model_dump(mode="json").items()
                if key != "code" or value is not None
            }
            for item in parsed.root
        ]
    return parsed.model_dump(mode="json")
