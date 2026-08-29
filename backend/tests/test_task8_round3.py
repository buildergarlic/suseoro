from __future__ import annotations

import hashlib
import json
import uuid

import pytest
from test_api_contract import _client, _headers
from test_task8_round2 import _active_catalog, _draft_workspace

from suseoro.db.connection import connect
from suseoro.jobs.handlers import build_job_runner
from suseoro.jobs.repository import JobRepository
from suseoro.services.comparison import ComparisonService

SCHOOL_ID = "550e8400-e29b-41d4-a716-446655440100"


def _upload_and_parse(client, settings, csrf: str, workspace_id: str, key: str) -> str:
    uploaded = client.post(
        f"/api/v2/workspaces/{workspace_id}/sources",
        headers=_headers(csrf, key),
        data={"role": "PURCHASE_REQUEST"},
        files=[("files", (f"{key}.csv", "제목,저자\n기준 책,저자\n", "text/csv"))],
    )
    assert uploaded.status_code == 202, uploaded.json()
    source_id = uploaded.json()["items"][0]["source_id"]
    with connect(settings.database_path) as connection:
        assert build_job_runner(connection).run_once().status == "SUCCEEDED"
    return source_id


def test_comparison_derives_all_101_authoritative_sources_server_side(data_dir) -> None:
    client, settings, csrf = _client(data_dir, raise_server_exceptions=False)
    workspace_id = _draft_workspace(client, csrf, "server-derived-source-set")
    _active_catalog(settings)
    source_id = _upload_and_parse(
        client, settings, csrf, workspace_id, "server-derived-seed"
    )
    with connect(settings.database_path) as connection:
        seed = connection.execute(
            """
            SELECT document.*, file.size_bytes, file.storage_path,
                   file.original_filename, file.sha256, config.mapping_json, config.vendor_scope,
                   config.remember_template, config.row_version,
                   config.updated_at AS config_updated_at,
                   link.created_at AS linked_at
            FROM source_documents document
            JOIN source_files file ON file.id = document.source_file_id
            JOIN source_configurations config
              ON config.source_document_id = document.id
            JOIN workspace_sources link
              ON link.source_document_id = document.id
            WHERE document.id = ? AND link.workspace_id = ?
            """,
            (source_id, workspace_id),
        ).fetchone()
        seed_row = connection.execute(
            "SELECT * FROM source_rows WHERE source_document_id = ?",
            (source_id,),
        ).fetchone()
        for index in range(100):
            clone_file_id = str(uuid.uuid4())
            clone_id = str(uuid.uuid4())
            clone_sha = hashlib.sha256(f"clone-{index}".encode()).hexdigest()
            connection.execute(
                """
                INSERT INTO source_files (
                    id, sha256, size_bytes, storage_path, original_filename,
                    detected_format, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    clone_file_id,
                    clone_sha,
                    seed["size_bytes"],
                    seed["storage_path"],
                    f"clone-{index}.csv",
                    seed["detected_format"],
                    seed["created_at"],
                ),
            )
            connection.execute(
                """
                INSERT INTO source_documents (
                    id, source_file_id, school_id, role, parser_version,
                    template_version, status, detected_format, created_at,
                        completed_at, activation_allowed,
                        requested_start_local_date, requested_through_local_date,
                        parsed_config_version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    clone_id,
                    clone_file_id,
                    seed["school_id"],
                    seed["role"],
                    seed["parser_version"],
                    seed["template_version"],
                    seed["status"],
                    seed["detected_format"],
                    seed["created_at"],
                    seed["completed_at"],
                    seed["activation_allowed"],
                    seed["requested_start_local_date"],
                    seed["requested_through_local_date"],
                    seed["parsed_config_version"],
                ),
            )
            connection.execute(
                """
                INSERT INTO source_configurations (
                    source_document_id, school_id, role, mapping_json,
                    row_version, updated_at, vendor_scope, remember_template
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    clone_id,
                    SCHOOL_ID,
                    "PURCHASE_REQUEST",
                    seed["mapping_json"],
                    seed["row_version"],
                    seed["config_updated_at"],
                    seed["vendor_scope"],
                    seed["remember_template"],
                ),
            )
            connection.execute(
                """
                INSERT INTO workspace_sources (
                    workspace_id, source_document_id, school_id, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (workspace_id, clone_id, SCHOOL_ID, seed["linked_at"]),
            )
            connection.execute(
                """
                INSERT INTO source_rows (
                    id, source_document_id, sheet_name, source_row, status,
                    raw_json, fields_json, warnings_json, error_code,
                    error_message, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    clone_id,
                    seed_row["sheet_name"],
                    seed_row["source_row"],
                    seed_row["status"],
                    seed_row["raw_json"],
                    seed_row["fields_json"],
                    seed_row["warnings_json"],
                    seed_row["error_code"],
                    seed_row["error_message"],
                    seed_row["created_at"],
                ),
            )
        connection.commit()

    queued = client.post(
        f"/api/v2/workspaces/{workspace_id}/comparison-jobs",
        headers=_headers(csrf, "server-derived-compare", version=1),
        json={},
    )

    assert queued.status_code == 202, queued.json()
    with connect(settings.database_path) as connection:
        payload = json.loads(
            connection.execute(
                "SELECT payload_json FROM durable_jobs WHERE id = ?",
                (queued.json()["job_id"],),
            ).fetchone()["payload_json"]
        )
    assert len(payload["source_document_ids"]) == 101
    assert len(payload["source_snapshot"]) == 101


def test_unknown_repair_requires_explicit_configuration_and_resolves_after_parse(
    data_dir,
) -> None:
    client, settings, csrf = _client(data_dir, raise_server_exceptions=False)
    workspace_id = _draft_workspace(client, csrf, "unknown-repair-confirmation")
    rejected = client.post(
        f"/api/v2/workspaces/{workspace_id}/sources",
        headers=_headers(csrf, "unknown-repair-rejected"),
        files=[("files", ("legacy.exe", b"MZ-legacy", "application/octet-stream"))],
    )
    assert rejected.status_code == 207
    obligation = rejected.json()["items"][0]

    unconfirmed = client.post(
        f"/api/v2/workspaces/{workspace_id}/sources",
        headers=_headers(csrf, "unknown-repair-unconfirmed"),
        data={
            "role": "PURCHASE_REQUEST",
            "repair_obligation_id": obligation["repair_obligation_id"],
            "repair_generation": "1",
        },
        files=[("files", ("fixed.csv", "제목\n복구 책\n", "text/csv"))],
    )
    assert unconfirmed.status_code == 422
    assert unconfirmed.json()["detail"]["code"] == (
        "UPLOAD_REPAIR_CONFIGURATION_CONFIRMATION_REQUIRED"
    )

    confirmed = client.post(
        f"/api/v2/workspaces/{workspace_id}/sources",
        headers=_headers(csrf, "unknown-repair-confirmed"),
        data={
            "role": "PURCHASE_REQUEST",
            "repair_obligation_id": obligation["repair_obligation_id"],
            "repair_generation": "1",
            "confirm_repair_configuration": "true",
        },
        files=[("files", ("fixed.csv", "제목\n복구 책\n", "text/csv"))],
    )
    assert confirmed.status_code == 202, confirmed.json()
    with connect(settings.database_path) as connection:
        before_parse = connection.execute(
            "SELECT status, role FROM upload_repair_obligations WHERE id = ?",
            (obligation["repair_obligation_id"],),
        ).fetchone()
    assert dict(before_parse) == {"status": "REPAIRING", "role": "PURCHASE_REQUEST"}

    _active_catalog(settings)
    blocked = client.post(
        f"/api/v2/workspaces/{workspace_id}/comparison-jobs",
        headers=_headers(csrf, "unknown-repair-before-parse", version=1),
        json={},
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "UPLOAD_REPAIR_REQUIRED"

    with connect(settings.database_path) as connection:
        assert build_job_runner(connection).run_once().status == "SUCCEEDED"
        after_parse = connection.execute(
            "SELECT status, resolved_source_document_id FROM upload_repair_obligations WHERE id = ?",
            (obligation["repair_obligation_id"],),
        ).fetchone()
    assert after_parse["status"] == "RESOLVED"
    assert (
        after_parse["resolved_source_document_id"]
        == confirmed.json()["items"][0]["source_id"]
    )


def test_mapping_change_invalidates_rows_until_current_configuration_is_reparsed(
    data_dir,
) -> None:
    client, settings, csrf = _client(data_dir, raise_server_exceptions=False)
    workspace_id = _draft_workspace(client, csrf, "mapping-generation")
    _active_catalog(settings)
    source_id = _upload_and_parse(client, settings, csrf, workspace_id, "mapping-seed")

    changed = client.patch(
        f"/api/v2/sources/{source_id}/mapping",
        headers=_headers(csrf, "mapping-generation-change", version=1),
        json={
            "role": "PURCHASE_REQUEST",
            "mapping": {"제목": "title", "저자": "author"},
            "remember_template": True,
            "vendor_scope": "*",
        },
    )
    assert changed.status_code == 200
    with connect(settings.database_path) as connection:
        versions = connection.execute(
            """
            SELECT document.status, document.parsed_config_version,
                   config.row_version
            FROM source_documents document
            JOIN source_configurations config
              ON config.source_document_id = document.id
            WHERE document.id = ?
            """,
            (source_id,),
        ).fetchone()
    assert versions["status"] == "PENDING"
    assert versions["parsed_config_version"] is None

    stale = client.post(
        f"/api/v2/workspaces/{workspace_id}/comparison-jobs",
        headers=_headers(csrf, "mapping-generation-stale", version=1),
        json={},
    )
    assert stale.status_code == 422
    assert stale.json()["detail"]["code"] == "COMPARISON_SOURCES_NOT_READY"

    reparsed = client.post(
        f"/api/v2/sources/{source_id}/parse",
        headers=_headers(csrf, "mapping-generation-reparse"),
    )
    assert reparsed.status_code == 202
    with connect(settings.database_path) as connection:
        assert build_job_runner(connection).run_once().status == "SUCCEEDED"
        versions = connection.execute(
            """
            SELECT document.parsed_config_version, config.row_version
            FROM source_documents document
            JOIN source_configurations config
              ON config.source_document_id = document.id
            WHERE document.id = ?
            """,
            (source_id,),
        ).fetchone()
    assert versions["parsed_config_version"] == versions["row_version"]


def test_comparison_locked_batch_rejects_configuration_drift_before_any_rows(
    data_dir,
) -> None:
    client, settings, csrf = _client(data_dir, raise_server_exceptions=False)
    workspace_id = _draft_workspace(client, csrf, "locked-config-fence")
    _active_catalog(settings)
    source_id = _upload_and_parse(client, settings, csrf, workspace_id, "locked-seed")
    queued = client.post(
        f"/api/v2/workspaces/{workspace_id}/comparison-jobs",
        headers=_headers(csrf, "locked-compare", version=1),
        json={"source_document_ids": [source_id]},
    )
    assert queued.status_code == 202
    with connect(settings.database_path) as connection:
        claimed = JobRepository(connection).claim_next()
        assert claimed is not None and claimed.id == queued.json()["job_id"]
        connection.execute(
            """
            UPDATE source_configurations
            SET mapping_json = '{"제목":"isbn13"}', row_version = row_version + 1
            WHERE source_document_id = ?
            """,
            (source_id,),
        )
        connection.commit()
        with pytest.raises(ValueError, match="snapshot|configuration|source"):
            ComparisonService(connection).compare_documents(
                school_id=SCHOOL_ID,
                workspace_id=workspace_id,
                source_document_ids=(source_id,),
                job_id=claimed.id,
                claim_token=claimed.claim_token,
                claim_generation=claimed.claim_generation,
            )
        row_count = connection.execute(
            "SELECT COUNT(*) FROM comparison_row_results WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchone()[0]
    assert row_count == 0


def test_shared_source_cannot_start_a_workspace_scoped_parse(data_dir) -> None:
    client, settings, csrf = _client(data_dir, raise_server_exceptions=False)
    first_workspace = _draft_workspace(client, csrf, "shared-parse-first")
    second_workspace = _draft_workspace(client, csrf, "shared-parse-second")
    source_id = _upload_and_parse(
        client, settings, csrf, first_workspace, "shared-parse-source"
    )
    with connect(settings.database_path) as connection:
        linked_at = connection.execute(
            "SELECT created_at FROM workspace_sources WHERE workspace_id = ? AND source_document_id = ?",
            (first_workspace, source_id),
        ).fetchone()["created_at"]
        connection.execute(
            """
            INSERT INTO workspace_sources (
                workspace_id, source_document_id, school_id, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            (second_workspace, source_id, SCHOOL_ID, linked_at),
        )
        connection.commit()

    parsed = client.post(
        f"/api/v2/sources/{source_id}/parse",
        headers=_headers(csrf, "shared-parse-command"),
    )

    assert parsed.status_code == 409
    assert parsed.json()["detail"]["code"] == "SOURCE_SHARED_ACROSS_WORKSPACES"


def test_parse_queued_before_sharing_blocks_comparison_in_every_linked_workspace(
    data_dir,
) -> None:
    client, settings, csrf = _client(data_dir, raise_server_exceptions=False)
    first_workspace = _draft_workspace(client, csrf, "shared-active-first")
    second_workspace = _draft_workspace(client, csrf, "shared-active-second")
    _active_catalog(settings)
    source_id = _upload_and_parse(
        client, settings, csrf, first_workspace, "shared-active-source"
    )
    queued = client.post(
        f"/api/v2/sources/{source_id}/parse",
        headers=_headers(csrf, "shared-active-parse"),
    )
    assert queued.status_code == 202
    with connect(settings.database_path) as connection:
        linked_at = connection.execute(
            """
            SELECT created_at FROM workspace_sources
            WHERE workspace_id = ? AND source_document_id = ?
            """,
            (first_workspace, source_id),
        ).fetchone()["created_at"]
        connection.execute(
            """
            INSERT INTO workspace_sources (
                workspace_id, source_document_id, school_id, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            (second_workspace, source_id, SCHOOL_ID, linked_at),
        )
        connection.commit()

    blocked = client.post(
        f"/api/v2/workspaces/{second_workspace}/comparison-jobs",
        headers=_headers(csrf, "shared-active-compare", version=1),
        json={},
    )

    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "SOURCE_PROCESSING_IN_PROGRESS"


def test_confirmed_unknown_repair_stays_open_when_mapping_is_still_required(
    data_dir,
) -> None:
    client, settings, csrf = _client(data_dir, raise_server_exceptions=False)
    workspace_id = _draft_workspace(client, csrf, "repair-mapping-required")
    _active_catalog(settings)
    rejected = client.post(
        f"/api/v2/workspaces/{workspace_id}/sources",
        headers=_headers(csrf, "repair-mapping-rejected"),
        files=[("files", ("legacy.exe", b"MZ-legacy", "application/octet-stream"))],
    )
    obligation_id = rejected.json()["items"][0]["repair_obligation_id"]
    replacement = client.post(
        f"/api/v2/workspaces/{workspace_id}/sources",
        headers=_headers(csrf, "repair-mapping-replacement"),
        data={
            "role": "PURCHASE_REQUEST",
            "repair_obligation_id": obligation_id,
            "repair_generation": "1",
            "confirm_repair_configuration": "true",
        },
        files=[("files", ("unknown.csv", "foo,bar\n값,값\n", "text/csv"))],
    )
    assert replacement.status_code == 202
    with connect(settings.database_path) as connection:
        assert build_job_runner(connection).run_once().status == "PARTIAL"
        repair = connection.execute(
            """
            SELECT status, resolved_source_document_id, pending_source_document_id
            FROM upload_repair_obligations WHERE id = ?
            """,
            (obligation_id,),
        ).fetchone()
    assert dict(repair) == {
        "status": "REPAIRING",
        "resolved_source_document_id": None,
        "pending_source_document_id": replacement.json()["items"][0]["source_id"],
    }
    blocked = client.post(
        f"/api/v2/workspaces/{workspace_id}/comparison-jobs",
        headers=_headers(csrf, "repair-mapping-compare", version=1),
        json={},
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "UPLOAD_REPAIR_REQUIRED"


def test_mixed_row_errors_count_only_successful_books_as_processed(data_dir) -> None:
    client, settings, csrf = _client(data_dir)
    workspace_id = _draft_workspace(client, csrf, "mixed-row-counts")
    uploaded = client.post(
        f"/api/v2/workspaces/{workspace_id}/sources",
        headers=_headers(csrf, "mixed-row-upload"),
        data={"role": "PURCHASE_REQUEST"},
        files=[
            (
                "files",
                ("mixed.csv", "제목,저자\n읽은 책,저자\n,제목 없음\n", "text/csv"),
            )
        ],
    )
    with connect(settings.database_path) as connection:
        result = build_job_runner(connection).run_once()
    assert result.status == "PARTIAL"
    item = client.get(f"/api/v2/jobs/{uploaded.json()['job_id']}").json()["items"][0]
    assert item["status"] == "PARTIAL"
    assert item["total_rows"] == 2
    assert item["processed_rows"] == 1


def test_source_replacement_inherits_vendor_mapping_and_template_contract(
    data_dir,
) -> None:
    client, settings, csrf = _client(data_dir, raise_server_exceptions=False)
    workspace_id = _draft_workspace(client, csrf, "replacement-full-contract")
    uploaded = client.post(
        f"/api/v2/workspaces/{workspace_id}/sources",
        headers=_headers(csrf, "replacement-original"),
        data={"role": "VENDOR_QUOTE", "vendor_scope": "vendor-a"},
        files=[("files", ("quote.csv", "제목,정가\n견적 책,12000\n", "text/csv"))],
    )
    original_id = uploaded.json()["items"][0]["source_id"]
    with connect(settings.database_path) as connection:
        build_job_runner(connection).run_once()
    mapped = client.patch(
        f"/api/v2/sources/{original_id}/mapping",
        headers=_headers(csrf, "replacement-original-map", version=1),
        json={
            "role": "VENDOR_QUOTE",
            "mapping": {"제목": "title", "정가": "list_price"},
            "remember_template": True,
            "vendor_scope": "vendor-a",
        },
    )
    assert mapped.status_code == 200

    replacement = client.post(
        f"/api/v2/workspaces/{workspace_id}/sources",
        headers=_headers(csrf, "replacement-new"),
        data={
            "role": "VENDOR_QUOTE",
            "replacement_source_document_id": original_id,
        },
        files=[("files", ("quote-new.csv", "제목,정가\n새 견적,13000\n", "text/csv"))],
    )
    assert replacement.status_code == 202, replacement.json()
    replacement_id = replacement.json()["items"][0]["source_id"]
    with connect(settings.database_path) as connection:
        config = connection.execute(
            """
            SELECT role, vendor_scope, mapping_json, remember_template
            FROM source_configurations WHERE source_document_id = ?
            """,
            (replacement_id,),
        ).fetchone()
    assert dict(config) == {
        "role": "VENDOR_QUOTE",
        "vendor_scope": "vendor-a",
        "mapping_json": json.dumps(
            {"제목": "title", "정가": "list_price"},
            ensure_ascii=False,
            sort_keys=True,
        ),
        "remember_template": 1,
    }


def test_delta_source_replacement_inherits_inclusive_window(data_dir) -> None:
    client, _, csrf = _client(data_dir, raise_server_exceptions=False)
    workspace_id = _draft_workspace(client, csrf, "replacement-delta-contract")
    uploaded = client.post(
        f"/api/v2/workspaces/{workspace_id}/sources",
        headers=_headers(csrf, "replacement-delta-original"),
        data={
            "role": "CATALOG_DELTA_UPDATE",
            "requested_start_local_date": "2026-03-01",
            "requested_through_local_date": "2026-03-31",
        },
        files=[("files", ("delta.csv", "등록번호,제목\nR-1,변경 책\n", "text/csv"))],
    )
    assert uploaded.status_code == 202
    original_id = uploaded.json()["items"][0]["source_id"]

    replacement = client.post(
        f"/api/v2/workspaces/{workspace_id}/sources",
        headers=_headers(csrf, "replacement-delta-new"),
        data={
            "role": "CATALOG_DELTA_UPDATE",
            "replacement_source_document_id": original_id,
        },
        files=[
            ("files", ("delta-new.csv", "등록번호,제목\nR-2,새 변경\n", "text/csv"))
        ],
    )

    assert replacement.status_code == 202, replacement.json()
    source = client.get(
        f"/api/v2/sources/{replacement.json()['items'][0]['source_id']}"
    ).json()
    assert source["requested_start_local_date"] == "2026-03-01"
    assert source["requested_through_local_date"] == "2026-03-31"
