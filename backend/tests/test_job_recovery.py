from __future__ import annotations

import importlib
import importlib.util
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from suseoro.db.connection import connect
from suseoro.db.migrations import apply_migrations

SCHOOL_ID = "40000000-0000-4000-8000-000000000001"
WORKSPACE_ID = "40000000-0000-4000-8000-000000000002"
NOW_TEXT = "2026-08-28T00:00:00Z"


def _api() -> SimpleNamespace:
    modules = ("suseoro.jobs.repository", "suseoro.jobs.runner")
    missing = [name for name in modules if importlib.util.find_spec(name) is None]
    assert not missing, f"Task 5 modules are not implemented: {', '.join(missing)}"
    repository = importlib.import_module(modules[0])
    runner = importlib.import_module(modules[1])
    return SimpleNamespace(
        DurableJobRunner=runner.DurableJobRunner,
        JobRepository=repository.JobRepository,
    )


def _database(tmp_path):
    connection = connect(tmp_path / "jobs.sqlite3")
    apply_migrations(connection)
    connection.execute(
        "INSERT INTO schools (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (SCHOOL_ID, "작업 테스트 학교", NOW_TEXT, NOW_TEXT),
    )
    connection.execute(
        """
        INSERT INTO acquisition_workspaces (
            id, school_id, name, status, created_at, updated_at
        ) VALUES (?, ?, '작업', 'DRAFT', ?, ?)
        """,
        (WORKSPACE_ID, SCHOOL_ID, NOW_TEXT, NOW_TEXT),
    )
    return connection


def test_job_persists_stage_progress_heartbeat_and_success(tmp_path) -> None:
    """Keeping progress only in memory would make restart status unknowable."""
    api = _api()
    now = datetime(2026, 8, 28, 1, 0, tzinfo=UTC)
    with _database(tmp_path) as connection:
        repository = api.JobRepository(connection)
        job = repository.create(
            school_id=SCHOOL_ID,
            workspace_id=WORKSPACE_ID,
            job_type="COMPARE",
            payload={"source_document_ids": ["doc-1"]},
            progress_total=4,
            now=now,
        )

        def handle(context, payload):
            assert payload == {"source_document_ids": ["doc-1"]}
            context.checkpoint(stage="NORMALIZING", current=2, total=4)
            context.checkpoint(stage="MATCHING", current=4, total=4)

        runner = api.DurableJobRunner(
            repository, handlers={"COMPARE": handle}, clock=lambda: now
        )
        completed = runner.run_once()
        stored = repository.get(job.id)

    assert completed.id == job.id
    assert stored.status == "SUCCEEDED"
    assert stored.stage == "COMPLETED"
    assert (stored.progress_current, stored.progress_total) == (4, 4)
    assert stored.heartbeat_at == now
    assert stored.error is None


def test_cancellation_is_durable_and_checked_at_safe_stage_boundaries(tmp_path) -> None:
    """Ignoring a persisted cancel request could activate work after the user cancels."""
    api = _api()
    now = datetime(2026, 8, 28, 2, 0, tzinfo=UTC)
    called = False
    with _database(tmp_path) as connection:
        repository = api.JobRepository(connection)
        job = repository.create(
            school_id=SCHOOL_ID,
            workspace_id=WORKSPACE_ID,
            job_type="COMPARE",
            payload={},
            now=now,
        )
        repository.request_cancel(job.id, now=now)

        def handle(context, payload):
            nonlocal called
            called = True

        api.DurableJobRunner(
            repository, handlers={"COMPARE": handle}, clock=lambda: now
        ).run_once()
        stored = repository.get(job.id)

    assert called is False
    assert stored.status == "CANCELLED"
    assert stored.stage == "CANCELLED"
    assert stored.cancel_requested_at == now


def test_failed_job_records_structured_error_and_can_be_retried(tmp_path) -> None:
    """Losing the error or making failure terminal would block operational recovery."""
    api = _api()
    now = datetime(2026, 8, 28, 3, 0, tzinfo=UTC)
    attempts = 0
    with _database(tmp_path) as connection:
        repository = api.JobRepository(connection)
        job = repository.create(
            school_id=SCHOOL_ID,
            workspace_id=WORKSPACE_ID,
            job_type="COMPARE",
            payload={"resume": True},
            now=now,
        )

        def handle(context, payload):
            nonlocal attempts
            attempts += 1
            context.checkpoint(stage="PARSING", current=1, total=2)
            if attempts == 1:
                raise RuntimeError("temporary parser failure")
            context.checkpoint(stage="MATCHING", current=2, total=2)

        runner = api.DurableJobRunner(
            repository, handlers={"COMPARE": handle}, clock=lambda: now
        )
        runner.run_once()
        failed = repository.get(job.id)
        repository.retry(job.id, now=now + timedelta(minutes=1))
        runner.run_once()
        recovered = repository.get(job.id)

    assert failed.status == "FAILED"
    assert failed.stage == "FAILED"
    assert failed.error == {
        "type": "RuntimeError",
        "message": "temporary parser failure",
    }
    assert recovered.status == "SUCCEEDED"
    assert recovered.retry_count == 1
    assert recovered.error is None
    assert attempts == 2


def test_stale_running_job_is_requeued_once_with_checkpoint_intact(tmp_path) -> None:
    """Leaving stale RUNNING rows stranded or resetting progress would break crash recovery."""
    api = _api()
    started = datetime(2026, 8, 28, 4, 0, tzinfo=UTC)
    recovered_at = started + timedelta(minutes=10)
    with _database(tmp_path) as connection:
        repository = api.JobRepository(connection)
        job = repository.create(
            school_id=SCHOOL_ID,
            workspace_id=WORKSPACE_ID,
            job_type="COMPARE",
            payload={"source": "immutable-sha"},
            progress_total=10,
            now=started,
        )
        claimed = repository.claim_next(now=started)
        repository.update_progress(
            claimed.id,
            stage="MATCHING",
            current=5,
            total=10,
            now=started,
        )

        recovered_ids = repository.recover_stale(
            stale_before=started + timedelta(minutes=5), now=recovered_at
        )
        second_recovery = repository.recover_stale(
            stale_before=recovered_at + timedelta(minutes=5), now=recovered_at
        )
        stored = repository.get(job.id)

    assert recovered_ids == [job.id]
    assert second_recovery == []
    assert stored.status == "QUEUED"
    assert stored.stage == "RECOVERING"
    assert (stored.progress_current, stored.progress_total) == (5, 10)
    assert stored.retry_count == 1
    assert stored.payload == {"source": "immutable-sha"}
    assert stored.error == {
        "type": "StaleJobRecovered",
        "message": "작업 프로세스 중단 뒤 재개 대기 중입니다.",
    }


def test_claim_is_atomic_so_two_workers_cannot_run_the_same_job(tmp_path) -> None:
    """An unconditional claim would allow duplicate comparison side effects."""
    api = _api()
    now = datetime(2026, 8, 28, 5, 0, tzinfo=UTC)
    with _database(tmp_path) as connection:
        repository = api.JobRepository(connection)
        job = repository.create(
            school_id=SCHOOL_ID,
            workspace_id=WORKSPACE_ID,
            job_type="COMPARE",
            payload={},
            now=now,
        )

        first = repository.claim_next(now=now)
        second = repository.claim_next(now=now)

    assert first.id == job.id
    assert first.status == "RUNNING"
    assert second is None
