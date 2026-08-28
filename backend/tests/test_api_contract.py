from __future__ import annotations

import json
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from suseoro.api.app import create_app
from suseoro.config import Settings
from suseoro.db.connection import connect
from suseoro.db.migrations import apply_migrations
from suseoro.jobs.handlers import build_job_runner
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
    data_dir: Path, *, reviewer: bool = False
) -> tuple[TestClient, Settings, str]:
    settings = Settings(data_dir=data_dir, secure_cookies=False)
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
    client = TestClient(create_app(settings), base_url="https://testserver")
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
