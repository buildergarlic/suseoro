-- 0011 corrects historical INGEST/PARSE file counts.  The legacy claim guard
-- rejects every update after a job becomes terminal, so only that conflicting
-- trigger is suspended.  The migration runner applies this prelude, committed
-- 0011, and the round-4 restoration as one atomic bundle.
DROP TRIGGER IF EXISTS job_file_results_scope_update;
