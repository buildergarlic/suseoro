from __future__ import annotations

import json
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from suseoro.api.app import create_app
from suseoro.api.routes.events import (
    EventFeed,
    publish_event,
    replay_events,
)
from suseoro.config import Settings
from suseoro.db.connection import connect
from suseoro.db.migrations import apply_migrations
from suseoro.jobs.repository import JobRepository
from suseoro.repositories.auth import UserRecord, issue_session

NOW = "2026-08-28T00:00:00.000000Z"


def _database(data_dir: Path) -> tuple[Settings, str, str, str]:
    settings = Settings(data_dir=data_dir)
    school_id = str(uuid.uuid4())
    actor_id = str(uuid.uuid4())
    workspace_id = str(uuid.uuid4())
    with connect(settings.database_path) as connection:
        apply_migrations(connection)
        connection.execute(
            "INSERT INTO schools (id, name, created_at, updated_at) VALUES (?, '학교', ?, ?)",
            (school_id, NOW, NOW),
        )
        connection.execute(
            """
            INSERT INTO users (
                id, school_id, username, password_hash, display_name,
                created_at, updated_at
            ) VALUES (?, ?, 'operator', 'hash', '담당자', ?, ?)
            """,
            (actor_id, school_id, NOW, NOW),
        )
        connection.execute(
            "INSERT INTO user_roles (school_id, user_id, role, created_at) VALUES (?, ?, 'OPERATOR', ?)",
            (school_id, actor_id, NOW),
        )
        connection.execute(
            """
            INSERT INTO acquisition_workspaces (
                id, school_id, name, status, created_by_user_id, created_at, updated_at
            ) VALUES (?, ?, '작업', 'DRAFT', ?, ?, ?)
            """,
            (workspace_id, school_id, actor_id, NOW, NOW),
        )
        connection.commit()
    return settings, school_id, actor_id, workspace_id


def test_sse_replays_only_events_after_last_event_id_in_stable_order(
    data_dir: Path,
) -> None:
    settings, school_id, actor_id, workspace_id = _database(data_dir)
    with connect(settings.database_path) as connection:
        ids = [
            publish_event(
                connection,
                school_id=school_id,
                workspace_id=workspace_id,
                event_type=event_type,
                data={"sequence": index, "actor_id": actor_id},
            )
            for index, event_type in enumerate(
                (
                    "job.progress",
                    "approval.requested",
                    "candidate.locked",
                    "scan.result",
                ),
                start=1,
            )
        ]
        connection.commit()

    replayed = replay_events(
        settings.database_path,
        school_id=school_id,
        workspace_id=workspace_id,
        last_event_id=ids[1],
        limit=10,
    )

    assert [event.id for event in replayed] == ids[2:]
    assert [event.event_type for event in replayed] == [
        "candidate.locked",
        "scan.result",
    ]
    assert [event.data["sequence"] for event in replayed] == [3, 4]


def test_sse_frames_include_id_type_and_json_payload(data_dir: Path) -> None:
    settings, school_id, _, workspace_id = _database(data_dir)
    with connect(settings.database_path) as connection:
        event_id = publish_event(
            connection,
            school_id=school_id,
            workspace_id=workspace_id,
            event_type="job.progress",
            data={"stage": "PARSING", "current": 3, "total": 10},
        )
        connection.commit()

    feed = EventFeed(
        settings.database_path,
        school_id=school_id,
        workspace_id=workspace_id,
        last_event_id=None,
        heartbeat_seconds=15,
    )
    frame = next(feed)

    assert frame.startswith(f"id: {event_id}\nevent: job.progress\n")
    payload_line = next(
        line for line in frame.splitlines() if line.startswith("data: ")
    )
    assert json.loads(payload_line.removeprefix("data: ")) == {
        "stage": "PARSING",
        "current": 3,
        "total": 10,
    }


def test_sse_heartbeat_waits_15_seconds_without_cancelling_durable_job(
    data_dir: Path,
) -> None:
    settings, school_id, _, workspace_id = _database(data_dir)
    waits: list[float] = []
    with connect(settings.database_path) as connection:
        repository = JobRepository(connection)
        queued = repository.create(
            school_id=school_id,
            workspace_id=workspace_id,
            job_type="COMPARE",
            payload={"source_document_ids": ["not-run-in-this-test"]},
        )
        running = repository.claim_next()
        assert running is not None and running.id == queued.id
        connection.commit()

    feed = EventFeed(
        settings.database_path,
        school_id=school_id,
        workspace_id=workspace_id,
        last_event_id=None,
        heartbeat_seconds=15,
        wait=lambda seconds: waits.append(seconds),
    )
    heartbeat = next(feed)
    feed.close()

    assert heartbeat == ": heartbeat\n\n"
    assert waits == [15]
    with connect(settings.database_path) as connection:
        persisted = JobRepository(connection).get(queued.id)
    assert persisted is not None
    assert persisted.status == "RUNNING"
    assert persisted.cancel_requested_at is None


def test_event_replay_is_strictly_school_and_workspace_scoped(data_dir: Path) -> None:
    settings, school_id, _, workspace_id = _database(data_dir)
    other_school_id = str(uuid.uuid4())
    other_workspace_id = str(uuid.uuid4())
    with connect(settings.database_path) as connection:
        connection.execute(
            "INSERT INTO schools (id, name, created_at, updated_at) VALUES (?, '타교', ?, ?)",
            (other_school_id, NOW, NOW),
        )
        connection.execute(
            """
            INSERT INTO acquisition_workspaces (id, school_id, name, status, created_at, updated_at)
            VALUES (?, ?, '타교 작업', 'DRAFT', ?, ?)
            """,
            (other_workspace_id, other_school_id, NOW, NOW),
        )
        own = publish_event(
            connection,
            school_id=school_id,
            workspace_id=workspace_id,
            event_type="job.progress",
            data={"visible": True},
        )
        publish_event(
            connection,
            school_id=other_school_id,
            workspace_id=other_workspace_id,
            event_type="job.progress",
            data={"visible": False},
        )
        connection.commit()

    events = replay_events(
        settings.database_path,
        school_id=school_id,
        workspace_id=workspace_id,
        last_event_id=None,
    )
    assert [event.id for event in events] == [own]


def test_durable_job_checkpoints_publish_replayable_progress_events(
    data_dir: Path,
) -> None:
    settings, school_id, _, workspace_id = _database(data_dir)
    with connect(settings.database_path) as connection:
        repository = JobRepository(connection)
        queued = repository.create(
            school_id=school_id,
            workspace_id=workspace_id,
            job_type="INGEST",
            payload={"source_document_ids": []},
            progress_total=3,
        )
        running = repository.claim_next()
        assert running is not None and running.claim_token is not None
        repository.update_progress(
            running.id,
            claim_token=running.claim_token,
            stage="PARSING",
            current=1,
            total=3,
        )
        connection.commit()

    events = replay_events(
        settings.database_path,
        school_id=school_id,
        workspace_id=workspace_id,
        last_event_id=None,
    )
    progress = [event for event in events if event.event_type == "job.progress"]
    assert progress[-1].data == {
        "job_id": queued.id,
        "status": "RUNNING",
        "stage": "PARSING",
        "current": 1,
        "total": 3,
    }


def test_event_publication_deduplicates_an_idempotent_mutation_side_effect(
    data_dir: Path,
) -> None:
    settings, school_id, _, workspace_id = _database(data_dir)
    with connect(settings.database_path) as connection:
        first = publish_event(
            connection,
            school_id=school_id,
            workspace_id=workspace_id,
            event_type="approval.requested",
            data={"revision_id": "one"},
            deduplication_key="actor:approval-route:key",
        )
        second = publish_event(
            connection,
            school_id=school_id,
            workspace_id=workspace_id,
            event_type="approval.requested",
            data={"revision_id": "one"},
            deduplication_key="actor:approval-route:key",
        )
        connection.commit()

    assert second == first
    events = replay_events(
        settings.database_path,
        school_id=school_id,
        workspace_id=workspace_id,
        last_event_id=None,
    )
    assert [event.id for event in events] == [first]


def test_event_route_constrains_last_event_id_to_non_negative_integer(
    data_dir: Path,
) -> None:
    settings, school_id, actor_id, _ = _database(data_dir)
    with connect(settings.database_path) as connection:
        issued = issue_session(
            connection,
            UserRecord(
                id=actor_id,
                school_id=school_id,
                username="operator",
                display_name="담당자",
                roles=("OPERATOR",),
            ),
            3_600,
        )
        connection.commit()
    client = TestClient(create_app(settings), base_url="https://testserver")
    client.cookies.set("suseoro_session", issued.session_token)

    with client:
        schema = client.get("/openapi.json").json()
    parameter = next(
        item
        for item in schema["paths"]["/api/v2/events"]["get"]["parameters"]
        if item["name"] == "Last-Event-ID"
    )
    assert parameter["schema"]["type"] == "integer"
    assert parameter["schema"]["minimum"] == 0


def test_event_feed_revalidates_and_closes_after_session_revocation(
    data_dir: Path,
) -> None:
    settings, school_id, actor_id, workspace_id = _database(data_dir)
    with connect(settings.database_path) as connection:
        issued = issue_session(
            connection,
            UserRecord(
                id=actor_id,
                school_id=school_id,
                username="operator",
                display_name="담당자",
                roles=("OPERATOR",),
            ),
            3_600,
        )
        connection.commit()
    feed = EventFeed(
        settings.database_path,
        school_id=school_id,
        workspace_id=workspace_id,
        last_event_id=None,
        session_token=issued.session_token,
        heartbeat_seconds=15,
        wait=lambda _: None,
    )
    assert next(feed) == ": heartbeat\n\n"

    with connect(settings.database_path) as connection:
        connection.execute(
            "UPDATE sessions SET revoked_at = ? WHERE id = ?", (NOW, issued.session_id)
        )
        connection.commit()

    try:
        next(feed)
    except StopIteration:
        pass
    else:
        raise AssertionError("a revoked SSE session must close on the next poll")
