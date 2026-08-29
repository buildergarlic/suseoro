-- 0012 is checksum-protected and may still rewrite legacy counts. Restore every
-- pre-0012 capture so that 0012 cannot further mutate history. Captures taken
-- after 0011 remain explicitly UNVERIFIED at the API boundary because 0011 may
-- already have reconstructed them; the confidence marker, not another rewrite,
-- is the fail-safe.
DROP TRIGGER IF EXISTS job_file_results_scope_update;

UPDATE job_file_results
SET total_rows = (
        SELECT history.total_rows
        FROM job_file_result_count_history AS history
        WHERE history.job_file_result_id = job_file_results.id
    ),
    processed_rows = (
        SELECT history.processed_rows
        FROM job_file_result_count_history AS history
        WHERE history.job_file_result_id = job_file_results.id
    ),
    row_error_count = (
        SELECT history.row_error_count
        FROM job_file_result_count_history AS history
        WHERE history.job_file_result_id = job_file_results.id
    )
WHERE EXISTS (
    SELECT 1
    FROM job_file_result_count_history AS history
    WHERE history.job_file_result_id = job_file_results.id
      AND (
          history.total_rows <> job_file_results.total_rows
          OR history.processed_rows <> job_file_results.processed_rows
          OR history.row_error_count <> job_file_results.row_error_count
      )
);

CREATE TRIGGER job_file_results_scope_update
BEFORE UPDATE ON job_file_results
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1 FROM durable_jobs j JOIN source_documents sd
      ON sd.id = NEW.source_document_id
    WHERE j.id = NEW.job_id AND j.school_id = sd.school_id
      AND j.job_type IN ('COMPARE', 'INGEST', 'PARSE') AND j.status = 'RUNNING'
      AND j.claim_token = NEW.claim_token
      AND j.claim_generation = NEW.claim_generation
)
BEGIN SELECT RAISE(ABORT, 'job file result scope or claim mismatch'); END;
