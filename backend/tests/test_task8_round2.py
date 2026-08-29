from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest
from fastapi import HTTPException
from test_api_contract import _client, _headers

from suseoro.catalog.contracts import CatalogRecord, SourceType
from suseoro.catalog.sync import CatalogSyncService
from suseoro.db.connection import connect
from suseoro.ingestion.contracts import DocumentRole
from suseoro.jobs.handlers import build_job_runner
from suseoro.jobs.repository import JobRepository
from suseoro.services.comparison import ComparisonService


def _draft_workspace(client, csrf: str, key: str) -> str:
    response = client.post(
        "/api/v2/workspaces",
        headers=_headers(csrf, key),
        json={"name": key},
    )
    assert response.status_code == 201
    return response.json()["id"]


def _active_catalog(
    settings, *, source_item_id: str = "H-1", title: str = "기준 장서"
) -> str:
    with connect(settings.database_path) as connection:
        version = CatalogSyncService(
            connection, _allow_unbound_sources=True
        ).import_full_snapshot(
            school_id="550e8400-e29b-41d4-a716-446655440100",
            source_type=SourceType.DLS_MARC,
            records=(CatalogRecord(source_item_id=source_item_id, title=title),),
            confirm_anomaly=True,
            _allow_unbound_source=True,
        )
        connection.commit()
        return version.id


def test_partial_upload_creates_durable_repair_obligation_and_blocks_comparison(
    data_dir,
) -> None:
    client, settings, csrf = _client(data_dir, raise_server_exceptions=False)
    workspace_id = _draft_workspace(client, csrf, "repair-workspace")
    _active_catalog(settings)

    partial = client.post(
        f"/api/v2/workspaces/{workspace_id}/sources",
        headers=_headers(csrf, "repair-batch"),
        data={"role": "PURCHASE_REQUEST"},
        files=[
            ("files", ("ready.csv", "제목,저자\n준비된 책,저자\n", "text/csv")),
            ("files", ("broken.exe", b"MZ-broken", "application/octet-stream")),
        ],
    )
    assert partial.status_code == 207
    failed = next(
        item for item in partial.json()["items"] if item["status"] == "FAILED"
    )
    assert failed["repair_obligation_id"]
    accepted_id = next(
        item["source_id"]
        for item in partial.json()["items"]
        if item["status"] == "ACCEPTED"
    )
    with connect(settings.database_path) as connection:
        assert build_job_runner(connection).run_once().status == "SUCCEEDED"

    blocked = client.post(
        f"/api/v2/workspaces/{workspace_id}/comparison-jobs",
        headers=_headers(csrf, "compare-before-repair", version=1),
        json={"source_document_ids": [accepted_id]},
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "UPLOAD_REPAIR_REQUIRED"

    replacement = client.post(
        f"/api/v2/workspaces/{workspace_id}/sources",
        headers=_headers(csrf, "repair-replacement"),
        data={
            "role": "PURCHASE_REQUEST",
            "repair_obligation_id": failed["repair_obligation_id"],
            "repair_generation": "1",
        },
        files=[("files", ("fixed.csv", "제목,저자\n수정된 책,저자\n", "text/csv"))],
    )
    assert replacement.status_code == 202
    assert (
        replacement.json()["items"][0]["repair_obligation_id"]
        == failed["repair_obligation_id"]
    )
    replacement_replay = client.post(
        f"/api/v2/workspaces/{workspace_id}/sources",
        headers=_headers(csrf, "repair-replacement"),
        data={
            "role": "PURCHASE_REQUEST",
            "repair_obligation_id": failed["repair_obligation_id"],
            "repair_generation": "1",
        },
        files=[("files", ("fixed.csv", "제목,저자\n수정된 책,저자\n", "text/csv"))],
    )
    assert replacement_replay.status_code == 202
    assert replacement_replay.json() == replacement.json()
    with connect(settings.database_path) as connection:
        assert build_job_runner(connection).run_once().status == "SUCCEEDED"

    comparison = client.post(
        f"/api/v2/workspaces/{workspace_id}/comparison-jobs",
        headers=_headers(csrf, "compare-after-repair", version=1),
        json={
            "source_document_ids": [
                accepted_id,
                replacement.json()["items"][0]["source_id"],
            ]
        },
    )
    assert comparison.status_code == 202
    with connect(settings.database_path) as connection:
        obligation = connection.execute(
            "SELECT status, generation, resolved_source_document_id FROM upload_repair_obligations WHERE id = ?",
            (failed["repair_obligation_id"],),
        ).fetchone()
        audit_count = connection.execute(
            "SELECT COUNT(*) FROM audit_events WHERE action = 'UPLOAD_REPAIR_RESOLVED'"
        ).fetchone()[0]
    assert dict(obligation) == {
        "status": "RESOLVED",
        "generation": 1,
        "resolved_source_document_id": replacement.json()["items"][0]["source_id"],
    }
    assert audit_count == 1


def test_terminal_legacy_comparison_reopens_workspace_for_required_repair(
    data_dir,
) -> None:
    client, settings, csrf = _client(data_dir, raise_server_exceptions=False)
    workspace_id = _draft_workspace(client, csrf, "legacy-repair-recovery")
    _active_catalog(settings)
    partial = client.post(
        f"/api/v2/workspaces/{workspace_id}/sources",
        headers=_headers(csrf, "legacy-repair-partial"),
        data={"role": "PURCHASE_REQUEST"},
        files=[
            ("files", ("ready.csv", "제목\n준비된 책\n", "text/csv")),
            ("files", ("broken.exe", b"MZ-legacy", "application/octet-stream")),
        ],
    )
    accepted_id = next(
        item["source_id"]
        for item in partial.json()["items"]
        if item["status"] == "ACCEPTED"
    )
    failed = next(
        item for item in partial.json()["items"] if item["status"] == "FAILED"
    )
    with connect(settings.database_path) as connection:
        assert build_job_runner(connection).run_once().status == "SUCCEEDED"
        repository = JobRepository(connection)
        legacy_compare = repository.create(
            school_id="550e8400-e29b-41d4-a716-446655440100",
            workspace_id=workspace_id,
            job_type="COMPARE",
            payload={"source_document_ids": [accepted_id]},
            progress_total=1,
        )
        connection.commit()
        claimed = repository.claim_next()
        assert claimed is not None and claimed.id == legacy_compare.id
        repository.mark_failed(
            claimed.id,
            claim_token=claimed.claim_token,
            error={"code": "JOB_FAILED", "message": "legacy comparison failed"},
        )
        connection.execute(
            """
            UPDATE acquisition_workspaces
            SET status = 'ANALYZING', row_version = row_version + 1
            WHERE id = ?
            """,
            (workspace_id,),
        )
        connection.commit()

    repaired = client.post(
        f"/api/v2/workspaces/{workspace_id}/sources",
        headers=_headers(csrf, "legacy-repair-replacement"),
        data={
            "role": "PURCHASE_REQUEST",
            "repair_obligation_id": failed["repair_obligation_id"],
            "repair_generation": "1",
        },
        files=[("files", ("fixed.csv", "제목\n복구된 책\n", "text/csv"))],
    )

    assert repaired.status_code == 202, repaired.json()
    current = client.get(f"/api/v2/workspaces/{workspace_id}").json()
    assert current["status"] == "DRAFT"
    with connect(settings.database_path) as connection:
        assert build_job_runner(connection).run_once().status == "SUCCEEDED"
        reopened_audits = connection.execute(
            """
            SELECT COUNT(*) FROM audit_events
            WHERE action = 'UPLOAD_REPAIR_REOPENED_WORKSPACE'
              AND entity_id = ?
            """,
            (workspace_id,),
        ).fetchone()[0]
    assert reopened_audits == 1
    source_ids = [
        source["id"]
        for source in client.get(f"/api/v2/workspaces/{workspace_id}/sources").json()[
            "items"
        ]
        if source["role"] == "PURCHASE_REQUEST"
    ]
    comparison = client.post(
        f"/api/v2/workspaces/{workspace_id}/comparison-jobs",
        headers=_headers(
            csrf, "legacy-repair-new-comparison", version=current["row_version"]
        ),
        json={"source_document_ids": source_ids},
    )
    assert comparison.status_code == 202


def test_comparison_rejects_an_older_subset_of_the_workspace_purchase_sources(
    data_dir,
) -> None:
    client, settings, csrf = _client(data_dir, raise_server_exceptions=False)
    workspace_id = _draft_workspace(client, csrf, "comparison-source-set-fence")
    _active_catalog(settings)
    uploaded = client.post(
        f"/api/v2/workspaces/{workspace_id}/sources",
        headers=_headers(csrf, "source-set-upload"),
        data={"role": "PURCHASE_REQUEST"},
        files=[
            ("files", ("older.csv", "제목\n먼저 올린 책\n", "text/csv")),
            ("files", ("replacement.csv", "제목\n나중에 반영된 책\n", "text/csv")),
        ],
    )
    assert uploaded.status_code == 202
    source_ids = [item["source_id"] for item in uploaded.json()["items"]]
    with connect(settings.database_path) as connection:
        assert build_job_runner(connection).run_once().status == "SUCCEEDED"

    stale_subset = client.post(
        f"/api/v2/workspaces/{workspace_id}/comparison-jobs",
        headers=_headers(csrf, "source-set-stale", version=1),
        json={"source_document_ids": source_ids[:1]},
    )

    assert stale_subset.status_code == 409
    assert stale_subset.json()["detail"]["code"] == "COMPARISON_SOURCE_SET_CHANGED"
    current_set = client.post(
        f"/api/v2/workspaces/{workspace_id}/comparison-jobs",
        headers=_headers(csrf, "source-set-current", version=1),
        json={"source_document_ids": source_ids},
    )
    assert current_set.status_code == 202


def test_unresolved_repair_obligations_are_discoverable_after_reload(data_dir) -> None:
    client, _, csrf = _client(data_dir, raise_server_exceptions=False)
    workspace_id = _draft_workspace(client, csrf, "repair-reload")
    partial = client.post(
        f"/api/v2/workspaces/{workspace_id}/sources",
        headers=_headers(csrf, "repair-reload-upload"),
        data={"role": "PURCHASE_REQUEST"},
        files=[("files", ("broken.exe", b"MZ-broken", "application/octet-stream"))],
    )
    failed = partial.json()["items"][0]

    repairs = client.get(f"/api/v2/workspaces/{workspace_id}/upload-repairs")

    assert repairs.status_code == 200
    assert repairs.json() == {
        "items": [
            {
                "id": failed["repair_obligation_id"],
                "filename": "broken.exe",
                "error": {
                    "code": "UNSUPPORTED_FILE_TYPE",
                    "message": "지원하지 않는 파일 형식입니다.",
                },
                "status": "UNRESOLVED",
                "generation": 0,
                "role": "PURCHASE_REQUEST",
                "vendor_scope": "*",
                "requested_start_local_date": None,
                "requested_through_local_date": None,
                "configuration_confirmation_required": False,
                "resolved_source_id": None,
                "created_at": repairs.json()["items"][0]["created_at"],
                "updated_at": repairs.json()["items"][0]["updated_at"],
            }
        ],
        "next_cursor": None,
    }


def test_repair_binds_the_original_vendor_scope_when_the_client_omits_it(
    data_dir,
) -> None:
    client, settings, csrf = _client(data_dir, raise_server_exceptions=False)
    workspace_id = _draft_workspace(client, csrf, "vendor-repair-config")
    rejected = client.post(
        f"/api/v2/workspaces/{workspace_id}/sources",
        headers=_headers(csrf, "vendor-repair-rejected"),
        data={"role": "VENDOR_QUOTE", "vendor_scope": "vendor-a"},
        files=[("files", ("quote.exe", b"MZ-vendor", "application/octet-stream"))],
    )
    assert rejected.status_code == 207
    obligation_id = rejected.json()["items"][0]["repair_obligation_id"]

    repaired = client.post(
        f"/api/v2/workspaces/{workspace_id}/sources",
        headers=_headers(csrf, "vendor-repair-fixed"),
        data={
            "role": "VENDOR_QUOTE",
            "repair_obligation_id": obligation_id,
            "repair_generation": "1",
        },
        files=[("files", ("quote.csv", "제목\n견적 책\n", "text/csv"))],
    )

    assert repaired.status_code == 202
    source_id = repaired.json()["items"][0]["source_id"]
    with connect(settings.database_path) as connection:
        configuration = connection.execute(
            "SELECT role, vendor_scope FROM source_configurations WHERE source_document_id = ?",
            (source_id,),
        ).fetchone()
    assert dict(configuration) == {
        "role": "VENDOR_QUOTE",
        "vendor_scope": "vendor-a",
    }


def test_repair_rejects_client_configuration_that_conflicts_with_the_obligation(
    data_dir,
) -> None:
    client, _, csrf = _client(data_dir, raise_server_exceptions=False)
    workspace_id = _draft_workspace(client, csrf, "repair-config-conflict")
    rejected = client.post(
        f"/api/v2/workspaces/{workspace_id}/sources",
        headers=_headers(csrf, "repair-config-conflict-rejected"),
        data={"role": "VENDOR_QUOTE", "vendor_scope": "vendor-a"},
        files=[("files", ("quote.exe", b"MZ-vendor", "application/octet-stream"))],
    )
    obligation_id = rejected.json()["items"][0]["repair_obligation_id"]

    conflict = client.post(
        f"/api/v2/workspaces/{workspace_id}/sources",
        headers=_headers(csrf, "repair-config-conflict-fixed"),
        data={
            "role": "VENDOR_QUOTE",
            "vendor_scope": "vendor-b",
            "repair_obligation_id": obligation_id,
            "repair_generation": "1",
        },
        files=[("files", ("quote.csv", "제목\n견적 책\n", "text/csv"))],
    )

    assert conflict.status_code == 422
    assert conflict.json()["detail"]["code"] == "UPLOAD_REPAIR_CONFIG_MISMATCH"


def test_repair_binds_the_original_catalog_delta_window_when_omitted(data_dir) -> None:
    client, settings, csrf = _client(data_dir, raise_server_exceptions=False)
    workspace_id = _draft_workspace(client, csrf, "delta-repair-config")
    rejected = client.post(
        f"/api/v2/workspaces/{workspace_id}/sources",
        headers=_headers(csrf, "delta-repair-rejected"),
        data={
            "role": "CATALOG_DELTA_REGISTRATION",
            "requested_start_local_date": "2026-08-01",
            "requested_through_local_date": "2026-08-29",
        },
        files=[("files", ("delta.exe", b"MZ-delta", "application/octet-stream"))],
    )
    assert rejected.status_code == 207
    obligation_id = rejected.json()["items"][0]["repair_obligation_id"]

    repaired = client.post(
        f"/api/v2/workspaces/{workspace_id}/sources",
        headers=_headers(csrf, "delta-repair-fixed"),
        data={
            "role": "CATALOG_DELTA_REGISTRATION",
            "repair_obligation_id": obligation_id,
            "repair_generation": "1",
        },
        files=[("files", ("delta.csv", "제목\n신규 책\n", "text/csv"))],
    )

    assert repaired.status_code == 202
    source_id = repaired.json()["items"][0]["source_id"]
    with connect(settings.database_path) as connection:
        source = connection.execute(
            """
            SELECT role, requested_start_local_date, requested_through_local_date
            FROM source_documents WHERE id = ?
            """,
            (source_id,),
        ).fetchone()
    assert dict(source) == {
        "role": "CATALOG_DELTA_REGISTRATION",
        "requested_start_local_date": "2026-08-01",
        "requested_through_local_date": "2026-08-29",
    }


def test_repair_generation_reservation_rechecks_workspace_under_writer_lock(
    data_dir,
) -> None:
    from suseoro.api.routes import sources as source_routes

    client, settings, csrf = _client(data_dir, raise_server_exceptions=False)
    workspace_id = _draft_workspace(client, csrf, "repair-state-race")
    partial = client.post(
        f"/api/v2/workspaces/{workspace_id}/sources",
        headers=_headers(csrf, "repair-state-race-upload"),
        data={"role": "PURCHASE_REQUEST"},
        files=[("files", ("broken.exe", b"MZ-broken", "application/octet-stream"))],
    )
    obligation_id = partial.json()["items"][0]["repair_obligation_id"]
    with connect(settings.database_path) as connection:
        obligation = connection.execute(
            "SELECT upload_claim_id FROM upload_repair_obligations WHERE id = ?",
            (obligation_id,),
        ).fetchone()
        connection.execute(
            "UPDATE acquisition_workspaces SET status = 'ANALYZING' WHERE id = ?",
            (workspace_id,),
        )
        connection.commit()

    with pytest.raises(HTTPException) as raised:
        source_routes._reserve_repair_generation(
            settings.database_path,
            obligation_id=obligation_id,
            school_id="550e8400-e29b-41d4-a716-446655440100",
            workspace_id=workspace_id,
            generation=1,
            role=DocumentRole.PURCHASE_REQUEST,
            vendor_scope="*",
            requested_start_local_date=None,
            requested_through_local_date=None,
            upload_claim_id=obligation["upload_claim_id"],
            actor_id="550e8400-e29b-41d4-a716-446655440200",
            request_id="repair-state-race-request",
        )

    assert raised.value.detail == {"code": "SOURCE_COMPARISON_IN_PROGRESS"}
    with connect(settings.database_path) as connection:
        state = connection.execute(
            "SELECT status, generation, active_upload_claim_id FROM upload_repair_obligations WHERE id = ?",
            (obligation_id,),
        ).fetchone()
    assert dict(state) == {
        "status": "UNRESOLVED",
        "generation": 0,
        "active_upload_claim_id": None,
    }


def test_parse_rechecks_workspace_after_idempotency_reservation(
    data_dir, monkeypatch: pytest.MonkeyPatch
) -> None:
    from suseoro.api.routes import sources as source_routes

    client, settings, csrf = _client(data_dir, raise_server_exceptions=False)
    workspace_id = _draft_workspace(client, csrf, "parse-state-race")
    uploaded = client.post(
        f"/api/v2/workspaces/{workspace_id}/sources",
        headers=_headers(csrf, "parse-state-upload"),
        data={"role": "PURCHASE_REQUEST"},
        files=[("files", ("ready.csv", "제목\n책\n", "text/csv"))],
    )
    source_id = uploaded.json()["items"][0]["source_id"]
    with connect(settings.database_path) as connection:
        assert build_job_runner(connection).run_once().status == "SUCCEEDED"

    original_reserve = source_routes.reserve_idempotency_key

    def reserve_then_transition(connection, **kwargs):
        replay = original_reserve(connection, **kwargs)
        connection.execute(
            "UPDATE acquisition_workspaces SET status = 'ANALYZING' WHERE id = ?",
            (workspace_id,),
        )
        connection.commit()
        return replay

    monkeypatch.setattr(
        source_routes, "reserve_idempotency_key", reserve_then_transition
    )
    parsed = client.post(
        f"/api/v2/sources/{source_id}/parse",
        headers=_headers(csrf, "parse-state-race-request"),
    )

    assert parsed.status_code == 409
    assert parsed.json()["detail"]["code"] == "SOURCE_COMPARISON_IN_PROGRESS"
    with connect(settings.database_path) as connection:
        parse_jobs = connection.execute(
            "SELECT COUNT(*) FROM durable_jobs WHERE job_type = 'PARSE' AND workspace_id = ?",
            (workspace_id,),
        ).fetchone()[0]
    assert parse_jobs == 0


def test_source_and_workspace_job_discovery_survive_reload(data_dir) -> None:
    client, settings, csrf = _client(data_dir)
    workspace_id = _draft_workspace(client, csrf, "reload-discovery")
    uploaded = client.post(
        f"/api/v2/workspaces/{workspace_id}/sources",
        headers=_headers(csrf, "reload-upload"),
        data={"role": "PURCHASE_REQUEST"},
        files=[("files", ("unknown.csv", "내부 열,쓴 사람\n책,저자\n", "text/csv"))],
    )
    with connect(settings.database_path) as connection:
        assert build_job_runner(connection).run_once().status == "PARTIAL"

    sources = client.get(f"/api/v2/workspaces/{workspace_id}/sources")
    jobs = client.get(f"/api/v2/workspaces/{workspace_id}/jobs")
    assert sources.status_code == jobs.status_code == 200
    source = sources.json()["items"][0]
    assert source["latest_job_id"] == uploaded.json()["job_id"]
    assert source["latest_result"]["source_document_id"] == source["id"]
    assert set(source["latest_result"]["error"]) == {"type", "code", "message"}
    assert source["latest_result"]["error"]["type"] is None
    assert source["latest_result"]["mapping_required"]["headers"] == [
        "내부 열",
        "쓴 사람",
    ]
    assert jobs.json()["items"][0]["id"] == uploaded.json()["job_id"]
    assert jobs.json()["items"][0]["items"][0]["mapping_required"] is not None

    with connect(settings.database_path) as connection:
        repository = __import__(
            "suseoro.jobs.repository", fromlist=["JobRepository"]
        ).JobRepository(connection)
        parse_job = repository.create(
            school_id="550e8400-e29b-41d4-a716-446655440100",
            workspace_id=workspace_id,
            job_type="PARSE",
            payload={"source_document_ids": [source["id"]]},
            progress_total=1,
        )
        compare_job = repository.create(
            school_id="550e8400-e29b-41d4-a716-446655440100",
            workspace_id=workspace_id,
            job_type="COMPARE",
            payload={"source_document_ids": [source["id"]]},
            progress_total=1,
        )
        connection.execute(
            "UPDATE durable_jobs SET created_at = '2099-01-01T00:00:00.000000Z' WHERE id = ?",
            (parse_job.id,),
        )
        connection.execute(
            "UPDATE durable_jobs SET created_at = '2100-01-01T00:00:00.000000Z' WHERE id = ?",
            (compare_job.id,),
        )
        connection.commit()

    refreshed_source = client.get(f"/api/v2/workspaces/{workspace_id}/sources").json()[
        "items"
    ][0]
    assert refreshed_source["latest_job_id"] == parse_job.id
    assert refreshed_source["latest_result"] is None
    filtered = client.get(
        f"/api/v2/workspaces/{workspace_id}/jobs?type=COMPARE&status=QUEUED"
    ).json()
    assert [item["id"] for item in filtered["items"]] == [compare_job.id]


def test_ingestion_parser_failure_never_counts_failed_row_as_read(data_dir) -> None:
    client, settings, csrf = _client(data_dir)
    workspace_id = _draft_workspace(client, csrf, "failed-counts")
    uploaded = client.post(
        f"/api/v2/workspaces/{workspace_id}/sources",
        headers=_headers(csrf, "failed-count-upload"),
        data={"role": "PURCHASE_REQUEST"},
        files=[("files", ("vanished.csv", "제목\n책\n", "text/csv"))],
    )
    with connect(settings.database_path) as connection:
        stored = connection.execute(
            "SELECT file.storage_path FROM source_files file JOIN source_documents source ON source.source_file_id = file.id WHERE source.id = ?",
            (uploaded.json()["items"][0]["source_id"],),
        ).fetchone()
        Path(stored["storage_path"]).unlink()
        assert build_job_runner(connection).run_once().status == "FAILED"
    result = client.get(f"/api/v2/jobs/{uploaded.json()['job_id']}").json()["items"][0]
    assert result["status"] == "FAILED"
    assert result["processed_rows"] == 0
    assert result["total_rows"] == 0
    assert "secret.sqlite" not in str(result)


def test_compare_snapshot_rejects_mapping_mutation_and_fences_direct_drift(
    data_dir,
) -> None:
    client, settings, csrf = _client(data_dir, raise_server_exceptions=False)
    workspace_id = _draft_workspace(client, csrf, "snapshot-fence")
    _active_catalog(settings)
    uploaded = client.post(
        f"/api/v2/workspaces/{workspace_id}/sources",
        headers=_headers(csrf, "snapshot-upload"),
        data={"role": "PURCHASE_REQUEST"},
        files=[("files", ("snapshot.csv", "제목,저자\n책,저자\n", "text/csv"))],
    )
    source_id = uploaded.json()["items"][0]["source_id"]
    with connect(settings.database_path) as connection:
        assert build_job_runner(connection).run_once().status == "SUCCEEDED"
    queued = client.post(
        f"/api/v2/workspaces/{workspace_id}/comparison-jobs",
        headers=_headers(csrf, "snapshot-compare", version=1),
        json={"source_document_ids": [source_id]},
    )
    assert queued.status_code == 202
    blocked = client.patch(
        f"/api/v2/sources/{source_id}/mapping",
        headers=_headers(csrf, "snapshot-map", version=1),
        json={"role": "PURCHASE_REQUEST", "mapping": {"제목": "title"}},
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "SOURCE_COMPARISON_IN_PROGRESS"

    blocked_parse = client.post(
        f"/api/v2/sources/{source_id}/parse",
        headers=_headers(csrf, "snapshot-parse"),
    )
    assert blocked_parse.status_code == 409
    assert blocked_parse.json()["detail"]["code"] == "SOURCE_COMPARISON_IN_PROGRESS"
    blocked_upload = client.post(
        f"/api/v2/workspaces/{workspace_id}/sources",
        headers=_headers(csrf, "snapshot-new-upload"),
        data={"role": "PURCHASE_REQUEST"},
        files=[("files", ("late.csv", "제목\n늦은 책\n", "text/csv"))],
    )
    assert blocked_upload.status_code == 409
    assert blocked_upload.json()["detail"]["code"] == "SOURCE_COMPARISON_IN_PROGRESS"

    with connect(settings.database_path) as connection:
        payload = __import__("json").loads(
            connection.execute(
                "SELECT payload_json FROM durable_jobs WHERE id = ?",
                (queued.json()["job_id"],),
            ).fetchone()["payload_json"]
        )
        assert {
            "id",
            "role",
            "status",
            "sha256",
            "config_version",
            "mapping_json",
            "parser_version",
            "completed_at",
            "row_count",
            "row_digest",
        } <= set(payload["source_snapshot"][0])
        assert payload["source_snapshot_version"] == 1
        connection.execute(
            "UPDATE source_configurations SET row_version = row_version + 1 WHERE source_document_id = ?",
            (source_id,),
        )
        connection.commit()
        failed = build_job_runner(connection).run_once()
    assert failed.status == "FAILED"
    job = client.get(f"/api/v2/jobs/{queued.json()['job_id']}").json()
    assert job["error"] == {
        "type": None,
        "code": "JOB_FAILED",
        "message": "작업을 처리하지 못했습니다. 다시 시도해 주세요.",
    }
    assert "source_configurations" not in str(job)


def test_comparison_service_rechecks_catalog_snapshot_inside_claim_batch(
    data_dir,
) -> None:
    client, settings, csrf = _client(data_dir, raise_server_exceptions=False)
    workspace_id = _draft_workspace(client, csrf, "catalog-claim-fence")
    first_catalog_id = _active_catalog(settings)
    uploaded = client.post(
        f"/api/v2/workspaces/{workspace_id}/sources",
        headers=_headers(csrf, "catalog-claim-upload"),
        data={"role": "PURCHASE_REQUEST"},
        files=[("files", ("catalog.csv", "제목\n비교할 책\n", "text/csv"))],
    )
    source_id = uploaded.json()["items"][0]["source_id"]
    with connect(settings.database_path) as connection:
        assert build_job_runner(connection).run_once().status == "SUCCEEDED"
    queued = client.post(
        f"/api/v2/workspaces/{workspace_id}/comparison-jobs",
        headers=_headers(csrf, "catalog-claim-compare", version=1),
        json={"source_document_ids": [source_id]},
    )
    with connect(settings.database_path) as connection:
        claimed = JobRepository(connection).claim_next()
        assert claimed is not None and claimed.id == queued.json()["job_id"]
        connection.commit()

    second_catalog_id = _active_catalog(
        settings, source_item_id="H-2", title="교체된 장서"
    )
    assert second_catalog_id != first_catalog_id
    with connect(settings.database_path) as connection:
        with pytest.raises(ValueError, match="catalog"):
            ComparisonService(connection).compare_documents(
                school_id="550e8400-e29b-41d4-a716-446655440100",
                workspace_id=workspace_id,
                source_document_ids=(source_id,),
                job_id=claimed.id,
                claim_token=claimed.claim_token,
                claim_generation=claimed.claim_generation,
            )
        persisted = connection.execute(
            "SELECT COUNT(*) FROM comparison_row_results WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchone()[0]
    assert persisted == 0


def test_versioned_compare_retry_resnapshots_after_catalog_rotation(data_dir) -> None:
    client, settings, csrf = _client(data_dir, raise_server_exceptions=False)
    workspace_id = _draft_workspace(client, csrf, "catalog-retry-refresh")
    first_catalog_id = _active_catalog(settings)
    uploaded = client.post(
        f"/api/v2/workspaces/{workspace_id}/sources",
        headers=_headers(csrf, "catalog-retry-upload"),
        data={"role": "PURCHASE_REQUEST"},
        files=[("files", ("retry.csv", "제목\n다시 비교할 책\n", "text/csv"))],
    )
    source_id = uploaded.json()["items"][0]["source_id"]
    with connect(settings.database_path) as connection:
        assert build_job_runner(connection).run_once().status == "SUCCEEDED"
    queued = client.post(
        f"/api/v2/workspaces/{workspace_id}/comparison-jobs",
        headers=_headers(csrf, "catalog-retry-compare", version=1),
        json={"source_document_ids": [source_id]},
    )
    second_catalog_id = _active_catalog(settings, source_item_id="H-2", title="새 장서")
    assert second_catalog_id != first_catalog_id
    with connect(settings.database_path) as connection:
        failed = build_job_runner(connection).run_once()
    assert failed.id == queued.json()["job_id"]
    assert failed.status == "FAILED"

    retried = client.post(
        f"/api/v2/jobs/{failed.id}/retry",
        headers=_headers(csrf, "catalog-retry-command"),
    )
    assert retried.status_code == 202
    with connect(settings.database_path) as connection:
        refreshed_payload = __import__("json").loads(
            connection.execute(
                "SELECT payload_json FROM durable_jobs WHERE id = ?", (failed.id,)
            ).fetchone()["payload_json"]
        )
        assert refreshed_payload["catalog_version_id"] == second_catalog_id
        succeeded = build_job_runner(connection).run_once()
    assert succeeded.status == "SUCCEEDED"
    assert client.get(f"/api/v2/workspaces/{workspace_id}").json()["status"] == (
        "CANDIDATE_REVIEW"
    )


def test_legacy_compare_without_versioned_snapshot_fails_closed_then_recovers(
    data_dir,
) -> None:
    client, settings, csrf = _client(data_dir, raise_server_exceptions=False)
    workspace_id = _draft_workspace(client, csrf, "legacy-snapshot-fence")
    _active_catalog(settings)
    uploaded = client.post(
        f"/api/v2/workspaces/{workspace_id}/sources",
        headers=_headers(csrf, "legacy-snapshot-upload"),
        data={"role": "PURCHASE_REQUEST"},
        files=[("files", ("legacy.csv", "제목\n이전 작업 책\n", "text/csv"))],
    )
    source_id = uploaded.json()["items"][0]["source_id"]
    with connect(settings.database_path) as connection:
        assert build_job_runner(connection).run_once().status == "SUCCEEDED"
        ComparisonService(connection).compare_documents(
            school_id="550e8400-e29b-41d4-a716-446655440100",
            workspace_id=workspace_id,
            source_document_ids=(source_id,),
        )
        connection.commit()
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM comparison_row_results WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()[0]
            == 1
        )
        connection.execute(
            "UPDATE acquisition_workspaces SET status = 'ANALYZING' WHERE id = ?",
            (workspace_id,),
        )
        legacy = JobRepository(connection).create(
            school_id="550e8400-e29b-41d4-a716-446655440100",
            workspace_id=workspace_id,
            job_type="COMPARE",
            payload={"source_document_ids": [source_id]},
            progress_total=1,
        )
        connection.commit()
        failed = build_job_runner(connection).run_once()
        stale_candidate_count = connection.execute(
            "SELECT COUNT(*) FROM candidate_decisions WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchone()[0]

    assert failed.id == legacy.id
    assert failed.status == "FAILED"
    assert stale_candidate_count == 1
    public = client.get(f"/api/v2/jobs/{legacy.id}").json()
    assert public["error"] == {
        "type": None,
        "code": "JOB_FAILED",
        "message": "작업을 처리하지 못했습니다. 다시 시도해 주세요.",
    }

    retried = client.post(
        f"/api/v2/jobs/{legacy.id}/retry",
        headers=_headers(csrf, "legacy-snapshot-retry"),
    )
    assert retried.status_code == 202
    with connect(settings.database_path) as connection:
        upgraded_payload = __import__("json").loads(
            connection.execute(
                "SELECT payload_json FROM durable_jobs WHERE id = ?", (legacy.id,)
            ).fetchone()["payload_json"]
        )
        assert upgraded_payload["source_snapshot_version"] == 1
        assert upgraded_payload["source_snapshot"][0]["id"] == source_id
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM comparison_row_results WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()[0]
            == 0
        )
        succeeded = build_job_runner(connection).run_once()
        rebuilt_row_count = connection.execute(
            "SELECT COUNT(*) FROM comparison_row_results WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchone()[0]

    assert succeeded.id == legacy.id
    assert succeeded.status == "SUCCEEDED"
    assert rebuilt_row_count == 1
    refreshed = client.get(f"/api/v2/workspaces/{workspace_id}")
    assert refreshed.json()["status"] == "CANDIDATE_REVIEW"


def test_legacy_compare_retry_expands_a_stale_subset_to_all_purchase_sources(
    data_dir,
) -> None:
    client, settings, csrf = _client(data_dir, raise_server_exceptions=False)
    workspace_id = _draft_workspace(client, csrf, "legacy-source-set-refresh")
    _active_catalog(settings)
    uploaded = client.post(
        f"/api/v2/workspaces/{workspace_id}/sources",
        headers=_headers(csrf, "legacy-source-set-upload"),
        data={"role": "PURCHASE_REQUEST"},
        files=[
            ("files", ("older.csv", "제목\n이전 책\n", "text/csv")),
            ("files", ("newer.csv", "제목\n추가된 책\n", "text/csv")),
        ],
    )
    source_ids = [item["source_id"] for item in uploaded.json()["items"]]
    with connect(settings.database_path) as connection:
        assert build_job_runner(connection).run_once().status == "SUCCEEDED"
        connection.execute(
            "UPDATE acquisition_workspaces SET status = 'ANALYZING' WHERE id = ?",
            (workspace_id,),
        )
        legacy = JobRepository(connection).create(
            school_id="550e8400-e29b-41d4-a716-446655440100",
            workspace_id=workspace_id,
            job_type="COMPARE",
            payload={"source_document_ids": source_ids[:1]},
            progress_total=1,
        )
        connection.commit()
        assert build_job_runner(connection).run_once().status == "FAILED"

    retried = client.post(
        f"/api/v2/jobs/{legacy.id}/retry",
        headers=_headers(csrf, "legacy-source-set-retry"),
    )
    assert retried.status_code == 202
    with connect(settings.database_path) as connection:
        refreshed_payload = __import__("json").loads(
            connection.execute(
                "SELECT payload_json FROM durable_jobs WHERE id = ?", (legacy.id,)
            ).fetchone()["payload_json"]
        )
        assert set(refreshed_payload["source_document_ids"]) == set(source_ids)
        assert {item["id"] for item in refreshed_payload["source_snapshot"]} == set(
            source_ids
        )
        succeeded = build_job_runner(connection).run_once()
        compared_rows = connection.execute(
            "SELECT COUNT(*) FROM comparison_row_results WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchone()[0]

    assert succeeded.status == "SUCCEEDED"
    assert compared_rows == 2
    assert client.get(f"/api/v2/workspaces/{workspace_id}").json()["status"] == (
        "CANDIDATE_REVIEW"
    )


def test_comparison_service_rejects_source_outside_job_snapshot(data_dir) -> None:
    client, settings, csrf = _client(data_dir, raise_server_exceptions=False)
    workspace_id = _draft_workspace(client, csrf, "service-snapshot-membership")
    outside_workspace_id = _draft_workspace(
        client, csrf, "service-snapshot-outside-workspace"
    )
    _active_catalog(settings)
    selected_upload = client.post(
        f"/api/v2/workspaces/{workspace_id}/sources",
        headers=_headers(csrf, "service-snapshot-upload"),
        data={"role": "PURCHASE_REQUEST"},
        files=[("files", ("selected.csv", "제목\n선택 책\n", "text/csv"))],
    )
    outside_upload = client.post(
        f"/api/v2/workspaces/{outside_workspace_id}/sources",
        headers=_headers(csrf, "service-snapshot-outside-upload"),
        data={"role": "PURCHASE_REQUEST"},
        files=[("files", ("outside.csv", "제목\n바깥 책\n", "text/csv"))],
    )
    selected_id = selected_upload.json()["items"][0]["source_id"]
    outside_id = outside_upload.json()["items"][0]["source_id"]
    with connect(settings.database_path) as connection:
        assert build_job_runner(connection).run_once().status == "SUCCEEDED"
        assert build_job_runner(connection).run_once().status == "SUCCEEDED"
    queued = client.post(
        f"/api/v2/workspaces/{workspace_id}/comparison-jobs",
        headers=_headers(csrf, "service-snapshot-compare", version=1),
        json={"source_document_ids": [selected_id]},
    )
    with connect(settings.database_path) as connection:
        claimed = JobRepository(connection).claim_next()
        assert claimed is not None and claimed.id == queued.json()["job_id"]
        with pytest.raises(ValueError, match="snapshot"):
            ComparisonService(connection).compare_documents(
                school_id="550e8400-e29b-41d4-a716-446655440100",
                workspace_id=workspace_id,
                source_document_ids=(outside_id,),
                job_id=claimed.id,
                claim_token=claimed.claim_token,
                claim_generation=claimed.claim_generation,
            )
        outside_rows = connection.execute(
            "SELECT COUNT(*) FROM comparison_row_results WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchone()[0]
    assert outside_rows == 0


def test_comparison_row_failure_is_publicly_sanitized(data_dir) -> None:
    client, settings, csrf = _client(data_dir, raise_server_exceptions=False)
    workspace_id = _draft_workspace(client, csrf, "comparison-sanitize")
    _active_catalog(settings)
    uploaded = client.post(
        f"/api/v2/workspaces/{workspace_id}/sources",
        headers=_headers(csrf, "sanitize-upload"),
        data={"role": "PURCHASE_REQUEST"},
        files=[
            (
                "files",
                ("sanitize.csv", "제목,저자\n누출 행,저자\n정상 행,저자\n", "text/csv"),
            )
        ],
    )
    source_id = uploaded.json()["items"][0]["source_id"]
    with connect(settings.database_path) as connection:
        assert build_job_runner(connection).run_once().status == "SUCCEEDED"
        connection.execute(
            """
            CREATE TRIGGER leak_comparison_error BEFORE INSERT ON candidate_decisions
            WHEN (SELECT original_title FROM recommendations WHERE id = NEW.recommendation_id) = '누출 행'
            BEGIN SELECT RAISE(ABORT, 'no such table secret_table at C:/private/catalog.sqlite SQL SELECT'); END
            """
        )
        connection.commit()
    queued = client.post(
        f"/api/v2/workspaces/{workspace_id}/comparison-jobs",
        headers=_headers(csrf, "sanitize-compare", version=1),
        json={"source_document_ids": [source_id]},
    )
    with connect(settings.database_path) as connection:
        build_job_runner(connection).run_once()
    job = client.get(f"/api/v2/jobs/{queued.json()['job_id']}").json()
    serialized = str(job)
    assert job["items"][0]["error"] == {
        "type": None,
        "code": "COMPARISON_FILE_FAILED",
        "message": "일부 책을 비교하지 못했습니다. 다시 시도해 주세요.",
    }
    for secret in ("IntegrityError", "secret_table", "catalog.sqlite", "SQL SELECT"):
        assert secret not in serialized

    with connect(settings.database_path) as connection:
        # Seed the exact historical payload that predates the current public
        # error producer.  The current claim trigger correctly prevents a
        # terminal row rewrite through normal production paths.
        connection.execute("DROP TRIGGER job_file_results_scope_update")
        connection.execute(
            "UPDATE durable_jobs SET error_json = ? WHERE id = ?",
            (
                '{"type":"OperationalError","code":"RAW","message":"no such table private_schema C:/secret.sqlite SELECT *"}',
                queued.json()["job_id"],
            ),
        )
        connection.execute(
            "UPDATE job_file_results SET error_json = ? WHERE job_id = ?",
            (
                '{"type":"IntegrityError","message":"private_table at D:/hidden.sqlite"}',
                queued.json()["job_id"],
            ),
        )
        connection.commit()
    historical = client.get(f"/api/v2/jobs/{queued.json()['job_id']}").json()
    assert historical["error"] == {
        "type": None,
        "code": "JOB_FAILED",
        "message": "작업을 처리하지 못했습니다. 다시 시도해 주세요.",
    }
    assert historical["items"][0]["error"] == {
        "type": None,
        "code": "COMPARISON_FILE_FAILED",
        "message": "일부 책을 비교하지 못했습니다. 다시 시도해 주세요.",
    }
    for secret in ("OperationalError", "private_schema", "secret.sqlite", "SELECT"):
        assert secret not in str(historical)


def test_active_source_processing_blocks_comparison_creation(data_dir) -> None:
    client, settings, csrf = _client(data_dir, raise_server_exceptions=False)
    workspace_id = _draft_workspace(client, csrf, "active-parse-fence")
    _active_catalog(settings)
    uploaded = client.post(
        f"/api/v2/workspaces/{workspace_id}/sources",
        headers=_headers(csrf, "active-upload"),
        data={"role": "PURCHASE_REQUEST"},
        files=[("files", ("active.csv", "제목\n책\n", "text/csv"))],
    )
    source_id = uploaded.json()["items"][0]["source_id"]
    with connect(settings.database_path) as connection:
        assert build_job_runner(connection).run_once().status == "SUCCEEDED"
    parse = client.post(
        f"/api/v2/sources/{source_id}/parse",
        headers=_headers(csrf, "active-reparse"),
    )
    assert parse.status_code == 202

    blocked = client.post(
        f"/api/v2/workspaces/{workspace_id}/comparison-jobs",
        headers=_headers(csrf, "active-compare", version=1),
        json={"source_document_ids": [source_id]},
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "SOURCE_PROCESSING_IN_PROGRESS"


def test_terminal_ingest_and_parse_jobs_cannot_retry_after_comparison_starts(
    data_dir,
) -> None:
    client, settings, csrf = _client(data_dir, raise_server_exceptions=False)
    workspace_id = _draft_workspace(client, csrf, "processing-retry-state-fence")
    _active_catalog(settings)
    uploaded = client.post(
        f"/api/v2/workspaces/{workspace_id}/sources",
        headers=_headers(csrf, "processing-retry-upload"),
        data={"role": "PURCHASE_REQUEST"},
        files=[("files", ("retry.csv", "제목\n재시도 차단 책\n", "text/csv"))],
    )
    source_id = uploaded.json()["items"][0]["source_id"]
    with connect(settings.database_path) as connection:
        assert build_job_runner(connection).run_once().status == "SUCCEEDED"
        repository = JobRepository(connection)
        failed_job_ids = []
        for job_type in ("INGEST", "PARSE"):
            failed = repository.create(
                school_id="550e8400-e29b-41d4-a716-446655440100",
                workspace_id=workspace_id,
                job_type=job_type,
                payload={"source_document_ids": [source_id]},
                progress_total=1,
            )
            connection.commit()
            claimed = repository.claim_next()
            assert claimed is not None and claimed.id == failed.id
            repository.mark_failed(
                claimed.id,
                claim_token=claimed.claim_token,
                error={"code": "JOB_FAILED", "message": "retryable"},
            )
            failed_job_ids.append(failed.id)
            connection.commit()
    comparison = client.post(
        f"/api/v2/workspaces/{workspace_id}/comparison-jobs",
        headers=_headers(csrf, "processing-retry-compare", version=1),
        json={"source_document_ids": [source_id]},
    )
    assert comparison.status_code == 202

    for job_id, key in zip(
        failed_job_ids, ("retry-old-ingest", "retry-old-parse"), strict=True
    ):
        retried = client.post(
            f"/api/v2/jobs/{job_id}/retry",
            headers=_headers(csrf, key),
        )
        assert retried.status_code == 409
        assert retried.json()["detail"]["code"] == "SOURCE_COMPARISON_IN_PROGRESS"


def test_source_retry_reloads_state_after_reservation_before_requeue(
    data_dir,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from suseoro.api.routes import sources as source_routes

    client, settings, csrf = _client(data_dir, raise_server_exceptions=False)
    workspace_id = _draft_workspace(client, csrf, "retry-reservation-state-fence")
    _active_catalog(settings)
    uploaded = client.post(
        f"/api/v2/workspaces/{workspace_id}/sources",
        headers=_headers(csrf, "retry-race-upload"),
        data={"role": "PURCHASE_REQUEST"},
        files=[("files", ("race.csv", "제목\n경합 책\n", "text/csv"))],
    )
    source_id = uploaded.json()["items"][0]["source_id"]
    with connect(settings.database_path) as connection:
        assert build_job_runner(connection).run_once().status == "SUCCEEDED"
        repository = JobRepository(connection)
        target = repository.create(
            school_id="550e8400-e29b-41d4-a716-446655440100",
            workspace_id=workspace_id,
            job_type="PARSE",
            payload={"source_document_ids": [source_id]},
            progress_total=1,
        )
        connection.commit()
        running = repository.claim_next()
        assert running is not None and running.id == target.id
        claim_token = running.claim_token
        connection.commit()

    retry_reached_reservation = Event()
    allow_retry_reservation = Event()
    original_reserve = source_routes.reserve_idempotency_key

    def pause_retry_reservation(connection, **kwargs):
        if kwargs["route"].endswith(f"/jobs/{target.id}/retry"):
            retry_reached_reservation.set()
            assert allow_retry_reservation.wait(timeout=5)
        return original_reserve(connection, **kwargs)

    monkeypatch.setattr(
        source_routes, "reserve_idempotency_key", pause_retry_reservation
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending_retry = pool.submit(
            client.post,
            f"/api/v2/jobs/{target.id}/retry",
            headers=_headers(csrf, "retry-race-request"),
        )
        assert retry_reached_reservation.wait(timeout=5)
        with connect(settings.database_path) as connection:
            JobRepository(connection).mark_failed(
                target.id,
                claim_token=claim_token,
                error={"code": "JOB_FAILED", "message": "retryable"},
            )
            connection.commit()
        comparison = client.post(
            f"/api/v2/workspaces/{workspace_id}/comparison-jobs",
            headers=_headers(csrf, "retry-race-compare", version=1),
            json={"source_document_ids": [source_id]},
        )
        assert comparison.status_code == 202
        allow_retry_reservation.set()
        retried = pending_retry.result(timeout=5)

    assert retried.status_code == 409
    assert retried.json()["detail"]["code"] == "SOURCE_COMPARISON_IN_PROGRESS"
    with connect(settings.database_path) as connection:
        assert JobRepository(connection).get(target.id).status == "FAILED"


def test_source_upload_mapping_and_parse_are_fenced_after_candidates_exist(
    data_dir,
) -> None:
    client, settings, csrf = _client(data_dir, raise_server_exceptions=False)
    workspace_id = _draft_workspace(client, csrf, "candidate-source-state-fence")
    _active_catalog(settings)
    uploaded = client.post(
        f"/api/v2/workspaces/{workspace_id}/sources",
        headers=_headers(csrf, "candidate-fence-upload"),
        data={"role": "PURCHASE_REQUEST"},
        files=[("files", ("ready.csv", "제목\n후보 책\n", "text/csv"))],
    )
    source_id = uploaded.json()["items"][0]["source_id"]
    with connect(settings.database_path) as connection:
        assert build_job_runner(connection).run_once().status == "SUCCEEDED"
    comparison = client.post(
        f"/api/v2/workspaces/{workspace_id}/comparison-jobs",
        headers=_headers(csrf, "candidate-fence-compare", version=1),
        json={"source_document_ids": [source_id]},
    )
    assert comparison.status_code == 202
    with connect(settings.database_path) as connection:
        assert build_job_runner(connection).run_once().status == "SUCCEEDED"
        before = {
            "sources": connection.execute(
                "SELECT COUNT(*) FROM workspace_sources WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()[0],
            "jobs": connection.execute(
                "SELECT COUNT(*) FROM durable_jobs WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()[0],
            "mapping": connection.execute(
                "SELECT mapping_json, row_version FROM source_configurations WHERE source_document_id = ?",
                (source_id,),
            ).fetchone(),
        }

    late_upload = client.post(
        f"/api/v2/workspaces/{workspace_id}/sources",
        headers=_headers(csrf, "candidate-fence-late-upload"),
        data={"role": "PURCHASE_REQUEST"},
        files=[("files", ("late.csv", "제목\n늦은 책\n", "text/csv"))],
    )
    late_mapping = client.patch(
        f"/api/v2/sources/{source_id}/mapping",
        headers=_headers(csrf, "candidate-fence-late-mapping", version=1),
        json={
            "role": "VENDOR_QUOTE",
            "mapping": {"제목": "title"},
            "remember_template": False,
            "vendor_scope": "*",
        },
    )
    late_parse = client.post(
        f"/api/v2/sources/{source_id}/parse",
        headers=_headers(csrf, "candidate-fence-late-parse"),
    )

    for blocked in (late_upload, late_mapping, late_parse):
        assert blocked.status_code == 409
        assert blocked.json()["detail"]["code"] == "SOURCE_COMPARISON_IN_PROGRESS"
    with connect(settings.database_path) as connection:
        after = {
            "sources": connection.execute(
                "SELECT COUNT(*) FROM workspace_sources WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()[0],
            "jobs": connection.execute(
                "SELECT COUNT(*) FROM durable_jobs WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()[0],
            "mapping": connection.execute(
                "SELECT mapping_json, row_version FROM source_configurations WHERE source_document_id = ?",
                (source_id,),
            ).fetchone(),
        }
    assert after["sources"] == before["sources"]
    assert after["jobs"] == before["jobs"]
    assert dict(after["mapping"]) == dict(before["mapping"])

    vendor_upload = client.post(
        f"/api/v2/workspaces/{workspace_id}/sources",
        headers=_headers(csrf, "candidate-fence-vendor-upload"),
        data={"role": "VENDOR_QUOTE"},
        files=[("files", ("quote.csv", "제목,가격\n견적 책,10000\n", "text/csv"))],
    )
    assert vendor_upload.status_code == 202
    vendor_source_id = vendor_upload.json()["items"][0]["source_id"]
    with connect(settings.database_path) as connection:
        assert build_job_runner(connection).run_once() is not None
    vendor_to_purchase = client.patch(
        f"/api/v2/sources/{vendor_source_id}/mapping",
        headers=_headers(csrf, "candidate-fence-vendor-to-purchase", version=1),
        json={
            "role": "PURCHASE_REQUEST",
            "mapping": {"제목": "title"},
            "remember_template": False,
            "vendor_scope": "vendor-a",
        },
    )
    assert vendor_to_purchase.status_code == 409
    assert (
        vendor_to_purchase.json()["detail"]["code"] == "SOURCE_COMPARISON_IN_PROGRESS"
    )
    vendor_mapping = client.patch(
        f"/api/v2/sources/{vendor_source_id}/mapping",
        headers=_headers(csrf, "candidate-fence-vendor-mapping", version=1),
        json={
            "role": "VENDOR_QUOTE",
            "mapping": {"제목": "title"},
            "remember_template": False,
            "vendor_scope": "vendor-a",
        },
    )
    assert vendor_mapping.status_code == 200
    vendor_parse = client.post(
        f"/api/v2/sources/{vendor_source_id}/parse",
        headers=_headers(csrf, "candidate-fence-vendor-parse"),
    )
    assert vendor_parse.status_code == 202


def test_unknown_repair_requires_confirmation_then_binds_delta_scope(
    data_dir,
) -> None:
    client, settings, csrf = _client(data_dir, raise_server_exceptions=False)
    workspace_id = _draft_workspace(client, csrf, "legacy-unknown-repair-binding")
    rejected = client.post(
        f"/api/v2/workspaces/{workspace_id}/sources",
        headers=_headers(csrf, "legacy-unknown-rejected"),
        files=[("files", ("legacy.exe", b"MZ-legacy", "application/octet-stream"))],
    )
    obligation_id = rejected.json()["items"][0]["repair_obligation_id"]
    with connect(settings.database_path) as connection:
        before = connection.execute(
            """
            SELECT role, vendor_scope, requested_start_local_date,
                   requested_through_local_date, generation
            FROM upload_repair_obligations WHERE id = ?
            """,
            (obligation_id,),
        ).fetchone()
    assert dict(before) == {
        "role": "UNKNOWN",
        "vendor_scope": "*",
        "requested_start_local_date": None,
        "requested_through_local_date": None,
        "generation": 0,
    }

    still_unknown = client.post(
        f"/api/v2/workspaces/{workspace_id}/sources",
        headers=_headers(csrf, "legacy-unknown-still-broken"),
        data={
            "role": "UNKNOWN",
            "repair_obligation_id": obligation_id,
            "repair_generation": "1",
        },
        files=[
            (
                "files",
                ("still-broken.exe", b"MZ-still-broken", "application/octet-stream"),
            )
        ],
    )
    assert still_unknown.status_code == 422
    assert (
        still_unknown.json()["detail"]["code"]
        == "UPLOAD_REPAIR_CONFIGURATION_CONFIRMATION_REQUIRED"
    )

    bound = client.post(
        f"/api/v2/workspaces/{workspace_id}/sources",
        headers=_headers(csrf, "legacy-unknown-bound"),
        data={
            "role": "CATALOG_DELTA_REGISTRATION",
            "vendor_scope": "vendor-a",
            "requested_start_local_date": "2026-08-01",
            "requested_through_local_date": "2026-08-29",
            "repair_obligation_id": obligation_id,
            "repair_generation": "1",
            "confirm_repair_configuration": "true",
        },
        files=[("files", ("registration.csv", "등록번호,제목\n1,책\n", "text/csv"))],
    )
    assert bound.status_code == 202
    with connect(settings.database_path) as connection:
        contract = connection.execute(
            """
            SELECT role, vendor_scope, requested_start_local_date,
                   requested_through_local_date, generation
            FROM upload_repair_obligations WHERE id = ?
            """,
            (obligation_id,),
        ).fetchone()
    assert dict(contract) == {
        "role": "CATALOG_DELTA_REGISTRATION",
        "vendor_scope": "vendor-a",
        "requested_start_local_date": "2026-08-01",
        "requested_through_local_date": "2026-08-29",
        "generation": 1,
    }

    changed_scope = client.post(
        f"/api/v2/workspaces/{workspace_id}/sources",
        headers=_headers(csrf, "legacy-unknown-changed-scope"),
        data={
            "role": "CATALOG_DELTA_REGISTRATION",
            "vendor_scope": "vendor-b",
            "requested_start_local_date": "2026-08-01",
            "requested_through_local_date": "2026-08-29",
            "repair_obligation_id": obligation_id,
            "repair_generation": "2",
        },
        files=[
            ("files", ("registration.csv", "등록번호,제목\n2,다른 책\n", "text/csv"))
        ],
    )
    assert changed_scope.status_code == 422
    assert changed_scope.json()["detail"]["code"] == "UPLOAD_REPAIR_CONFIG_MISMATCH"


def test_partial_comparison_can_replace_source_and_reach_candidates(data_dir) -> None:
    client, settings, csrf = _client(data_dir, raise_server_exceptions=False)
    workspace_id = _draft_workspace(client, csrf, "partial-source-recovery")
    _active_catalog(settings)
    partial_upload = client.post(
        f"/api/v2/workspaces/{workspace_id}/sources",
        headers=_headers(csrf, "partial-source-upload"),
        data={"role": "PURCHASE_REQUEST"},
        files=[
            (
                "files",
                ("partial.csv", "제목,저자\n정상 추천,저자\n,누락\n", "text/csv"),
            )
        ],
    )
    partial_source_id = partial_upload.json()["items"][0]["source_id"]
    with connect(settings.database_path) as connection:
        assert build_job_runner(connection).run_once().status == "PARTIAL"
    compare = client.post(
        f"/api/v2/workspaces/{workspace_id}/comparison-jobs",
        headers=_headers(csrf, "partial-source-compare", version=1),
        json={"source_document_ids": [partial_source_id]},
    )
    with connect(settings.database_path) as connection:
        compared = build_job_runner(connection).run_once()
        assert compared.id == compare.json()["job_id"]
        assert compared.status == "PARTIAL"

    corrected = client.post(
        f"/api/v2/workspaces/{workspace_id}/sources",
        headers=_headers(csrf, "partial-source-corrected"),
        data={
            "role": "PURCHASE_REQUEST",
            "replacement_source_document_id": partial_source_id,
        },
        files=[
            (
                "files",
                (
                    "corrected.csv",
                    "제목,저자\n정상 추천,저자\n수정 추천,저자\n",
                    "text/csv",
                ),
            )
        ],
    )
    assert corrected.status_code == 202
    corrected_replay = client.post(
        f"/api/v2/workspaces/{workspace_id}/sources",
        headers=_headers(csrf, "partial-source-corrected"),
        data={
            "role": "PURCHASE_REQUEST",
            "replacement_source_document_id": partial_source_id,
        },
        files=[
            (
                "files",
                (
                    "corrected.csv",
                    "제목,저자\n정상 추천,저자\n수정 추천,저자\n",
                    "text/csv",
                ),
            )
        ],
    )
    assert corrected_replay.status_code == 202
    assert corrected_replay.json() == corrected.json()
    reopened = client.get(f"/api/v2/workspaces/{workspace_id}").json()
    assert reopened["status"] == "DRAFT"
    corrected_source_id = corrected.json()["items"][0]["source_id"]
    with connect(settings.database_path) as connection:
        assert build_job_runner(connection).run_once().status == "SUCCEEDED"
    next_compare = client.post(
        f"/api/v2/workspaces/{workspace_id}/comparison-jobs",
        headers=_headers(
            csrf, "partial-source-next-compare", version=reopened["row_version"]
        ),
        json={"source_document_ids": [corrected_source_id]},
    )
    assert next_compare.status_code == 202
    with connect(settings.database_path) as connection:
        assert build_job_runner(connection).run_once().status == "SUCCEEDED"
    completed = client.get(f"/api/v2/workspaces/{workspace_id}").json()
    assert completed["status"] == "CANDIDATE_REVIEW"
    sources = client.get(f"/api/v2/workspaces/{workspace_id}/sources").json()["items"]
    roles = {source["id"]: source["role"] for source in sources}
    assert roles[partial_source_id] == "UNKNOWN"
    assert roles[corrected_source_id] == "PURCHASE_REQUEST"


def test_replacement_rejects_a_source_shared_with_a_review_workspace(data_dir) -> None:
    client, settings, csrf = _client(data_dir, raise_server_exceptions=False)
    correction_workspace_id = _draft_workspace(client, csrf, "shared-correction")
    review_workspace_id = _draft_workspace(client, csrf, "shared-review")
    _active_catalog(settings)
    uploaded = client.post(
        f"/api/v2/workspaces/{correction_workspace_id}/sources",
        headers=_headers(csrf, "shared-source-upload"),
        data={"role": "PURCHASE_REQUEST"},
        files=[("files", ("shared.csv", "제목\n공유된 책\n", "text/csv"))],
    )
    source_id = uploaded.json()["items"][0]["source_id"]
    with connect(settings.database_path) as connection:
        assert build_job_runner(connection).run_once().status == "SUCCEEDED"
        connection.execute(
            """
            INSERT INTO workspace_sources (
                workspace_id, source_document_id, school_id, created_at
            )
            SELECT ?, source_document_id, school_id, created_at
            FROM workspace_sources
            WHERE workspace_id = ? AND source_document_id = ?
            """,
            (review_workspace_id, correction_workspace_id, source_id),
        )
        connection.commit()

    review_compare = client.post(
        f"/api/v2/workspaces/{review_workspace_id}/comparison-jobs",
        headers=_headers(csrf, "shared-review-compare", version=1),
        json={"source_document_ids": [source_id]},
    )
    assert review_compare.status_code == 202
    with connect(settings.database_path) as connection:
        assert build_job_runner(connection).run_once().status == "SUCCEEDED"
    assert (
        client.get(f"/api/v2/workspaces/{review_workspace_id}").json()["status"]
        == "CANDIDATE_REVIEW"
    )

    correction_compare = client.post(
        f"/api/v2/workspaces/{correction_workspace_id}/comparison-jobs",
        headers=_headers(csrf, "shared-correction-compare", version=1),
        json={"source_document_ids": [source_id]},
    )
    assert correction_compare.status_code == 202
    with connect(settings.database_path) as connection:
        repository = JobRepository(connection)
        running = repository.claim_next()
        assert running is not None and running.id == correction_compare.json()["job_id"]
        repository.mark_failed(
            running.id,
            claim_token=running.claim_token,
            error={"code": "COMPARISON_FAILED", "message": "비교 실패"},
        )

    rejected = client.post(
        f"/api/v2/workspaces/{correction_workspace_id}/sources",
        headers=_headers(csrf, "shared-source-replacement"),
        data={
            "role": "PURCHASE_REQUEST",
            "replacement_source_document_id": source_id,
        },
        files=[("files", ("corrected.csv", "제목\n수정한 책\n", "text/csv"))],
    )

    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "SOURCE_REPLACEMENT_SHARED"
    assert (
        client.get(f"/api/v2/workspaces/{review_workspace_id}").json()["status"]
        == "CANDIDATE_REVIEW"
    )
    with connect(settings.database_path) as connection:
        role = connection.execute(
            "SELECT role FROM source_configurations WHERE source_document_id = ?",
            (source_id,),
        ).fetchone()["role"]
        replacement_count = connection.execute(
            """
            SELECT COUNT(*) FROM workspace_sources link
            JOIN source_configurations config
              ON config.source_document_id = link.source_document_id
            WHERE link.workspace_id = ? AND config.role = 'PURCHASE_REQUEST'
            """,
            (correction_workspace_id,),
        ).fetchone()[0]
    assert role == "PURCHASE_REQUEST"
    assert replacement_count == 1


def test_failed_new_replacement_preserves_previous_source_until_atomic_swap(
    data_dir,
) -> None:
    client, settings, csrf = _client(data_dir, raise_server_exceptions=False)
    workspace_id = _draft_workspace(client, csrf, "repair-swap")
    partial = client.post(
        f"/api/v2/workspaces/{workspace_id}/sources",
        headers=_headers(csrf, "swap-partial"),
        data={"role": "PURCHASE_REQUEST"},
        files=[("files", ("broken.exe", b"MZ-first", "application/octet-stream"))],
    )
    obligation_id = partial.json()["items"][0]["repair_obligation_id"]
    first = client.post(
        f"/api/v2/workspaces/{workspace_id}/sources",
        headers=_headers(csrf, "swap-first"),
        data={
            "role": "PURCHASE_REQUEST",
            "repair_obligation_id": obligation_id,
            "repair_generation": "1",
        },
        files=[("files", ("first.csv", "제목\n첫 책\n", "text/csv"))],
    )
    first_source = first.json()["items"][0]["source_id"]
    failed = client.post(
        f"/api/v2/workspaces/{workspace_id}/sources",
        headers=_headers(csrf, "swap-second"),
        data={
            "role": "PURCHASE_REQUEST",
            "repair_obligation_id": obligation_id,
            "repair_generation": "2",
        },
        files=[
            ("files", ("still-broken.exe", b"MZ-second", "application/octet-stream"))
        ],
    )
    assert failed.status_code == 207
    linked = client.get(f"/api/v2/workspaces/{workspace_id}/sources").json()["items"]
    assert first_source in {item["id"] for item in linked}
    with connect(settings.database_path) as connection:
        obligation = connection.execute(
            """
            SELECT status, generation, resolved_source_document_id,
                   pending_source_document_id
            FROM upload_repair_obligations WHERE id = ?
            """,
            (obligation_id,),
        ).fetchone()
    assert dict(obligation) == {
        "status": "REPAIRING",
        "generation": 2,
        "resolved_source_document_id": None,
        "pending_source_document_id": first_source,
    }

    swapped = client.post(
        f"/api/v2/workspaces/{workspace_id}/sources",
        headers=_headers(csrf, "swap-third"),
        data={
            "role": "PURCHASE_REQUEST",
            "repair_obligation_id": obligation_id,
            "repair_generation": "3",
        },
        files=[("files", ("second.csv", "제목\n둘째 책\n", "text/csv"))],
    )
    second_source = swapped.json()["items"][0]["source_id"]
    linked_after = client.get(f"/api/v2/workspaces/{workspace_id}/sources").json()[
        "items"
    ]
    assert {item["id"] for item in linked_after} == {second_source}


def test_ambiguous_replacement_retry_keeps_same_obligation_generation_and_claim(
    data_dir,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from suseoro.api.routes import sources as source_routes

    client, settings, csrf = _client(data_dir, raise_server_exceptions=False)
    workspace_id = _draft_workspace(client, csrf, "repair-ambiguous-retry")
    partial = client.post(
        f"/api/v2/workspaces/{workspace_id}/sources",
        headers=_headers(csrf, "ambiguous-partial"),
        data={"role": "PURCHASE_REQUEST"},
        files=[("files", ("broken.exe", b"MZ-original", "application/octet-stream"))],
    )
    obligation_id = partial.json()["items"][0]["repair_obligation_id"]
    original_begin = source_routes.begin_upload_mutation
    interrupted = False

    def fail_once(connection, lease, *, wait_deadline_seconds):
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise sqlite3.OperationalError("simulated transient failure")
        return original_begin(
            connection,
            lease,
            wait_deadline_seconds=wait_deadline_seconds,
        )

    monkeypatch.setattr(source_routes, "begin_upload_mutation", fail_once)
    request = {
        "headers": _headers(csrf, "ambiguous-repair"),
        "data": {
            "role": "PURCHASE_REQUEST",
            "repair_obligation_id": obligation_id,
            "repair_generation": "1",
        },
        "files": [("files", ("fixed.csv", "제목\n복구 책\n", "text/csv"))],
    }
    first = client.post(
        f"/api/v2/workspaces/{workspace_id}/sources",
        **request,
    )
    retry = client.post(
        f"/api/v2/workspaces/{workspace_id}/sources",
        **request,
    )

    assert first.status_code == 503
    assert retry.status_code == 202
    with connect(settings.database_path) as connection:
        obligation = connection.execute(
            """
            SELECT generation, status, active_upload_claim_id,
                   resolved_source_document_id, pending_source_document_id
            FROM upload_repair_obligations WHERE id = ?
            """,
            (obligation_id,),
        ).fetchone()
        claim = connection.execute(
            """
            SELECT id, generation, state FROM upload_idempotency_claims
            WHERE key = 'ambiguous-repair'
            """
        ).fetchone()
    assert obligation["generation"] == 1
    assert obligation["status"] == "REPAIRING"
    assert obligation["resolved_source_document_id"] is None
    assert (
        obligation["pending_source_document_id"]
        == retry.json()["items"][0]["source_id"]
    )
    assert obligation["active_upload_claim_id"] == claim["id"]
    assert dict(claim) == {
        "id": claim["id"],
        "generation": 2,
        "state": "COMPLETED",
    }
