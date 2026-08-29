-- Rebuild counts from durable row evidence.  A preview row that still needs a
-- mapping has no successful source_row and must remain unread rather than being
-- inferred as successful from total_rows alone.
UPDATE job_file_results
SET processed_rows = CASE
        WHEN status = 'SUCCESS' THEN total_rows
        WHEN status = 'FAILED' THEN 0
        WHEN status = 'PARTIAL' AND json_extract(
            CASE WHEN json_valid(error_json) THEN error_json ELSE '{}' END,
            '$.code'
        ) = 'MAPPING_REQUIRED' THEN 0
        ELSE MIN(
            MAX(0, total_rows - 1),
            (
                SELECT COUNT(*)
                FROM source_rows AS source_row
                WHERE source_row.source_document_id = job_file_results.source_document_id
                  AND source_row.status = 'SUCCESS'
            )
        )
    END,
    row_error_count = CASE
        WHEN status = 'SUCCESS' THEN 0
        WHEN status = 'FAILED' THEN total_rows
        WHEN status = 'PARTIAL' AND json_extract(
            CASE WHEN json_valid(error_json) THEN error_json ELSE '{}' END,
            '$.code'
        ) = 'MAPPING_REQUIRED' THEN 0
        ELSE total_rows - MIN(
            MAX(0, total_rows - 1),
            (
                SELECT COUNT(*)
                FROM source_rows AS source_row
                WHERE source_row.source_document_id = job_file_results.source_document_id
                  AND source_row.status = 'SUCCESS'
            )
        )
    END
WHERE job_id IN (
    SELECT id FROM durable_jobs WHERE job_type IN ('INGEST', 'PARSE')
)
AND status IN ('SUCCESS', 'PARTIAL', 'FAILED');

DROP TRIGGER IF EXISTS job_file_results_scope_update;

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
