"""Single-claim durable job runner with safe cancellation checkpoints."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from suseoro.jobs.public_errors import job_failure
from suseoro.jobs.repository import Job, JobRepository
from suseoro.security.sessions import utc_now


class JobCancelled(RuntimeError):
    pass


logger = logging.getLogger(__name__)


class JobContext:
    def __init__(
        self,
        repository: JobRepository,
        job: Job,
        clock: Callable[[], datetime],
    ) -> None:
        self.repository = repository
        self.job = job
        self.clock = clock

    def ensure_not_cancelled(self) -> None:
        if self.job.claim_token is None:
            raise RuntimeError("running job has no claim token")
        current = self.repository.assert_claim(
            self.job.id, self.job.claim_token, self.job.claim_generation
        )
        if current.cancel_requested_at is not None:
            raise JobCancelled()

    def checkpoint(self, *, stage: str, current: int, total: int | None) -> None:
        self.ensure_not_cancelled()
        self.job = self.repository.update_progress(
            self.job.id,
            claim_token=self.job.claim_token,
            stage=stage,
            current=current,
            total=total,
            now=self.clock(),
        )
        # A checkpoint is a safe restart boundary, so publish it immediately.
        self.repository.connection.commit()


class DurableJobRunner:
    def __init__(
        self,
        repository: JobRepository,
        *,
        handlers: dict[str, Callable[[JobContext, dict[str, Any]], None]],
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.repository = repository
        self.handlers = handlers
        self.clock = clock

    def run_once(self) -> Job | None:
        now = self.clock()
        job = self.repository.claim_next(now=now)
        if job is None:
            return None
        # Release the atomic claim before doing potentially long-running work.
        self.repository.connection.commit()
        if job.cancel_requested_at is not None:
            cancelled = self.repository.mark_cancelled(
                job.id, claim_token=job.claim_token, now=self.clock()
            )
            self.repository.connection.commit()
            return cancelled
        handler = self.handlers.get(job.job_type)
        if handler is None:
            failed = self.repository.mark_failed(
                job.id,
                claim_token=job.claim_token,
                error={
                    "type": "UnknownJobType",
                    "message": f"No handler registered for {job.job_type}",
                },
                now=self.clock(),
            )
            self.repository.connection.commit()
            return failed
        context = JobContext(self.repository, job, self.clock)
        try:
            handler(context, job.payload)
            context.ensure_not_cancelled()
        except JobCancelled:
            self.repository.connection.rollback()
            cancelled = self.repository.mark_cancelled(
                job.id, claim_token=job.claim_token, now=self.clock()
            )
            self.repository.connection.commit()
            return cancelled
        # A job boundary must persist every ordinary handler failure for retry.
        except Exception:
            logger.exception("Durable job %s failed", job.id)
            self.repository.connection.rollback()
            failed = self.repository.mark_failed(
                job.id,
                claim_token=job.claim_token,
                error=job_failure(),
                now=self.clock(),
            )
            self.repository.connection.commit()
            return failed
        terminal = self.repository.item_terminal_status(job.id)
        if terminal == "FAILED":
            failed = self.repository.mark_failed(
                job.id,
                claim_token=job.claim_token,
                error={
                    "type": "AllItemsFailed",
                    "message": "모든 파일 처리에 실패했습니다.",
                },
                now=self.clock(),
            )
            self.repository.connection.commit()
            return failed
        if terminal == "PARTIAL":
            partial = self.repository.mark_partial(
                job.id, claim_token=job.claim_token, now=self.clock()
            )
            self.repository.connection.commit()
            return partial
        succeeded = self.repository.mark_succeeded(
            job.id, claim_token=job.claim_token, now=self.clock()
        )
        self.repository.connection.commit()
        return succeeded
