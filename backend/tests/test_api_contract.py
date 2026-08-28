from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from suseoro.api.app import create_app
from suseoro.api.schemas import ApiErrorResponse, UploadResponse
from suseoro.backup.service import BackupService
from suseoro.catalog.contracts import CatalogRecord, SourceType
from suseoro.catalog.sync import CatalogSyncService
from suseoro.config import Settings
from suseoro.db.connection import connect
from suseoro.db.migrations import apply_migrations
from suseoro.jobs.handlers import build_job_runner
from suseoro.jobs.repository import JobRepository
from suseoro.repositories.auth import UserRecord, issue_session
from suseoro.security.passwords import hash_password

NOW = "2026-08-28T12:34:56.000000Z"
SCHOOL_ID = "550e8400-e29b-41d4-a716-446655440100"
OTHER_SCHOOL_ID = "550e8400-e29b-41d4-a716-446655440101"
OPERATOR_ID = "550e8400-e29b-41d4-a716-446655440102"
REVIEWER_ID = "550e8400-e29b-41d4-a716-446655440103"
WORKSPACE_ID = "550e8400-e29b-41d4-a716-446655440104"
REQUEST_ID = "550e8400-e29b-41d4-a716-446655440105"


def _seed(settings: Settings) -> None:
    with connect(settings.database_path) as connection:
        apply_migrations(connection)
        connection.executemany(
            """
            INSERT INTO schools (id, name, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                (SCHOOL_ID, "계약 학교", NOW, NOW),
                (OTHER_SCHOOL_ID, "다른 학교", NOW, NOW),
            ),
        )
        password_hash = hash_password("Safe contract 42!")
        connection.executemany(
            """
            INSERT INTO users (
                id, school_id, username, password_hash, display_name,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    OPERATOR_ID,
                    SCHOOL_ID,
                    "operator",
                    password_hash,
                    "담당자",
                    NOW,
                    NOW,
                ),
                (
                    REVIEWER_ID,
                    SCHOOL_ID,
                    "reviewer",
                    password_hash,
                    "검토자",
                    NOW,
                    NOW,
                ),
            ),
        )
        connection.executemany(
            """
            INSERT INTO user_roles (school_id, user_id, role, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                (SCHOOL_ID, OPERATOR_ID, "OPERATOR", NOW),
                (SCHOOL_ID, REVIEWER_ID, "REVIEWER", NOW),
            ),
        )
        connection.execute(
            """
            INSERT INTO acquisition_workspaces (
                id, school_id, name, status, created_by_user_id,
                created_at, updated_at
            ) VALUES (?, ?, '기존 작업', 'CANDIDATE_REVIEW', ?, ?, ?)
            """,
            (WORKSPACE_ID, SCHOOL_ID, OPERATOR_ID, NOW, NOW),
        )
        connection.commit()


def _client(
    data_dir: Path,
    *,
    reviewer: bool = False,
    raise_server_exceptions: bool = True,
    settings_kwargs: dict[str, object] | None = None,
) -> tuple[TestClient, Settings, str]:
    settings = Settings(
        data_dir=data_dir,
        secure_cookies=False,
        **(settings_kwargs or {}),
    )
    _seed(settings)
    user = UserRecord(
        id=REVIEWER_ID if reviewer else OPERATOR_ID,
        school_id=SCHOOL_ID,
        username="reviewer" if reviewer else "operator",
        display_name="검토자" if reviewer else "담당자",
        roles=("REVIEWER",) if reviewer else ("OPERATOR",),
    )
    with connect(settings.database_path) as connection:
        issued = issue_session(connection, user, 3_600)
        connection.commit()
    client = TestClient(
        create_app(settings),
        base_url="https://testserver",
        raise_server_exceptions=raise_server_exceptions,
    )
    client.cookies.set("suseoro_session", issued.session_token)
    client.cookies.set("suseoro_csrf", issued.csrf_token)
    return client, settings, issued.csrf_token


def _headers(csrf: str, key: str, *, version: int | None = None) -> dict[str, str]:
    headers = {
        "X-CSRF-Token": csrf,
        "Idempotency-Key": key,
        "X-Request-ID": REQUEST_ID,
    }
    if version is not None:
        headers["If-Match"] = f'"{version}"'
    return headers


def _source_and_candidates(settings: Settings) -> tuple[str, list[str]]:
    source_file_id = str(uuid.uuid4())
    source_document_id = str(uuid.uuid4())
    candidate_ids: list[str] = []
    with connect(settings.database_path) as connection:
        connection.execute(
            """
            INSERT INTO source_files (
                id, sha256, size_bytes, storage_path, original_filename,
                detected_format, created_at
            ) VALUES (?, ?, 3, 'fixture.csv', 'fixture.csv', 'CSV', ?)
            """,
            (source_file_id, "a" * 64, NOW),
        )
        connection.execute(
            """
            INSERT INTO source_documents (
                id, source_file_id, school_id, role, parser_version, status,
                detected_format, created_at, completed_at
            ) VALUES (?, ?, ?, 'PURCHASE_REQUEST', 'test', 'SUCCESS', 'CSV', ?, ?)
            """,
            (source_document_id, source_file_id, SCHOOL_ID, NOW, NOW),
        )
        for index, outcome in enumerate(
            ("CANDIDATE", "CANDIDATE", "CANDIDATE", "EXCLUDED")
        ):
            source_row_id = str(uuid.uuid4())
            recommendation_id = str(uuid.uuid4())
            candidate_id = f"550e8400-e29b-41d4-a716-4466554402{index:02d}"
            fields = json.dumps(
                {"title": f"책 {index}", "author": "저자", "isbn": None},
                ensure_ascii=False,
            )
            connection.execute(
                """
                INSERT INTO source_rows (
                    id, source_document_id, source_row, status, raw_json,
                    fields_json, warnings_json, created_at
                ) VALUES (?, ?, ?, 'SUCCESS', ?, ?, '[]', ?)
                """,
                (source_row_id, source_document_id, index + 1, fields, fields, NOW),
            )
            connection.execute(
                """
                INSERT INTO recommendations (
                    id, school_id, workspace_id, source_document_id, source_row_id,
                    original_title, original_authors_json, original_json,
                    title_key, subtitle_key, author_key, publisher_key,
                    volume_key, edition_key, series_key, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, '["저자"]', ?, ?, '', '저자', '', '', '', '', ?)
                """,
                (
                    recommendation_id,
                    SCHOOL_ID,
                    WORKSPACE_ID,
                    source_document_id,
                    source_row_id,
                    f"책 {index}",
                    fields,
                    f"책{index}",
                    NOW,
                ),
            )
            connection.execute(
                """
                INSERT INTO candidate_decisions (
                    id, school_id, workspace_id, recommendation_id, outcome,
                    reason, quantity, unit_price, modified_by_user_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'fixture', 1, 10000, ?, ?, ?)
                """,
                (
                    candidate_id,
                    SCHOOL_ID,
                    WORKSPACE_ID,
                    recommendation_id,
                    outcome,
                    OPERATOR_ID,
                    NOW,
                    NOW,
                ),
            )
            candidate_ids.append(candidate_id)
        connection.commit()
    return source_document_id, candidate_ids


def _catalog_source_document(
    settings: Settings,
    *,
    role: str,
    sha_digit: str,
    rows: tuple[dict[str, object], ...],
    status: str = "SUCCESS",
    activation_allowed: bool = True,
    window_start: str | None = None,
    window_end: str | None = None,
    school_id: str = SCHOOL_ID,
) -> str:
    file_id = str(uuid.uuid4())
    document_id = str(uuid.uuid4())
    with connect(settings.database_path) as connection:
        connection.execute(
            """
            INSERT INTO source_files (
                id, sha256, size_bytes, storage_path, original_filename,
                detected_format, created_at
            ) VALUES (?, ?, 10, ?, ?, 'MARC', ?)
            """,
            (
                file_id,
                sha_digit * 64,
                f"fixture-{sha_digit}.mrc",
                f"fixture-{sha_digit}.mrc",
                NOW,
            ),
        )
        connection.execute(
            """
            INSERT INTO source_documents (
                id, source_file_id, school_id, role, parser_version, status,
                detected_format, activation_allowed,
                requested_start_local_date, requested_through_local_date,
                created_at, completed_at
            ) VALUES (?, ?, ?, ?, 'marc-v1', ?, 'MARC', ?, ?, ?, ?, ?)
            """,
            (
                document_id,
                file_id,
                school_id,
                role,
                status,
                int(activation_allowed),
                window_start,
                window_end,
                NOW,
                NOW,
            ),
        )
        for index, row in enumerate(rows, start=1):
            title = row.get("title")
            registration = row.get("registration_number")
            fields = {
                "title": {"value": title},
                "registration_number": {"value": registration},
            }
            connection.execute(
                """
                INSERT INTO source_rows (
                    id, source_document_id, source_row, status, raw_json,
                    fields_json, warnings_json, error_code, error_message,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, '[]', ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    document_id,
                    index,
                    row.get("status", "SUCCESS"),
                    json.dumps(row, ensure_ascii=False),
                    json.dumps(fields, ensure_ascii=False),
                    row.get("error_code"),
                    row.get("error_message"),
                    NOW,
                ),
            )
        connection.commit()
    return document_id


def test_openapi_exposes_complete_stable_korean_v2_contract(data_dir: Path) -> None:
    client, _, _ = _client(data_dir)

    with client:
        schema = client.get("/openapi.json").json()

    expected_operation_ids = {
        "createWorkspace",
        "listWorkspaces",
        "getWorkspace",
        "transitionWorkspace",
        "uploadSources",
        "listSources",
        "getSource",
        "updateSourceMapping",
        "parseSource",
        "getJob",
        "retryJob",
        "cancelJob",
        "listCandidates",
        "updateCandidate",
        "bulkDecideCandidates",
        "lockCandidate",
        "listApprovals",
        "getApproval",
        "requestApproval",
        "cancelApproval",
        "approveApproval",
        "requestApprovalChanges",
        "commentApproval",
        "listQuotes",
        "createQuote",
        "matchQuoteRow",
        "createOrder",
        "downloadOrder",
        "markOrderSent",
        "listDeliveries",
        "createDelivery",
        "startScanSession",
        "recordScan",
        "setReceivingDisposition",
        "completeReceiving",
        "streamEvents",
        "listAuditEvents",
        "inspectV1Migration",
        "runV1Migration",
        "createBackup",
        "listBackups",
        "restoreBackup",
    }
    operations = [
        operation
        for path, path_item in schema["paths"].items()
        if path.startswith("/api/v2")
        for method, operation in path_item.items()
        if method in {"get", "post", "put", "patch", "delete"}
    ]
    operation_ids = [operation["operationId"] for operation in operations]

    assert expected_operation_ids.issubset(operation_ids)
    assert len(operation_ids) == len(set(operation_ids))
    assert all(operation.get("summary") for operation in operations)
    assert all(
        any("가" <= char <= "힣" for char in operation["summary"])
        for operation in operations
    )


def test_login_runtime_headers_match_its_public_openapi_contract(
    data_dir: Path,
) -> None:
    settings = Settings(data_dir=data_dir, secure_cookies=False)
    _seed(settings)
    client = TestClient(create_app(settings), base_url="https://testserver")
    payload = {
        "school_id": SCHOOL_ID,
        "username": "operator",
        "password": "Safe contract 42!",
    }

    with client:
        missing_idempotency = client.post(
            "/api/v2/auth/login",
            headers={"X-Request-ID": REQUEST_ID},
            json=payload,
        )
        missing_request_id = client.post(
            "/api/v2/auth/login",
            headers={"Idempotency-Key": "login-missing-request-id"},
            json=payload,
        )
        accepted = client.post(
            "/api/v2/auth/login",
            headers={
                "Idempotency-Key": "login-complete-headers",
                "X-Request-ID": REQUEST_ID,
            },
            json=payload,
        )
        schema = client.get("/openapi.json").json()

    assert missing_idempotency.status_code == 400
    assert missing_idempotency.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert missing_request_id.status_code == 400
    assert missing_request_id.json()["detail"]["code"] == "REQUEST_ID_REQUIRED"
    assert accepted.status_code == 200
    operation = schema["paths"]["/api/v2/auth/login"]["post"]
    parameters = {
        (parameter["in"], parameter["name"]): parameter
        for parameter in operation.get("parameters", [])
    }
    assert operation["security"] == []
    assert ("header", "X-CSRF-Token") not in parameters
    assert ("cookie", "suseoro_session") not in parameters
    for header in ("Idempotency-Key", "X-Request-ID"):
        assert parameters[("header", header)]["required"] is True
        assert parameters[("header", header)]["schema"] == {"type": "string"}


def test_read_only_v1_inspection_runtime_headers_match_its_openapi_contract(
    data_dir: Path,
) -> None:
    allowed_root = data_dir / "allowed-v1"
    source_root = allowed_root / "legacy-source"
    source_root.mkdir(parents=True)
    client, _, csrf = _client(
        data_dir / "api",
        settings_kwargs={"v1_import_roots": (allowed_root,)},
    )
    confirmation = client.app.state.local_admin_confirmation_token

    with client:
        inspected = client.post(
            "/api/v2/admin/v1-migration/inspect",
            headers={
                "X-CSRF-Token": csrf,
                "X-Local-Admin-Confirmation": confirmation,
            },
            json={"source_path": str(source_root)},
        )
        schema = client.get("/openapi.json").json()

    assert inspected.status_code == 200
    operation = schema["paths"]["/api/v2/admin/v1-migration/inspect"]["post"]
    required_headers = {
        parameter["name"]: parameter
        for parameter in operation.get("parameters", [])
        if parameter["in"] == "header" and parameter.get("required")
    }
    assert operation["security"] == [
        {"SessionCookie": [], "CsrfCookie": [], "CsrfHeader": []}
    ]
    assert set(required_headers) == {
        "X-CSRF-Token",
        "X-Local-Admin-Confirmation",
    }
    assert all(
        parameter["schema"] == {"type": "string"}
        for parameter in required_headers.values()
    )


def test_workspace_mutations_require_operator_csrf_and_replay_idempotently(
    data_dir: Path,
) -> None:
    client, settings, csrf = _client(data_dir)
    headers = _headers(csrf, "create-workspace")

    with client:
        missing_csrf = client.post(
            "/api/v2/workspaces",
            headers={
                key: value for key, value in headers.items() if key != "X-CSRF-Token"
            },
            json={"name": "2026-3차 수서"},
        )
        first = client.post(
            "/api/v2/workspaces", headers=headers, json={"name": "2026-3차 수서"}
        )
        second = client.post(
            "/api/v2/workspaces", headers=headers, json={"name": "2026-3차 수서"}
        )

    assert missing_csrf.status_code == 403
    assert missing_csrf.json()["detail"] == {
        "code": "CSRF_VALIDATION_FAILED",
        "message": "요청을 확인할 수 없습니다. 새로고침 후 다시 시도해 주세요.",
        "request_id": REQUEST_ID,
        "fields": [],
    }
    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json() == first.json()
    assert first.headers["etag"] == '"1"'
    with connect(settings.database_path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM acquisition_workspaces WHERE name = '2026-3차 수서'"
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM audit_events WHERE action = 'WORKSPACE_CREATED'"
            ).fetchone()[0]
            == 1
        )


def test_reviewer_cannot_create_workspace_and_validation_uses_error_envelope(
    data_dir: Path,
) -> None:
    client, _, csrf = _client(data_dir, reviewer=True)

    with client:
        denied = client.post(
            "/api/v2/workspaces",
            headers=_headers(csrf, "reviewer-create"),
            json={"name": "권한 없음"},
        )
        invalid = client.post(
            "/api/v2/workspaces",
            headers=_headers(csrf, "reviewer-invalid"),
            json={"name": ""},
        )

    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "ROLE_REQUIRED"
    assert denied.json()["detail"]["request_id"] == REQUEST_ID
    assert denied.json()["detail"]["message"] == "담당자 권한이 필요합니다."
    assert invalid.status_code in {403, 422}
    if invalid.status_code == 422:
        assert invalid.json()["detail"]["code"] == "VALIDATION_ERROR"
        assert invalid.json()["detail"]["fields"][0]["field"] == "body.name"


def test_workspace_and_candidate_lists_use_stable_filtered_cursor_pages(
    data_dir: Path,
) -> None:
    client, settings, csrf = _client(data_dir)
    _, candidate_ids = _source_and_candidates(settings)
    with client:
        for index in range(3):
            response = client.post(
                "/api/v2/workspaces",
                headers=_headers(csrf, f"workspace-{index}"),
                json={"name": f"추가 작업 {index}"},
            )
            assert response.status_code == 201
        first = client.get("/api/v2/workspaces?status=DRAFT&limit=2")
        second = client.get(
            "/api/v2/workspaces",
            params={
                "status": "DRAFT",
                "limit": 2,
                "cursor": first.json()["next_cursor"],
            },
        )
        candidate_first = client.get(
            f"/api/v2/workspaces/{WORKSPACE_ID}/candidates?outcome=CANDIDATE&limit=2"
        )
        candidate_second = client.get(
            f"/api/v2/workspaces/{WORKSPACE_ID}/candidates",
            params={
                "outcome": "CANDIDATE",
                "limit": 2,
                "cursor": candidate_first.json()["next_cursor"],
            },
        )

    workspace_ids = [
        item["id"] for item in first.json()["items"] + second.json()["items"]
    ]
    paged_candidate_ids = [
        item["id"]
        for item in candidate_first.json()["items"] + candidate_second.json()["items"]
    ]
    assert len(workspace_ids) == 3
    assert len(workspace_ids) == len(set(workspace_ids))
    assert paged_candidate_ids == candidate_ids[:3]
    assert all(
        item["outcome"] == "CANDIDATE" for item in candidate_first.json()["items"]
    )
    assert "items" in candidate_second.json()


def test_candidate_patch_returns_etag_and_structured_412_conflict(
    data_dir: Path,
) -> None:
    client, settings, csrf = _client(data_dir)
    _, candidate_ids = _source_and_candidates(settings)
    candidate_id = candidate_ids[0]

    with client:
        updated = client.patch(
            f"/api/v2/candidates/{candidate_id}",
            headers=_headers(csrf, "candidate-update", version=1),
            json={
                "workspace_id": WORKSPACE_ID,
                "changes": {"quantity": 2},
                "reason": "두 권",
            },
        )
        stale = client.patch(
            f"/api/v2/candidates/{candidate_id}",
            headers=_headers(csrf, "candidate-stale", version=1),
            json={
                "workspace_id": WORKSPACE_ID,
                "changes": {"quantity": 3},
                "reason": "세 권",
            },
        )

    assert updated.status_code == 200
    assert updated.json()["quantity"] == 2
    assert updated.headers["etag"] == '"2"'
    assert stale.status_code == 412
    assert stale.json()["detail"] == {
        "code": "ROW_VERSION_CONFLICT",
        "message": "다른 사용자가 먼저 수정했습니다. 현재 내용과 변경 내용을 확인해 주세요.",
        "request_id": REQUEST_ID,
        "fields": [
            {"field": "current", "value": 2},
            {"field": "submitted", "value": 1},
        ],
    }
    ApiErrorResponse.model_validate(stale.json())


def test_multipart_upload_streams_each_file_and_keeps_partial_success(
    data_dir: Path,
) -> None:
    client, settings, csrf = _client(data_dir)

    with client:
        response = client.post(
            f"/api/v2/workspaces/{WORKSPACE_ID}/sources",
            headers=_headers(csrf, "partial-upload"),
            data={"role": "PURCHASE_REQUEST"},
            files=[
                ("files", ("books.csv", b"title,author\nA,B\n", "text/csv")),
                (
                    "files",
                    ("bad.exe", b"MZ-not-an-allowed-file", "application/octet-stream"),
                ),
            ],
        )

    assert response.status_code == 207
    body = response.json()
    assert set(body["items"][0]) == {"filename", "status", "source_id", "error"}
    assert set(body["items"][1]) == {"filename", "status", "source_id", "error"}
    assert body["items"][0]["error"] is None
    assert body["items"][1]["source_id"] is None
    UploadResponse.model_validate(body)
    assert uuid.UUID(body["job_id"])
    assert [item["status"] for item in body["items"]] == ["ACCEPTED", "FAILED"]
    assert body["items"][0]["source_id"]
    assert body["items"][1]["error"]["code"] == "UNSUPPORTED_FILE_TYPE"
    with connect(settings.database_path) as connection:
        job = connection.execute(
            "SELECT status, payload_json FROM durable_jobs WHERE id = ?",
            (body["job_id"],),
        ).fetchone()
        sources = connection.execute(
            "SELECT COUNT(*) FROM source_documents WHERE school_id = ? AND role = 'PURCHASE_REQUEST'",
            (SCHOOL_ID,),
        ).fetchone()[0]
    assert job["status"] == "QUEUED"
    assert body["items"][0]["source_id"] in job["payload_json"]
    assert sources == 1


def test_upload_runtime_statuses_match_typed_openapi_responses(data_dir: Path) -> None:
    client, _, csrf = _client(
        data_dir,
        settings_kwargs={
            "upload_max_file_bytes": 500,
            "upload_max_batch_bytes": 700,
        },
    )
    valid = b"title,author\nA,B\n"
    partial_valid = b"title,author\nC,D\n"
    aggregate_part = b"title,author\n" + (b"A,B\n" * 95)
    assert len(aggregate_part) < 500
    assert len(aggregate_part) * 2 > 700

    with client:
        accepted = client.post(
            f"/api/v2/workspaces/{WORKSPACE_ID}/sources",
            headers=_headers(csrf, "schema-upload-accepted"),
            data={"role": "PURCHASE_REQUEST"},
            files=[("files", ("accepted.csv", valid, "text/csv"))],
        )
        partial = client.post(
            f"/api/v2/workspaces/{WORKSPACE_ID}/sources",
            headers=_headers(csrf, "schema-upload-partial"),
            data={"role": "PURCHASE_REQUEST"},
            files=[
                ("files", ("partial.csv", partial_valid, "text/csv")),
                (
                    "files",
                    ("rejected.exe", b"MZ-invalid", "application/octet-stream"),
                ),
            ],
        )
        rejected = client.post(
            f"/api/v2/workspaces/{WORKSPACE_ID}/sources",
            headers=_headers(csrf, "schema-upload-aggregate-rejection"),
            data={"role": "PURCHASE_REQUEST"},
            files=[
                ("files", ("aggregate-1.csv", aggregate_part, "text/csv")),
                ("files", ("aggregate-2.csv", aggregate_part, "text/csv")),
            ],
        )
        schema = client.get("/openapi.json").json()

    assert accepted.status_code == 202
    assert partial.status_code == 207
    assert rejected.status_code == 413
    UploadResponse.model_validate(accepted.json())
    UploadResponse.model_validate(partial.json())
    ApiErrorResponse.model_validate(rejected.json())
    assert rejected.json()["detail"]["code"] == "UPLOAD_BATCH_LIMIT_EXCEEDED"

    operations = {
        operation["operationId"]: operation
        for path_item in schema["paths"].values()
        for method, operation in path_item.items()
        if method in {"get", "post", "put", "patch", "delete"}
    }
    expected_custom_successes = {
        "logout": {"204"},
        "createWorkspace": {"201"},
        "createComparisonJob": {"202"},
        "uploadSources": {"202", "207"},
        "parseSource": {"202"},
        "stageCatalogSnapshot": {"201"},
        "retryJob": {"202"},
        "createBackup": {"201"},
    }
    for operation_id, expected in expected_custom_successes.items():
        documented = {
            status
            for status in operations[operation_id]["responses"]
            if status.startswith("2")
        }
        assert documented == expected, operation_id

    upload_responses = operations["uploadSources"]["responses"]
    assert "200" not in upload_responses
    for status in ("202", "207"):
        assert upload_responses[status]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/UploadResponse"
        }
    assert upload_responses["413"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ApiErrorResponse"
    }


def test_uploaded_csv_durable_job_parses_and_persists_rows(data_dir: Path) -> None:
    client, settings, csrf = _client(data_dir)

    with client:
        response = client.post(
            f"/api/v2/workspaces/{WORKSPACE_ID}/sources",
            headers=_headers(csrf, "parse-upload"),
            data={"role": "PURCHASE_REQUEST"},
            files=[("files", ("books.csv", "제목,저자\nA,B\n", "text/csv"))],
        )

    assert response.status_code == 202
    source_id = response.json()["items"][0]["source_id"]
    with connect(settings.database_path) as connection:
        completed = build_job_runner(connection).run_once()
        source = connection.execute(
            "SELECT status, parser_version FROM source_documents WHERE id = ?",
            (source_id,),
        ).fetchone()
        rows = connection.execute(
            "SELECT status, fields_json FROM source_rows WHERE source_document_id = ?",
            (source_id,),
        ).fetchall()

    assert completed is not None and completed.status == "SUCCEEDED"
    assert source["status"] == "SUCCESS"
    assert source["parser_version"] != "v2-api"
    assert len(rows) == 1
    assert json.loads(rows[0]["fields_json"])["title"]["value"] == "A"


def test_source_mapping_and_parse_replay_idempotently(data_dir: Path) -> None:
    client, settings, csrf = _client(data_dir)
    with client:
        uploaded = client.post(
            f"/api/v2/workspaces/{WORKSPACE_ID}/sources",
            headers=_headers(csrf, "mapping-upload"),
            data={"role": "UNKNOWN"},
            files=[("files", ("books.csv", "제목,저자\nA,B\n", "text/csv"))],
        )
        source_id = uploaded.json()["items"][0]["source_id"]
        mapping_headers = _headers(csrf, "mapping-save", version=1)
        mapping_first = client.patch(
            f"/api/v2/sources/{source_id}/mapping",
            headers=mapping_headers,
            json={"role": "PURCHASE_REQUEST", "mapping": {"제목": "title"}},
        )
        mapping_second = client.patch(
            f"/api/v2/sources/{source_id}/mapping",
            headers=mapping_headers,
            json={"role": "PURCHASE_REQUEST", "mapping": {"제목": "title"}},
        )
        parse_headers = _headers(csrf, "parse-source")
        parse_first = client.post(
            f"/api/v2/sources/{source_id}/parse", headers=parse_headers
        )
        parse_second = client.post(
            f"/api/v2/sources/{source_id}/parse", headers=parse_headers
        )

    assert mapping_first.status_code == mapping_second.status_code == 200
    assert mapping_first.json() == mapping_second.json()
    assert mapping_second.headers["etag"] == '"2"'
    assert parse_first.status_code == parse_second.status_code == 202
    assert parse_first.json() == parse_second.json()
    with connect(settings.database_path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM durable_jobs WHERE job_type = 'PARSE'"
            ).fetchone()[0]
            == 1
        )


def test_generated_openapi_artifact_matches_the_tested_application(
    data_dir: Path,
) -> None:
    client, _, _ = _client(data_dir)
    artifact = Path(__file__).parents[1] / "openapi.json"

    with client:
        live = client.get("/openapi.json").json()

    assert artifact.is_file()
    assert json.loads(artifact.read_text(encoding="utf-8")) == live


def test_catalog_activation_and_comparison_complete_the_acquisition_path(
    data_dir: Path,
) -> None:
    client, settings, csrf = _client(data_dir)
    with client:
        created = client.post(
            "/api/v2/workspaces",
            headers=_headers(csrf, "composition-workspace"),
            json={"name": "실제 비교 작업"},
        )
        workspace_id = created.json()["id"]
        catalog_upload = client.post(
            f"/api/v2/workspaces/{workspace_id}/sources",
            headers=_headers(csrf, "catalog-upload"),
            data={"role": "CATALOG_FULL"},
            files=[
                (
                    "files",
                    (
                        "holdings.csv",
                        "등록번호,제목,ISBN\nR-1,이미 있음,9780306406157\n",
                        "text/csv",
                    ),
                )
            ],
        )
    catalog_source_id = catalog_upload.json()["items"][0]["source_id"]
    with connect(settings.database_path) as connection:
        assert build_job_runner(connection).run_once().status == "SUCCEEDED"

    with client:
        staged = client.post(
            f"/api/v2/sources/{catalog_source_id}/catalog/staging",
            headers=_headers(csrf, "catalog-stage"),
            json={"source_type": "DLS_EXCEL"},
        )
        assert staged.status_code == 201
        activated = client.post(
            f"/api/v2/catalog/versions/{staged.json()['id']}/activate",
            headers=_headers(csrf, "catalog-activate"),
            json={"confirm_anomaly": False},
        )
        assert activated.status_code == 200
        recommendation_upload = client.post(
            f"/api/v2/workspaces/{workspace_id}/sources",
            headers=_headers(csrf, "recommendation-upload"),
            data={"role": "PURCHASE_REQUEST"},
            files=[
                (
                    "files",
                    (
                        "recommendations.csv",
                        "제목,저자,ISBN\n이미 있음,저자,9780306406157\n새 책,새 저자,\n",
                        "text/csv",
                    ),
                )
            ],
        )
    recommendation_id = recommendation_upload.json()["items"][0]["source_id"]
    with connect(settings.database_path) as connection:
        assert build_job_runner(connection).run_once().status == "SUCCEEDED"

    with client:
        comparison = client.post(
            f"/api/v2/workspaces/{workspace_id}/comparison-jobs",
            headers=_headers(csrf, "comparison-start", version=1),
            json={"source_document_ids": [recommendation_id]},
        )
        before_run = client.get(f"/api/v2/workspaces/{workspace_id}")
    assert comparison.status_code == 202
    assert before_run.json()["status"] == "ANALYZING"
    with connect(settings.database_path) as connection:
        completed = build_job_runner(connection).run_once()
        assert completed is not None and completed.status == "SUCCEEDED"
    with client:
        after_run = client.get(f"/api/v2/workspaces/{workspace_id}")
        candidates = client.get(
            f"/api/v2/workspaces/{workspace_id}/candidates?limit=10"
        )
    assert after_run.json()["status"] == "CANDIDATE_REVIEW"
    assert {item["outcome"] for item in candidates.json()["items"]} == {
        "CANDIDATE",
        "EXCLUDED",
    }


def test_delta_source_upload_requires_and_persists_exact_inclusive_window(
    data_dir: Path,
) -> None:
    client, settings, csrf = _client(data_dir)
    files = [("files", ("delta.csv", "등록번호,제목\nR-1,책\n", "text/csv"))]
    with client:
        missing = client.post(
            f"/api/v2/workspaces/{WORKSPACE_ID}/sources",
            headers=_headers(csrf, "delta-missing-window"),
            data={"role": "CATALOG_DELTA_REGISTRATION"},
            files=files,
        )
        valid = client.post(
            f"/api/v2/workspaces/{WORKSPACE_ID}/sources",
            headers=_headers(csrf, "delta-valid-window"),
            data={
                "role": "CATALOG_DELTA_REGISTRATION",
                "requested_start_local_date": "2026-08-18",
                "requested_through_local_date": "2026-08-28",
            },
            files=files,
        )

    assert missing.status_code == 422
    assert missing.json()["detail"]["code"] == "DELTA_WINDOW_REQUIRED"
    assert valid.status_code == 202
    source_id = valid.json()["items"][0]["source_id"]
    with connect(settings.database_path) as connection:
        row = connection.execute(
            """
            SELECT requested_start_local_date, requested_through_local_date
            FROM source_documents WHERE id = ?
            """,
            (source_id,),
        ).fetchone()
    assert tuple(row) == ("2026-08-18", "2026-08-28")


def test_catalog_full_staging_rejects_non_full_partial_and_activation_blocked_sources(
    data_dir: Path,
) -> None:
    client, settings, csrf = _client(data_dir)
    with client:
        recommendation = client.post(
            f"/api/v2/workspaces/{WORKSPACE_ID}/sources",
            headers=_headers(csrf, "catalog-role-rejection"),
            data={"role": "PURCHASE_REQUEST"},
            files=[
                (
                    "files",
                    ("recommendation.csv", "제목,저자\n추천 책,저자\n", "text/csv"),
                )
            ],
        )
    recommendation_id = recommendation.json()["items"][0]["source_id"]
    with connect(settings.database_path) as connection:
        assert build_job_runner(connection).run_once() is not None
    partial_id = _catalog_source_document(
        settings,
        role="CATALOG_FULL",
        sha_digit="5",
        rows=(
            {"registration_number": "R-1", "title": "정상 장서"},
            {
                "status": "ROW_ERROR",
                "error_code": "INVALID_ROW",
                "error_message": "제목 누락",
            },
        ),
        status="ROW_ERROR",
        activation_allowed=True,
    )
    blocked_id = _catalog_source_document(
        settings,
        role="CATALOG_FULL",
        sha_digit="6",
        rows=({"registration_number": "R-2", "title": "차단 장서"},),
        status="SUCCESS",
        activation_allowed=False,
    )

    with client:
        wrong_role = client.post(
            f"/api/v2/sources/{recommendation_id}/catalog/staging",
            headers=_headers(csrf, "catalog-wrong-role-stage"),
            json={"source_type": "DLS_EXCEL"},
        )
        partial_parse = client.post(
            f"/api/v2/sources/{partial_id}/catalog/staging",
            headers=_headers(csrf, "catalog-partial-stage"),
            json={"source_type": "DLS_MARC"},
        )
        activation_blocked = client.post(
            f"/api/v2/sources/{blocked_id}/catalog/staging",
            headers=_headers(csrf, "catalog-activation-blocked-stage"),
            json={"source_type": "DLS_MARC"},
        )

    assert (
        wrong_role.status_code
        == partial_parse.status_code
        == activation_blocked.status_code
        == 422
    )
    assert wrong_role.json()["detail"]["code"] == "CATALOG_VALIDATION_FAILED"
    assert partial_parse.json()["detail"]["code"] == "CATALOG_VALIDATION_FAILED"
    assert activation_blocked.json()["detail"]["code"] == "CATALOG_VALIDATION_FAILED"
    with connect(settings.database_path) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM catalog_versions").fetchone()[0]
            == 0
        )


def test_catalog_delta_api_applies_distinct_inclusive_registration_and_update_files(
    data_dir: Path,
) -> None:
    client, settings, csrf = _client(data_dir)
    with connect(settings.database_path) as connection:
        CatalogSyncService(
            connection, _allow_unbound_sources=True
        ).import_full_snapshot(
            school_id=SCHOOL_ID,
            source_type=SourceType.DLS_MARC,
            records=(CatalogRecord(source_item_id="BASE-1", title="기준 장서"),),
            confirm_anomaly=True,
            as_of_date=date(2026, 8, 20),
            _allow_unbound_source=True,
        )
        connection.commit()
    registration_id = _catalog_source_document(
        settings,
        role="CATALOG_DELTA_REGISTRATION",
        sha_digit="3",
        rows=({"registration_number": "NEW-1", "title": "신규 장서"},),
        window_start="2026-08-18",
        window_end="2026-08-28",
    )
    update_id = _catalog_source_document(
        settings,
        role="CATALOG_DELTA_UPDATE",
        sha_digit="4",
        rows=({"registration_number": "BASE-1", "title": "갱신 장서"},),
        window_start="2026-08-18",
        window_end="2026-08-28",
    )

    with client:
        applied = client.post(
            "/api/v2/catalog/deltas",
            headers=_headers(csrf, "apply-catalog-delta"),
            json={
                "source_type": "DLS_MARC",
                "registration_source_id": registration_id,
                "update_source_id": update_id,
                "requested_start_local_date": "2026-08-18",
                "requested_through_local_date": "2026-08-28",
            },
        )

    assert applied.status_code == 200
    assert applied.json()["status"] == "SUCCESS"
    assert applied.json()["watermark_local_date"] == "2026-08-28"
    with connect(settings.database_path) as connection:
        state = connection.execute(
            "SELECT watermark_local_date FROM catalog_source_state WHERE school_id = ?",
            (SCHOOL_ID,),
        ).fetchone()
        active_titles = {
            row["original_title"]
            for row in connection.execute(
                """
                SELECT holding.original_title
                FROM holdings holding JOIN catalog_versions version
                  ON version.id = holding.catalog_version_id
                WHERE version.school_id = ? AND version.status = 'ACTIVE'
                """,
                (SCHOOL_ID,),
            )
        }
    assert state["watermark_local_date"] == "2026-08-28"
    assert active_titles == {"갱신 장서", "신규 장서"}


@pytest.mark.parametrize(
    ("document_status", "activation_allowed", "rows"),
    [
        (
            "ROW_ERROR",
            True,
            (
                {"registration_number": "NEW-1", "title": "신규 장서"},
                {
                    "registration_number": "BROKEN-1",
                    "title": "오류 장서",
                    "status": "ROW_ERROR",
                    "error_code": "INVALID_ROW",
                },
            ),
        ),
        ("SUCCESS", True, ()),
        (
            "SUCCESS",
            True,
            (
                {"registration_number": "NEW-1", "title": "신규 장서"},
                {
                    "registration_number": "BROKEN-1",
                    "title": "오류 장서",
                    "status": "ROW_ERROR",
                    "error_code": "INVALID_ROW",
                },
            ),
        ),
        (
            "SUCCESS",
            False,
            ({"registration_number": "NEW-1", "title": "차단 장서"},),
        ),
    ],
    ids=("row-error-document", "empty", "partial-rows", "activation-blocked"),
)
def test_delta_batch_records_partial_without_applying_or_advancing_watermark(
    data_dir: Path,
    document_status: str,
    activation_allowed: bool,
    rows: tuple[dict[str, object], ...],
) -> None:
    client, settings, csrf = _client(data_dir)
    with connect(settings.database_path) as connection:
        CatalogSyncService(
            connection, _allow_unbound_sources=True
        ).import_full_snapshot(
            school_id=SCHOOL_ID,
            source_type=SourceType.DLS_MARC,
            records=(CatalogRecord(source_item_id="BASE-1", title="기준 장서"),),
            confirm_anomaly=True,
            as_of_date=date(2026, 8, 20),
            _allow_unbound_source=True,
        )
        connection.commit()
    registration_id = _catalog_source_document(
        settings,
        role="CATALOG_DELTA_REGISTRATION",
        sha_digit="7",
        rows=rows,
        status=document_status,
        activation_allowed=activation_allowed,
        window_start="2026-08-18",
        window_end="2026-08-28",
    )
    update_id = _catalog_source_document(
        settings,
        role="CATALOG_DELTA_UPDATE",
        sha_digit="8",
        rows=({"registration_number": "BASE-1", "title": "갱신 금지"},),
        window_start="2026-08-18",
        window_end="2026-08-28",
    )
    request = {
        "source_type": "DLS_MARC",
        "registration_source_id": registration_id,
        "update_source_id": update_id,
        "requested_start_local_date": "2026-08-18",
        "requested_through_local_date": "2026-08-28",
    }

    with client:
        first = client.post(
            "/api/v2/catalog/deltas",
            headers=_headers(csrf, "reject-unsafe-delta"),
            json=request,
        )
        replay = client.post(
            "/api/v2/catalog/deltas",
            headers=_headers(csrf, "reject-unsafe-delta"),
            json=request,
        )

    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json()
    assert first.json()["applied"] is False
    assert first.json()["status"] == "PARTIAL_FAILURE"
    assert first.json()["watermark_local_date"] == "2026-08-20"
    with connect(settings.database_path) as connection:
        state = connection.execute(
            "SELECT watermark_local_date FROM catalog_source_state WHERE school_id = ?",
            (SCHOOL_ID,),
        ).fetchone()
        batches = connection.execute(
            "SELECT status FROM catalog_delta_batches WHERE school_id = ?",
            (SCHOOL_ID,),
        ).fetchall()
        active_titles = {
            row["original_title"]
            for row in connection.execute(
                """
                SELECT holding.original_title
                FROM holdings holding JOIN catalog_versions version
                  ON version.id = holding.catalog_version_id
                WHERE version.school_id = ? AND version.status = 'ACTIVE'
                """,
                (SCHOOL_ID,),
            )
        }
        delta_versions = connection.execute(
            """
            SELECT COUNT(*) FROM catalog_versions
            WHERE school_id = ? AND import_mode = 'DELTA'
            """,
            (SCHOOL_ID,),
        ).fetchone()[0]
        applied_row_results = connection.execute(
            """
            SELECT COUNT(*) FROM catalog_delta_row_results result
            JOIN catalog_delta_batches batch ON batch.id = result.batch_id
            WHERE batch.school_id = ? AND result.outcome = 'APPLIED'
            """,
            (SCHOOL_ID,),
        ).fetchone()[0]
    assert state["watermark_local_date"] == "2026-08-20"
    assert [row["status"] for row in batches] == ["PARTIAL_FAILURE"]
    assert active_titles == {"기준 장서"}
    assert delta_versions == 0
    assert applied_row_results == 0


def test_saved_mapping_template_and_parser_cache_are_used_by_durable_retries(
    data_dir: Path,
) -> None:
    client, settings, csrf = _client(data_dir)
    with client:
        uploaded = client.post(
            f"/api/v2/workspaces/{WORKSPACE_ID}/sources",
            headers=_headers(csrf, "custom-map-upload"),
            data={"role": "PURCHASE_REQUEST"},
            files=[
                (
                    "files",
                    ("custom.csv", "책이름,쓴사람\n첫 책,첫 저자\n", "text/csv"),
                )
            ],
        )
        source_id = uploaded.json()["items"][0]["source_id"]
        mapped = client.patch(
            f"/api/v2/sources/{source_id}/mapping",
            headers=_headers(csrf, "custom-map-save", version=1),
            json={
                "role": "PURCHASE_REQUEST",
                "mapping": {"책이름": "title", "쓴사람": "author"},
                "remember_template": True,
                "vendor_scope": "vendor-a",
            },
        )
        reparsed = client.post(
            f"/api/v2/sources/{source_id}/parse",
            headers=_headers(csrf, "custom-map-retry"),
        )
    assert mapped.status_code == 200
    assert reparsed.status_code == 202
    with connect(settings.database_path) as connection:
        runner = build_job_runner(connection)
        assert runner.run_once().status == "SUCCEEDED"
        assert runner.run_once().status == "SUCCEEDED"
        first_fields = json.loads(
            connection.execute(
                "SELECT fields_json FROM source_rows WHERE source_document_id = ?",
                (source_id,),
            ).fetchone()["fields_json"]
        )
        parser_runs = connection.execute(
            "SELECT COUNT(*) FROM parser_runs WHERE role = 'PURCHASE_REQUEST'"
        ).fetchone()[0]
        templates = connection.execute(
            "SELECT COUNT(*) FROM mapping_templates WHERE school_id = ?",
            (SCHOOL_ID,),
        ).fetchone()[0]
    assert first_fields["title"]["value"] == "첫 책"
    assert parser_runs == 1
    assert templates == 1

    with client:
        remembered = client.post(
            f"/api/v2/workspaces/{WORKSPACE_ID}/sources",
            headers=_headers(csrf, "remembered-map-upload"),
            data={"role": "PURCHASE_REQUEST", "vendor_scope": "vendor-a"},
            files=[
                (
                    "files",
                    (
                        "custom-2.csv",
                        "책이름,쓴사람\n둘째 책,둘째 저자\n셋째 책,셋째 저자\n",
                        "text/csv",
                    ),
                )
            ],
        )
    remembered_id = remembered.json()["items"][0]["source_id"]
    with connect(settings.database_path) as connection:
        assert build_job_runner(connection).run_once().status == "SUCCEEDED"
        titles = [
            json.loads(row["fields_json"])["title"]["value"]
            for row in connection.execute(
                """
                SELECT fields_json FROM source_rows
                WHERE source_document_id = ? ORDER BY source_row
                """,
                (remembered_id,),
            ).fetchall()
        ]
    assert titles == ["둘째 책", "셋째 책"]


def test_ingestion_job_exposes_truthful_partial_per_file_results(
    data_dir: Path,
) -> None:
    client, settings, csrf = _client(data_dir)
    with client:
        uploaded = client.post(
            f"/api/v2/workspaces/{WORKSPACE_ID}/sources",
            headers=_headers(csrf, "partial-job-items"),
            data={"role": "PURCHASE_REQUEST"},
            files=[
                ("files", ("good.csv", "제목,저자\n정상,저자\n", "text/csv")),
                ("files", ("bad.csv", "foo,bar\n값,값\n", "text/csv")),
            ],
        )
    job_id = uploaded.json()["job_id"]
    with connect(settings.database_path) as connection:
        completed = build_job_runner(connection).run_once()
    assert completed is not None and completed.status == "PARTIAL"
    with client:
        job = client.get(f"/api/v2/jobs/{job_id}")
    assert [item["status"] for item in job.json()["items"]] == [
        "FAILED",
        "SUCCESS",
    ]
    assert all(
        item["processed_rows"] == item["total_rows"] for item in job.json()["items"]
    )


def test_partial_compare_is_retryable_and_does_not_advance_workspace(
    data_dir: Path,
) -> None:
    client, settings, csrf = _client(data_dir)
    with connect(settings.database_path) as connection:
        CatalogSyncService(
            connection, _allow_unbound_sources=True
        ).import_full_snapshot(
            school_id=SCHOOL_ID,
            source_type=SourceType.DLS_EXCEL,
            records=(CatalogRecord(source_item_id="BASE", title="기준"),),
            confirm_anomaly=True,
            _allow_unbound_source=True,
        )
        connection.commit()
    with client:
        workspace = client.post(
            "/api/v2/workspaces",
            headers=_headers(csrf, "partial-compare-workspace"),
            json={"name": "부분 비교 복구"},
        )
        workspace_id = workspace.json()["id"]
        uploaded = client.post(
            f"/api/v2/workspaces/{workspace_id}/sources",
            headers=_headers(csrf, "partial-compare-source"),
            data={"role": "PURCHASE_REQUEST"},
            files=[
                (
                    "files",
                    (
                        "partial.csv",
                        "제목,저자\n정상 추천,저자\n,누락\n",
                        "text/csv",
                    ),
                )
            ],
        )
    source_id = uploaded.json()["items"][0]["source_id"]
    with connect(settings.database_path) as connection:
        ingested = build_job_runner(connection).run_once()
        assert ingested is not None and ingested.status == "PARTIAL"
    with client:
        queued = client.post(
            f"/api/v2/workspaces/{workspace_id}/comparison-jobs",
            headers=_headers(csrf, "partial-compare-start", version=1),
            json={"source_document_ids": [source_id]},
        )
    compare_job_id = queued.json()["job_id"]
    with connect(settings.database_path) as connection:
        compared = build_job_runner(connection).run_once()
    with client:
        current_workspace = client.get(f"/api/v2/workspaces/{workspace_id}")
        retried = client.post(
            f"/api/v2/jobs/{compare_job_id}/retry",
            headers=_headers(csrf, "partial-compare-retry"),
        )

    assert compared is not None and compared.status == "PARTIAL"
    assert current_workspace.json()["status"] == "ANALYZING"
    assert retried.status_code == 202
    assert retried.json()["status"] == "QUEUED"


def test_empty_ingestion_is_failed_with_a_durable_zero_row_item(
    data_dir: Path,
) -> None:
    client, settings, csrf = _client(data_dir)
    with client:
        uploaded = client.post(
            f"/api/v2/workspaces/{WORKSPACE_ID}/sources",
            headers=_headers(csrf, "empty-ingestion"),
            data={"role": "PURCHASE_REQUEST"},
            files=[("files", ("empty.csv", "제목,저자\n", "text/csv"))],
        )
    job_id = uploaded.json()["job_id"]
    with connect(settings.database_path) as connection:
        completed = build_job_runner(connection).run_once()
    with client:
        job = client.get(f"/api/v2/jobs/{job_id}")

    assert completed is not None and completed.status == "FAILED"
    assert job.json()["status"] == "FAILED"
    assert job.json()["items"] == [
        {
            "source_document_id": uploaded.json()["items"][0]["source_id"],
            "filename": "empty.csv",
            "status": "FAILED",
            "total_rows": 0,
            "processed_rows": 0,
            "error": {"code": "NO_LOGICAL_ROWS"},
        }
    ]


def test_source_and_audit_cursor_lists_use_configured_role_and_qualified_keys(
    data_dir: Path,
) -> None:
    client, _, csrf = _client(data_dir)
    with client:
        uploaded = client.post(
            f"/api/v2/workspaces/{WORKSPACE_ID}/sources",
            headers=_headers(csrf, "list-source-role"),
            data={"role": "UNKNOWN"},
            files=[("files", ("list.csv", "제목\n책\n", "text/csv"))],
        )
        source_id = uploaded.json()["items"][0]["source_id"]
        client.patch(
            f"/api/v2/sources/{source_id}/mapping",
            headers=_headers(csrf, "list-source-map", version=1),
            json={"role": "PURCHASE_REQUEST", "mapping": {"제목": "title"}},
        )
        sources = client.get(f"/api/v2/workspaces/{WORKSPACE_ID}/sources")
        first = client.get("/api/v2/audit?limit=1")
        second = client.get(
            "/api/v2/audit",
            params={"limit": 1, "cursor": first.json()["next_cursor"]},
        )

    assert sources.status_code == 200
    assert sources.json()["items"][0]["role"] == "PURCHASE_REQUEST"
    assert first.status_code == second.status_code == 200
    assert first.json()["items"][0]["id"] != second.json()["items"][0]["id"]


def test_openapi_has_typed_json_responses_errors_and_required_mutation_headers(
    data_dir: Path,
) -> None:
    client, _, _ = _client(data_dir)
    with client:
        schema = client.get("/openapi.json").json()

    for path, path_item in schema["paths"].items():
        for method, operation in path_item.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            success = next(
                response
                for status, response in operation["responses"].items()
                if status.startswith("2")
            )
            if path.endswith("/download") or operation["operationId"] == "streamEvents":
                continue
            if "content" not in success:
                continue
            success_schema = success["content"]["application/json"]["schema"]
            assert success_schema.get("$ref"), operation["operationId"]
            if "422" in operation["responses"]:
                assert (
                    operation["responses"]["422"]["content"]["application/json"][
                        "schema"
                    ]["$ref"]
                    == "#/components/schemas/ApiErrorResponse"
                )
            if (
                method in {"post", "put", "patch", "delete"}
                and operation["operationId"] != "login"
            ):
                required_headers = {
                    parameter["name"]
                    for parameter in operation.get("parameters", [])
                    if parameter["in"] == "header" and parameter.get("required")
                }
                assert "X-CSRF-Token" in required_headers

    components = schema["components"]["schemas"]

    def resolved(response_schema: dict[str, object]) -> dict[str, object]:
        reference = response_schema.get("$ref")
        assert isinstance(reference, str) and reference.startswith(
            "#/components/schemas/"
        )
        return components[reference.rsplit("/", 1)[-1]]

    for path_item in schema["paths"].values():
        for method, operation in path_item.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            if operation["operationId"] == "streamEvents":
                continue
            for status, success in operation["responses"].items():
                if not status.startswith("2") or "content" not in success:
                    continue
                content = success["content"].get("application/json")
                if content is None:
                    continue
                model = resolved(content["schema"])
                assert model.get("type") == "object", operation["operationId"]
                assert model.get("properties"), operation["operationId"]
                assert model.get("required"), operation["operationId"]
                assert model.get("additionalProperties") is not True

    workspace_model = resolved(
        schema["paths"]["/api/v2/workspaces"]["post"]["responses"]["201"]["content"][
            "application/json"
        ]["schema"]
    )
    assert {"id", "name", "status", "row_version"} <= set(workspace_model["required"])
    assert workspace_model["properties"]["row_version"]["type"] == "integer"

    job_model = resolved(
        schema["paths"]["/api/v2/jobs/{job_id}"]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]
    )
    assert {
        "id",
        "type",
        "status",
        "stage",
        "progress_current",
        "items",
    } <= set(job_model["required"])
    assert job_model["properties"]["items"]["type"] == "array"

    error_detail = components["ApiErrorDetail"]
    error_field = resolved(error_detail["properties"]["fields"]["items"])
    assert {"field", "message"} <= set(error_field["properties"])

    delta_path = schema["paths"].get("/api/v2/catalog/deltas")
    assert delta_path is not None
    delta_request_reference = delta_path["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]["$ref"]
    delta_request = components[delta_request_reference.rsplit("/", 1)[-1]]
    assert {
        "source_type",
        "registration_source_id",
        "update_source_id",
        "requested_start_local_date",
        "requested_through_local_date",
    } <= set(delta_request["required"])


def test_openapi_security_and_every_reachable_nested_json_schema_are_concrete(
    data_dir: Path,
) -> None:
    client, _, _ = _client(data_dir)
    with client:
        schema = client.get("/openapi.json").json()

    schemes = schema["components"]["securitySchemes"]
    assert schemes == {
        "SessionCookie": {
            "type": "apiKey",
            "in": "cookie",
            "name": "suseoro_session",
        },
        "CsrfCookie": {
            "type": "apiKey",
            "in": "cookie",
            "name": "suseoro_csrf",
        },
        "CsrfHeader": {
            "type": "apiKey",
            "in": "header",
            "name": "X-CSRF-Token",
        },
    }
    assert schema["paths"]["/api/v2/health"]["get"]["security"] == []
    assert schema["paths"]["/api/v2/auth/login"]["post"]["security"] == []
    assert schema["paths"]["/api/v2/workspaces"]["get"]["security"] == [
        {"SessionCookie": []}
    ]
    assert schema["paths"]["/api/v2/workspaces"]["post"]["security"] == [
        {"SessionCookie": [], "CsrfCookie": [], "CsrfHeader": []}
    ]
    mutation_parameters = {
        item["name"]: item
        for item in schema["paths"]["/api/v2/candidates/{candidate_id}"]["patch"][
            "parameters"
        ]
        if item["in"] == "header"
    }
    for header in ("If-Match", "Idempotency-Key", "X-CSRF-Token", "X-Request-ID"):
        assert mutation_parameters[header]["required"] is True
        assert mutation_parameters[header]["schema"] == {"type": "string"}

    components = schema["components"]["schemas"]
    visited: set[str] = set()

    def assert_concrete(node: object, location: str) -> None:
        assert isinstance(node, dict), location
        reference = node.get("$ref")
        if reference is not None:
            assert reference.startswith("#/components/schemas/"), location
            name = reference.rsplit("/", 1)[-1]
            if name not in visited:
                visited.add(name)
                assert_concrete(components[name], f"component:{name}")
            return
        assert node, f"untyped schema at {location}"
        assert node.get("additionalProperties") is not True, location
        for keyword in ("properties",):
            for name, child in node.get(keyword, {}).items():
                assert_concrete(child, f"{location}.{name}")
        for keyword in ("items", "additionalProperties"):
            child = node.get(keyword)
            if isinstance(child, dict):
                assert_concrete(child, f"{location}.{keyword}")
        for keyword in ("anyOf", "oneOf", "allOf", "prefixItems"):
            for index, child in enumerate(node.get(keyword, [])):
                assert_concrete(child, f"{location}.{keyword}[{index}]")

    for path, path_item in schema["paths"].items():
        for method, operation in path_item.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            for parameter in operation.get("parameters", []):
                assert_concrete(
                    parameter["schema"],
                    f"{method.upper()} {path} parameter {parameter['name']}",
                )
            for content_type, media in (
                operation.get("requestBody", {}).get("content", {}).items()
            ):
                assert_concrete(
                    media["schema"], f"{method.upper()} {path} request {content_type}"
                )
            for status, response in operation["responses"].items():
                for content_type, media in response.get("content", {}).items():
                    if operation["operationId"] == "streamEvents" and status.startswith(
                        "2"
                    ):
                        continue
                    assert_concrete(
                        media["schema"],
                        f"{method.upper()} {path} response {status} {content_type}",
                    )

    error_field = components["ApiErrorField"]
    assert set(error_field["required"]) == {"field"}
    assert {"message", "value"} <= set(error_field["properties"])


def test_openapi_common_error_statuses_follow_runtime_dependency_surfaces(
    data_dir: Path,
) -> None:
    client, _, _ = _client(data_dir)
    with client:
        schema = client.get("/openapi.json").json()

    operations = {
        operation["operationId"]: (method, operation)
        for path_item in schema["paths"].values()
        for method, operation in path_item.items()
        if method in {"get", "post", "put", "patch", "delete"}
    }
    if_match_operations = {
        "transitionWorkspace",
        "createComparisonJob",
        "updateSourceMapping",
        "updateCandidate",
        "requestApproval",
        "cancelApproval",
        "approveApproval",
        "requestApprovalChanges",
        "commentApproval",
        "createQuote",
        "matchQuoteRow",
        "createOrder",
        "markOrderSent",
        "createDelivery",
        "startScanSession",
        "setReceivingDisposition",
        "completeReceiving",
    }
    idempotent_operations = {
        "login",
        "logout",
        "createWorkspace",
        "transitionWorkspace",
        "createComparisonJob",
        "uploadSources",
        "updateSourceMapping",
        "parseSource",
        "stageCatalogSnapshot",
        "activateCatalogVersion",
        "applyCatalogDelta",
        "retryJob",
        "cancelJob",
        "updateCandidate",
        "bulkDecideCandidates",
        "lockCandidate",
        "requestApproval",
        "cancelApproval",
        "approveApproval",
        "requestApprovalChanges",
        "commentApproval",
        "createQuote",
        "matchQuoteRow",
        "createOrder",
        "markOrderSent",
        "createDelivery",
        "startScanSession",
        "recordScan",
        "setReceivingDisposition",
        "completeReceiving",
        "runV1Migration",
        "activateV1CatalogCandidate",
        "createBackup",
        "restoreBackup",
    }
    unsafe_methods = {"post", "put", "patch", "delete"}

    for operation_id, (method, operation) in operations.items():
        responses = operation["responses"]
        assert responses["default"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/ApiErrorResponse"
        }
        assert ("400" in responses) is (operation_id in idempotent_operations)
        assert ("412" in responses) is (operation_id in if_match_operations)
        assert ("428" in responses) is (operation_id in if_match_operations)
        assert ("409" in responses) is (operation_id in idempotent_operations)
        assert ("401" in responses) is (operation_id != "getHealth")
        assert ("403" in responses) is (
            (method in unsafe_methods and operation_id != "login")
            or operation_id == "listBackups"
        )
        assert ("422" in responses) is (operation_id != "getHealth")
        assert ("503" in responses) is (operation_id != "getHealth")
    assert set(operations["getHealth"][1]["responses"]) == {"200", "default"}


def test_job_runtime_and_domain_role_errors_keep_structured_status_codes(
    data_dir: Path,
) -> None:
    client, settings, csrf = _client(data_dir, raise_server_exceptions=False)
    with connect(settings.database_path) as connection:
        queued = JobRepository(connection).create(
            school_id=SCHOOL_ID,
            workspace_id=WORKSPACE_ID,
            job_type="INGEST",
            payload={"source_document_ids": ["missing"]},
        )
        connection.commit()
    with client:
        retry = client.post(
            f"/api/v2/jobs/{queued.id}/retry",
            headers=_headers(csrf, "invalid-retry"),
        )
    assert retry.status_code == 409
    assert retry.json()["detail"]["code"] == "JOB_RETRY_NOT_ALLOWED"
    assert retry.json()["detail"]["request_id"] == REQUEST_ID

    reviewer, reviewer_settings, reviewer_csrf = _client(
        data_dir / "reviewer", reviewer=True
    )
    draft_id = str(uuid.uuid4())
    with connect(reviewer_settings.database_path) as connection:
        connection.execute(
            """
            INSERT INTO acquisition_workspaces (
                id, school_id, name, status, created_at, updated_at
            ) VALUES (?, ?, '검토자 차단', 'DRAFT', ?, ?)
            """,
            (draft_id, SCHOOL_ID, NOW, NOW),
        )
        connection.commit()
    with reviewer:
        denied = reviewer.patch(
            f"/api/v2/workspaces/{draft_id}/status",
            headers=_headers(reviewer_csrf, "reviewer-transition", version=1),
            json={"target": "ANALYZING", "reason": "권한 없음"},
        )
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "OPERATOR_ROLE_REQUIRED"


def test_upload_and_bulk_payload_limits_reject_before_durable_mutation(
    data_dir: Path,
) -> None:
    client, settings, csrf = _client(data_dir)
    too_many_files = [
        (
            "files",
            (
                f"books-{index}.csv",
                f"제목,저자\n책 {index},저자\n".encode(),
                "text/csv",
            ),
        )
        for index in range(21)
    ]
    with client:
        upload = client.post(
            f"/api/v2/workspaces/{WORKSPACE_ID}/sources",
            headers=_headers(csrf, "too-many-files"),
            data={"role": "PURCHASE_REQUEST"},
            files=too_many_files,
        )
        bulk = client.post(
            f"/api/v2/workspaces/{WORKSPACE_ID}/candidates/bulk-decision",
            headers=_headers(csrf, "too-many-candidates"),
            json={"items": [{} for _ in range(1001)]},
        )

    assert upload.status_code == 413
    assert upload.json()["detail"]["code"] == "UPLOAD_BATCH_LIMIT_EXCEEDED"
    assert bulk.status_code == 422
    with connect(settings.database_path) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM source_files").fetchone()[0] == 0
        )

    from pydantic import ValidationError

    from suseoro.api.routes.deliveries import DeliveryCreate
    from suseoro.api.routes.procurement import QuoteCreate

    with pytest.raises(ValidationError):
        QuoteCreate.model_validate(
            {
                "approval_revision_id": "a",
                "vendor_name": "업체",
                "rows": [{} for _ in range(5001)],
                "reason": "제한",
            }
        )
    with pytest.raises(ValidationError):
        DeliveryCreate.model_validate(
            {
                "order_revision_id": "o",
                "rows": [{} for _ in range(5001)],
                "reason": "제한",
            }
        )


def test_upload_aggregate_limit_counts_bytes_from_per_file_rejections(
    data_dir: Path,
) -> None:
    client, settings, csrf = _client(
        data_dir,
        settings_kwargs={
            "upload_max_file_bytes": 20,
            "upload_max_batch_bytes": 30,
        },
    )
    first = b"a,b\n1234567890,1234567890\n"
    second = b"a,b\nabcdefghij,abcdefghij\n"
    assert len(first) > 20 and len(second) > 20 and len(first) + len(second) > 30

    with client:
        response = client.post(
            f"/api/v2/workspaces/{WORKSPACE_ID}/sources",
            headers=_headers(csrf, "aggregate-rejected-bytes"),
            data={"role": "PURCHASE_REQUEST"},
            files=[
                ("files", ("first.csv", first, "text/csv")),
                ("files", ("second.csv", second, "text/csv")),
            ],
        )

    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "UPLOAD_BATCH_LIMIT_EXCEEDED"
    with connect(settings.database_path) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM source_files").fetchone()[0] == 0
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM durable_jobs").fetchone()[0] == 0
        )
    assert not [path for path in settings.sources_dir.rglob("*") if path.is_file()]


def test_upload_drains_rejected_file_tails_before_deciding_aggregate_result(
    data_dir: Path,
) -> None:
    client, settings, csrf = _client(
        data_dir,
        settings_kwargs={
            "upload_max_file_bytes": 1_000,
            "upload_max_batch_bytes": 150_000,
        },
    )
    first = b"a" * 100_000
    second = b"b" * 100_000

    with client:
        response = client.post(
            f"/api/v2/workspaces/{WORKSPACE_ID}/sources",
            headers=_headers(csrf, "aggregate-rejected-tails"),
            data={"role": "PURCHASE_REQUEST"},
            files=[
                ("files", ("first.csv", first, "text/csv")),
                ("files", ("second.csv", second, "text/csv")),
            ],
        )

    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "UPLOAD_BATCH_LIMIT_EXCEEDED"
    with connect(settings.database_path) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM source_files").fetchone()[0] == 0
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM durable_jobs").fetchone()[0] == 0
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM idempotency_keys").fetchone()[0]
            == 0
        )
    assert not [path for path in settings.sources_dir.rglob("*") if path.is_file()]


@pytest.mark.parametrize(
    "database_error",
    [
        sqlite3.OperationalError("database is locked: internal path"),
        sqlite3.ProgrammingError("closed cursor exposes internal state"),
    ],
)
def test_expected_sqlite_failures_use_safe_request_id_error_envelopes(
    data_dir: Path, database_error: sqlite3.Error
) -> None:
    settings = Settings(data_dir=data_dir, secure_cookies=False)
    app = create_app(settings)

    @app.get("/api/v2/test/database-failure")
    def fail_database_probe():
        raise database_error

    client = TestClient(
        app,
        base_url="https://testserver",
        raise_server_exceptions=False,
    )
    with client:
        response = client.get(
            "/api/v2/test/database-failure",
            headers={"X-Request-ID": REQUEST_ID},
        )

    assert response.status_code == 503
    assert response.json()["detail"] == {
        "code": "DATABASE_UNAVAILABLE",
        "message": "데이터베이스 작업을 완료할 수 없습니다. 잠시 후 다시 시도해 주세요.",
        "request_id": REQUEST_ID,
        "fields": [],
    }


@pytest.mark.parametrize(
    "database_error",
    [
        sqlite3.OperationalError("middleware database is locked: secret path"),
        sqlite3.ProgrammingError("middleware connection was closed"),
    ],
)
def test_middleware_session_database_failures_use_the_request_id_json_envelope(
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    database_error: sqlite3.Error,
) -> None:
    client, _, csrf = _client(data_dir)

    def fail_session_connection(_path: Path):
        raise database_error

    with client:
        monkeypatch.setattr("suseoro.api.app.connect", fail_session_connection)
        response = client.post(
            "/api/v2/workspaces",
            headers=_headers(csrf, "middleware-database-failure"),
            json={"name": "실패하면 안 되는 평문 경계"},
        )

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["detail"] == {
        "code": "DATABASE_UNAVAILABLE",
        "message": "데이터베이스 작업을 완료할 수 없습니다. 잠시 후 다시 시도해 주세요.",
        "request_id": REQUEST_ID,
        "fields": [],
    }
    assert "secret path" not in response.text


def test_candidate_lock_is_audited_once_with_actor_and_before_after(
    data_dir: Path,
) -> None:
    client, settings, csrf = _client(data_dir)
    _, candidate_ids = _source_and_candidates(settings)
    with client:
        first = client.post(
            f"/api/v2/candidates/{candidate_ids[0]}/lock",
            headers=_headers(csrf, "candidate-lock-audit"),
            json={"workspace_id": WORKSPACE_ID},
        )
        second = client.post(
            f"/api/v2/candidates/{candidate_ids[0]}/lock",
            headers=_headers(csrf, "candidate-lock-audit"),
            json={"workspace_id": WORKSPACE_ID},
        )
    assert first.json() == second.json()
    with connect(settings.database_path) as connection:
        events = connection.execute(
            """
            SELECT actor_id, before_json, after_json, request_id
            FROM audit_events WHERE action = 'CANDIDATE_LOCK_ACQUIRED'
            """
        ).fetchall()
    assert len(events) == 1
    assert events[0]["actor_id"] == OPERATOR_ID
    assert events[0]["before_json"] is not None
    assert events[0]["after_json"] is not None
    assert events[0]["request_id"] == REQUEST_ID


def test_v1_admin_routes_require_local_confirmation_and_configured_path_roots(
    data_dir: Path,
) -> None:
    from openpyxl import Workbook

    allowed = data_dir / "configured-imports"
    source = allowed / "v1-source" / "학교"
    source.mkdir(parents=True)
    workbook = Workbook()
    workbook.active.append(["ISBN", "자료명"])
    workbook.active.append(["9780306406157", "이전 장서"])
    workbook.save(source / "DLS_1.xlsx")
    destination = data_dir / "configured-destination"
    outside = data_dir / "outside"
    outside.mkdir()
    client, settings, csrf = _client(
        data_dir / "api",
        settings_kwargs={
            "v1_import_roots": (allowed,),
            "v1_destination_root": destination,
        },
    )
    confirmation = client.app.state.local_admin_confirmation_token
    headers = _headers(csrf, "v1-admin-boundary")
    confirmed = {**headers, "X-Local-Admin-Confirmation": confirmation}

    with client:
        missing_confirmation = client.post(
            "/api/v2/admin/v1-migration/inspect",
            headers=headers,
            json={"source_path": str(source.parent)},
        )
        outside_source = client.post(
            "/api/v2/admin/v1-migration/inspect",
            headers=confirmed,
            json={"source_path": str(outside)},
        )
        escaped_destination = client.post(
            "/api/v2/admin/v1-migration/run",
            headers=confirmed,
            json={
                "source_path": str(source.parent),
                "destination_path": str(data_dir / "escaped-destination"),
            },
        )
        migrated = client.post(
            "/api/v2/admin/v1-migration/run",
            headers={**confirmed, "Idempotency-Key": "v1-contained-run"},
            json={
                "source_path": str(source.parent),
                "destination_path": str(destination),
            },
        )
        with connect(settings.database_path) as connection:
            candidate_id = connection.execute(
                "SELECT id FROM v1_catalog_candidates"
            ).fetchone()["id"]
        activated = client.post(
            f"/api/v2/admin/v1-migration/catalog-candidates/{candidate_id}/activate",
            headers={**confirmed, "Idempotency-Key": "v1-candidate-activation"},
            json={"confirmed_row_count": 1},
        )

    assert missing_confirmation.status_code == 403
    assert (
        missing_confirmation.json()["detail"]["code"]
        == "LOCAL_ADMIN_CONFIRMATION_REQUIRED"
    )
    assert outside_source.status_code == escaped_destination.status_code == 400
    assert outside_source.json()["detail"]["code"] == "PATH_OUTSIDE_CONFIGURED_ROOT"
    assert (
        escaped_destination.json()["detail"]["code"] == "PATH_OUTSIDE_CONFIGURED_ROOT"
    )
    assert migrated.status_code == 200
    assert activated.status_code == 200
    with connect(settings.database_path) as connection:
        audit = connection.execute(
            "SELECT action, actor_id, request_id FROM audit_events WHERE action = 'V1_MIGRATION_COMPLETED'"
        ).fetchall()
        v1_candidate = connection.execute(
            "SELECT status, catalog_version_id FROM v1_catalog_candidates"
        ).fetchone()
        active_catalogs = connection.execute(
            "SELECT COUNT(*) FROM catalog_versions WHERE status = 'ACTIVE'"
        ).fetchone()[0]
    assert [(row["action"], row["actor_id"], row["request_id"]) for row in audit] == [
        ("V1_MIGRATION_COMPLETED", OPERATOR_ID, REQUEST_ID)
    ]
    assert v1_candidate["status"] == "ACTIVATED"
    assert v1_candidate["catalog_version_id"]
    assert active_catalogs == 1


def test_v1_pending_candidates_and_legacy_history_are_tenant_scoped_and_paginated(
    data_dir: Path,
) -> None:
    client, settings, csrf = _client(data_dir)
    own_candidate_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    other_candidate_id = str(uuid.uuid4())
    with connect(settings.database_path) as connection:
        for index, candidate_id in enumerate((*own_candidate_ids, other_candidate_id)):
            school_id = SCHOOL_ID if index < 2 else OTHER_SCHOOL_ID
            connection.execute(
                """
                INSERT INTO v1_catalog_candidates (
                    id, school_id, source_copy_path, sha256, row_count, created_at
                ) VALUES (?, ?, ?, ?, 1, ?)
                """,
                (
                    candidate_id,
                    school_id,
                    str(settings.v1_destination_root / f"candidate-{index}.xlsx"),
                    f"{index + 1:x}" * 64,
                    f"2026-08-2{index + 1}T00:00:00.000000Z",
                ),
            )
        for index, school_id in enumerate((SCHOOL_ID, OTHER_SCHOOL_ID)):
            connection.execute(
                """
                INSERT INTO legacy_v1_workspaces (
                    id, school_id, display_name, source_copy_path, sha256, imported_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    school_id,
                    f"이전 작업 {index}",
                    str(settings.v1_destination_root / f"legacy-{index}.xlsx"),
                    f"{index + 7:x}" * 64,
                    f"2026-08-2{index + 1}T00:00:00.000000Z",
                ),
            )
        connection.commit()
    confirmation = client.app.state.local_admin_confirmation_token
    with client:
        first = client.get(
            "/api/v2/v1/catalog-candidates",
            params={"status": "PENDING_CONFIRMATION", "limit": 1},
        )
        cursor = first.json().get("next_cursor")
        second = (
            client.get(
                "/api/v2/v1/catalog-candidates",
                params={
                    "status": "PENDING_CONFIRMATION",
                    "limit": 1,
                    "cursor": cursor,
                },
            )
            if cursor
            else first
        )
        history = client.get("/api/v2/v1/legacy-workspaces", params={"limit": 10})
        cross_tenant_activation = client.post(
            f"/api/v2/admin/v1-migration/catalog-candidates/{other_candidate_id}/activate",
            headers={
                **_headers(csrf, "cross-tenant-v1-candidate"),
                "X-Local-Admin-Confirmation": confirmation,
            },
            json={"confirmed_row_count": 1},
        )

    assert first.status_code == second.status_code == history.status_code == 200
    visible_candidates = {
        first.json()["items"][0]["id"],
        second.json()["items"][0]["id"],
    }
    assert visible_candidates == set(own_candidate_ids)
    assert first.json()["next_cursor"]
    assert history.json()["items"] == [
        {
            "id": history.json()["items"][0]["id"],
            "display_name": "이전 작업 0",
            "source_copy_path": str(settings.v1_destination_root / "legacy-0.xlsx"),
            "sha256": "7" * 64,
            "is_read_only": True,
            "imported_at": "2026-08-21T00:00:00.000000Z",
        }
    ]
    assert cross_tenant_activation.status_code == 404
    assert (
        cross_tenant_activation.json()["detail"]["code"]
        == "V1_CATALOG_CANDIDATE_NOT_FOUND"
    )


def test_receiving_differences_have_independent_filtered_stable_cursor_pages(
    data_dir: Path,
) -> None:
    client, settings, _ = _client(data_dir)
    with connect(settings.database_path) as connection:
        connection.commit()
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("DROP TRIGGER receiving_difference_scope_insert")
        for index in range(3):
            connection.execute(
                """
                INSERT INTO receiving_differences (
                    id, school_id, workspace_id, order_revision_id, kind,
                    reference_key, details_json, disposition, active,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'historical-order', 'MISSING', ?, '{}',
                          'VENDOR_CHECK', 1, ?, ?)
                """,
                (
                    f"550e8400-e29b-41d4-a716-4466554403{index:02d}",
                    SCHOOL_ID,
                    WORKSPACE_ID,
                    f"row-{index}",
                    f"2026-08-2{index}T00:00:00.000000Z",
                    f"2026-08-2{index}T00:00:00.000000Z",
                ),
            )
        connection.commit()
    params = {
        "kind": "MISSING",
        "disposition": "VENDOR_CHECK",
        "active": True,
        "limit": 1,
    }
    with client:
        first = client.get(
            f"/api/v2/workspaces/{WORKSPACE_ID}/receiving/differences",
            params=params,
        )
        second = client.get(
            f"/api/v2/workspaces/{WORKSPACE_ID}/receiving/differences",
            params={**params, "cursor": first.json()["next_cursor"]},
        )

    assert first.status_code == second.status_code == 200
    assert first.json()["items"][0]["reference_key"] == "row-2"
    assert second.json()["items"][0]["reference_key"] == "row-1"
    assert first.json()["items"][0]["kind"] == "MISSING"
    assert first.json()["next_cursor"] != second.json()["next_cursor"]


def test_backup_creation_is_audited_and_listing_uses_a_stable_cursor(
    data_dir: Path,
) -> None:
    from datetime import UTC, datetime, timedelta

    client, settings, csrf = _client(data_dir)
    service = BackupService(settings.database_path, settings.backups_dir)
    start = datetime(2026, 8, 20, tzinfo=UTC)
    for offset in range(3):
        service.create(kind="daily", now=start + timedelta(days=offset))
    with client:
        first = client.get("/api/v2/admin/backups", params={"limit": 1})
        second = client.get(
            "/api/v2/admin/backups",
            params={"limit": 1, "cursor": first.json()["next_cursor"]},
        )
        created = client.post(
            "/api/v2/admin/backups",
            headers=_headers(csrf, "audited-backup"),
            json={"kind": "manual"},
        )

    assert first.status_code == second.status_code == created.status_code - 1 == 200
    assert first.json()["items"][0]["id"] != second.json()["items"][0]["id"]
    with connect(settings.database_path) as connection:
        audit = connection.execute(
            """
            SELECT actor_id, before_json, after_json, request_id
            FROM audit_events WHERE action = 'DATABASE_BACKUP_CREATED'
            """
        ).fetchall()
    assert len(audit) == 1
    assert audit[0]["actor_id"] == OPERATOR_ID
    assert audit[0]["before_json"] is not None
    assert audit[0]["after_json"] is not None
    assert audit[0]["request_id"] == REQUEST_ID
