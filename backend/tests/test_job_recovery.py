from __future__ import annotations

import importlib
import importlib.util
import json
import sqlite3
import threading
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from suseoro.db.connection import connect
from suseoro.db.migrations import apply_migrations

SCHOOL_ID = "40000000-0000-4000-8000-000000000001"
WORKSPACE_ID = "40000000-0000-4000-8000-000000000002"
OTHER_SCHOOL_ID = "40000000-0000-4000-8000-000000000003"
OTHER_WORKSPACE_ID = "40000000-0000-4000-8000-000000000004"
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
    connection.execute(
        "INSERT INTO schools (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (OTHER_SCHOOL_ID, "다른 학교", NOW_TEXT, NOW_TEXT),
    )
    connection.execute(
        """
        INSERT INTO acquisition_workspaces (
            id, school_id, name, status, created_at, updated_at
        ) VALUES (?, ?, '타교 작업', 'DRAFT', ?, ?)
        """,
        (OTHER_WORKSPACE_ID, OTHER_SCHOOL_ID, NOW_TEXT, NOW_TEXT),
    )
    return connection


def _comparison_api() -> SimpleNamespace:
    modules = (
        "suseoro.jobs.handlers",
        "suseoro.services.comparison",
    )
    missing = [name for name in modules if importlib.util.find_spec(name) is None]
    assert not missing, f"Task 5 job modules are not implemented: {', '.join(missing)}"
    handlers = importlib.import_module(modules[0])
    comparison = importlib.import_module(modules[1])
    return SimpleNamespace(
        build_comparison_handler=handlers.build_comparison_handler,
        build_job_runner=handlers.build_job_runner,
        ComparisonService=comparison.ComparisonService,
    )


def _source_document(connection, *, sha_digit: str, row_count: int = 2):
    file_id = str(uuid.uuid4())
    document_id = str(uuid.uuid4())
    connection.execute(
        """
        INSERT INTO source_files (
            id, sha256, size_bytes, storage_path, detected_format, created_at
        ) VALUES (?, ?, 10, ?, 'XLSX', ?)
        """,
        (file_id, sha_digit * 64, f"jobs/{sha_digit}.xlsx", NOW_TEXT),
    )
    connection.execute(
        """
        INSERT INTO source_documents (
            id, source_file_id, school_id, role, parser_version, status,
            detected_format, created_at, completed_at
        ) VALUES (?, ?, ?, 'PURCHASE_REQUEST', 'tabular-v1', 'SUCCESS', 'XLSX', ?, ?)
        """,
        (document_id, file_id, SCHOOL_ID, NOW_TEXT, NOW_TEXT),
    )
    row_ids = []
    for index in range(1, row_count + 1):
        row_id = str(uuid.uuid4())
        row_ids.append(row_id)
        fields = {
            "title": {"value": f"작업 후보 {index}", "raw_value": f"작업 후보 {index}"},
            "author": {"value": f"저자 {index}", "raw_value": f"저자 {index}"},
        }
        connection.execute(
            """
            INSERT INTO source_rows (
                id, source_document_id, source_row, status, raw_json,
                fields_json, warnings_json, created_at
            ) VALUES (?, ?, ?, 'SUCCESS', ?, ?, '[]', ?)
            """,
            (
                row_id,
                document_id,
                index,
                json.dumps({"title": f"작업 후보 {index}"}, ensure_ascii=False),
                json.dumps(fields, ensure_ascii=False),
                NOW_TEXT,
            ),
        )
    return document_id, tuple(row_ids)


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
            claim_token=claimed.claim_token,
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


def test_job_creation_rejects_a_workspace_from_another_school(tmp_path) -> None:
    """A cross-school workspace on a job would bypass later tenant checks."""
    api = _api()
    with (
        _database(tmp_path) as connection,
        pytest.raises(ValueError, match="workspace"),
    ):
        api.JobRepository(connection).create(
            school_id=SCHOOL_ID,
            workspace_id=OTHER_WORKSPACE_ID,
            job_type="COMPARE",
            payload={},
        )


def test_stale_worker_claim_is_fenced_after_recovery_and_reclaim(tmp_path) -> None:
    """A recovered job's old worker must not overwrite the new worker's progress."""
    api = _api()
    started = datetime(2026, 8, 28, 6, 0, tzinfo=UTC)
    recovered_at = started + timedelta(minutes=10)
    with _database(tmp_path) as connection:
        old_worker = api.JobRepository(connection)
        job = old_worker.create(
            school_id=SCHOOL_ID,
            workspace_id=WORKSPACE_ID,
            job_type="COMPARE",
            payload={},
            now=started,
        )
        connection.commit()
        old_claim = old_worker.claim_next(now=started)
        old_worker.update_progress(
            job.id,
            claim_token=old_claim.claim_token,
            stage="COMPARING",
            current=1,
            total=2,
            now=started,
        )
        connection.commit()
        reclaim_connection = connect(tmp_path / "jobs.sqlite3")
        new_worker = api.JobRepository(reclaim_connection)
        new_worker.recover_stale(
            stale_before=started + timedelta(minutes=5), now=recovered_at
        )
        new_claim = new_worker.claim_next(now=recovered_at)
        reclaim_connection.commit()

        with pytest.raises(RuntimeError, match="claim"):
            old_worker.update_progress(
                job.id,
                claim_token=old_claim.claim_token,
                stage="STALE_WRITE",
                current=2,
                total=2,
                now=recovered_at,
            )
        with pytest.raises(RuntimeError, match="claim"):
            old_worker.mark_succeeded(
                job.id, claim_token=old_claim.claim_token, now=recovered_at
            )
        stored = new_worker.get(job.id)
        reclaim_connection.close()

    assert old_claim.claim_token
    assert new_claim.claim_token
    assert new_claim.claim_token != old_claim.claim_token
    assert new_claim.claim_generation == old_claim.claim_generation + 1
    assert stored.status == "RUNNING"
    assert stored.stage == "STARTING"


def test_real_compare_handler_batches_rows_and_resumes_persisted_results(
    tmp_path,
) -> None:
    """The production handler must use ComparisonService and resume without duplicates."""
    jobs = _api()
    comparison = _comparison_api()
    now = datetime(2026, 8, 28, 7, 0, tzinfo=UTC)
    with _database(tmp_path) as connection:
        document_id, row_ids = _source_document(connection, sha_digit="a", row_count=3)
        # Simulate the durable result left by a prior worker's completed batch.
        comparison.ComparisonService(connection).compare_documents(
            school_id=SCHOOL_ID,
            workspace_id=WORKSPACE_ID,
            source_document_ids=(document_id,),
            source_row_ids=(row_ids[0],),
        )
        repository = jobs.JobRepository(connection)
        job = repository.create(
            school_id=SCHOOL_ID,
            workspace_id=WORKSPACE_ID,
            job_type="COMPARE",
            payload={"source_document_ids": [document_id]},
            now=now,
        )
        connection.execute(
            "CREATE TABLE observed_job_stages (stage TEXT NOT NULL, current INTEGER NOT NULL)"
        )
        connection.execute(
            """
            CREATE TRIGGER observe_job_stages AFTER UPDATE OF stage, progress_current
            ON durable_jobs FOR EACH ROW WHEN NEW.id = OLD.id
            BEGIN
                INSERT INTO observed_job_stages(stage, current)
                VALUES (NEW.stage, NEW.progress_current);
            END
            """
        )
        runner = comparison.build_job_runner(
            connection,
            file_batch_size=1,
            row_batch_size=1,
            clock=lambda: now,
        )

        completed = runner.run_once()
        stored = repository.get(job.id)
        results = connection.execute(
            "SELECT source_row_id FROM comparison_row_results WHERE workspace_id = ?",
            (WORKSPACE_ID,),
        ).fetchall()
        stages = connection.execute(
            "SELECT stage, current FROM observed_job_stages ORDER BY rowid"
        ).fetchall()

    assert completed.status == "SUCCEEDED"
    assert stored.progress_current == stored.progress_total == 3
    assert {row["source_row_id"] for row in results} == set(row_ids)
    assert [row["stage"] for row in stages] == [
        "STARTING",
        "VALIDATING",
        "COMPARING",
        "COMPARING",
        "FINALIZING",
        "COMPLETED",
    ]


def test_compare_handler_observes_cancellation_between_row_batches(tmp_path) -> None:
    """A completed batch stays durable while cancellation stops the next batch."""
    jobs = _api()
    comparison = _comparison_api()
    now = datetime(2026, 8, 28, 8, 0, tzinfo=UTC)
    with _database(tmp_path) as connection:
        document_id, _ = _source_document(connection, sha_digit="b", row_count=2)
        repository = jobs.JobRepository(connection)
        job = repository.create(
            school_id=SCHOOL_ID,
            workspace_id=WORKSPACE_ID,
            job_type="COMPARE",
            payload={"source_document_ids": [document_id]},
            now=now,
        )
        connection.execute(
            f"""
            CREATE TRIGGER cancel_after_first_real_result
            AFTER INSERT ON comparison_row_results
            BEGIN
                UPDATE durable_jobs SET cancel_requested_at = '{NOW_TEXT}'
                WHERE id = '{job.id}';
            END
            """
        )
        runner = jobs.DurableJobRunner(
            repository,
            handlers={
                "COMPARE": comparison.build_comparison_handler(
                    connection, file_batch_size=1, row_batch_size=1
                )
            },
            clock=lambda: now,
        )

        cancelled = runner.run_once()
        stored = repository.get(job.id)
        result_count = connection.execute(
            "SELECT COUNT(*) FROM comparison_row_results WHERE workspace_id = ?",
            (WORKSPACE_ID,),
        ).fetchone()[0]

    assert cancelled.status == "CANCELLED"
    assert stored.stage == "CANCELLED"
    assert result_count == 1


def test_compare_handler_recovers_from_last_committed_real_row_batch(tmp_path) -> None:
    """A worker crash must retain prior real results and resume without rescoring them."""
    jobs = _api()
    comparison = _comparison_api()
    now = datetime(2026, 8, 28, 8, 30, tzinfo=UTC)
    with _database(tmp_path) as connection:
        document_id, row_ids = _source_document(connection, sha_digit="d", row_count=3)
        repository = jobs.JobRepository(connection)
        job = repository.create(
            school_id=SCHOOL_ID,
            workspace_id=WORKSPACE_ID,
            job_type="COMPARE",
            payload={"source_document_ids": [document_id]},
            now=now,
        )
        connection.execute(
            f"""
            CREATE TRIGGER crash_second_real_batch
            BEFORE INSERT ON comparison_row_results
            FOR EACH ROW WHEN NEW.source_row_id = '{row_ids[1]}'
            BEGIN SELECT RAISE(ABORT, 'simulated worker crash'); END
            """
        )
        runner = jobs.DurableJobRunner(
            repository,
            handlers={
                "COMPARE": comparison.build_comparison_handler(
                    connection, file_batch_size=1, row_batch_size=1
                )
            },
            clock=lambda: now,
        )

        failed = runner.run_once()
        after_crash = connection.execute(
            """
            SELECT source_row_id FROM comparison_row_results
            WHERE workspace_id = ? ORDER BY source_row_id
            """,
            (WORKSPACE_ID,),
        ).fetchall()
        connection.execute("DROP TRIGGER crash_second_real_batch")
        repository.retry(job.id, now=now)
        completed = runner.run_once()
        after_retry = connection.execute(
            """
            SELECT source_row_id FROM comparison_row_results
            WHERE workspace_id = ? ORDER BY source_row_id
            """,
            (WORKSPACE_ID,),
        ).fetchall()

    assert failed.status == "FAILED"
    assert [row["source_row_id"] for row in after_crash] == [row_ids[0]]
    assert completed.status == "SUCCEEDED"
    assert {row["source_row_id"] for row in after_retry} == set(row_ids)


def test_stale_claim_cannot_write_comparison_or_file_results(tmp_path) -> None:
    """Job fencing must cover result side effects, not only the progress row."""
    jobs = _api()
    comparison = _comparison_api()
    started = datetime(2026, 8, 28, 9, 0, tzinfo=UTC)
    recovered_at = started + timedelta(minutes=10)
    with _database(tmp_path) as connection:
        document_id, _ = _source_document(connection, sha_digit="c", row_count=1)
        repository = jobs.JobRepository(connection)
        job = repository.create(
            school_id=SCHOOL_ID,
            workspace_id=WORKSPACE_ID,
            job_type="COMPARE",
            payload={"source_document_ids": [document_id]},
            now=started,
        )
        old_claim = repository.claim_next(now=started)
        connection.commit()
        repository.recover_stale(
            stale_before=started + timedelta(minutes=5), now=recovered_at
        )
        repository.claim_next(now=recovered_at)

        with pytest.raises(RuntimeError, match="claim"):
            comparison.ComparisonService(connection).compare_documents(
                school_id=SCHOOL_ID,
                workspace_id=WORKSPACE_ID,
                source_document_ids=(document_id,),
                job_id=job.id,
                claim_token=old_claim.claim_token,
            )
        row_count = connection.execute(
            "SELECT COUNT(*) FROM comparison_row_results WHERE workspace_id = ?",
            (WORKSPACE_ID,),
        ).fetchone()[0]
        file_count = connection.execute(
            "SELECT COUNT(*) FROM job_file_results WHERE job_id = ?", (job.id,)
        ).fetchone()[0]

    assert row_count == 0
    assert file_count == 0


def test_reclaim_cannot_interleave_after_old_claim_check_before_domain_commit(
    tmp_path, monkeypatch
) -> None:
    """Claim validation outside the write transaction lets a stale worker persist rows."""
    jobs = _api()
    comparison = _comparison_api()
    from suseoro.jobs.repository import JobRepository

    started = datetime(2026, 8, 28, 10, 0, tzinfo=UTC)
    database_path = tmp_path / "jobs.sqlite3"
    with _database(tmp_path) as setup:
        document_id, row_ids = _source_document(setup, sha_digit="e", row_count=2)
        repository = jobs.JobRepository(setup)
        job = repository.create(
            school_id=SCHOOL_ID,
            workspace_id=WORKSPACE_ID,
            job_type="COMPARE",
            payload={"source_document_ids": [document_id]},
            now=started,
        )
        old_claim = repository.claim_next(now=started)
        setup.commit()

    checked = threading.Event()
    reclaim_attempted = threading.Event()
    state = {"reclaimed": False, "worker_error": None}
    original_assert_claim = JobRepository.assert_claim

    def barrier_assert_claim(self, job_id, claim_token, claim_generation=None):
        current = original_assert_claim(self, job_id, claim_token, claim_generation)
        if threading.current_thread().name == "old-compare-worker":
            checked.set()
            assert reclaim_attempted.wait(5), "reclaim barrier timed out"
            if not state["reclaimed"]:
                raise RuntimeError("claim interleaving was fenced")
        return current

    monkeypatch.setattr(JobRepository, "assert_claim", barrier_assert_claim)

    def run_old_worker() -> None:
        connection = connect(database_path)
        try:
            # Exercise the savepoint path used when a caller already owns the
            # transaction.  A read-only claim check must be upgraded to a
            # write fence before the controlled interleaving below.
            connection.execute("BEGIN")
            comparison.ComparisonService(connection).compare_documents(
                school_id=SCHOOL_ID,
                workspace_id=WORKSPACE_ID,
                source_document_ids=(document_id,),
                source_row_ids=(row_ids[0],),
                job_id=job.id,
                claim_token=old_claim.claim_token,
            )
            connection.commit()
        except RuntimeError as error:  # the fenced implementation aborts here
            connection.rollback()
            state["worker_error"] = error
        finally:
            connection.close()

    worker = threading.Thread(target=run_old_worker, name="old-compare-worker")
    worker.start()
    assert checked.wait(5), "old worker never reached the claim barrier"
    reclaim = connect(database_path)
    reclaim.execute("PRAGMA busy_timeout=100")
    try:
        reclaim_repository = jobs.JobRepository(reclaim)
        reclaim_repository.recover_stale(
            stale_before=started + timedelta(minutes=1),
            now=started + timedelta(minutes=2),
        )
        new_claim = reclaim_repository.claim_next(now=started + timedelta(minutes=2))
        reclaim.commit()
        state["reclaimed"] = new_claim is not None
    except sqlite3.OperationalError as error:
        assert "locked" in str(error).casefold()
        reclaim.rollback()
    finally:
        reclaim_attempted.set()
        reclaim.close()
    worker.join(5)
    assert not worker.is_alive()

    with connect(database_path) as verify:
        domain_rows = verify.execute(
            "SELECT COUNT(*) FROM comparison_row_results WHERE workspace_id = ?",
            (WORKSPACE_ID,),
        ).fetchone()[0]

    assert state["reclaimed"] is False
    assert isinstance(state["worker_error"], RuntimeError)
    assert domain_rows == 0
