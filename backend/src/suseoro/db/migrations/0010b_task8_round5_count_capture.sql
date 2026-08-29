-- Capture the per-job values before either 0011 or 0012 can reconstruct them
-- from mutable per-source rows.  A database that already recorded either
-- rewrite has lost the necessary temporal evidence and is explicitly marked
-- UNVERIFIED instead of treating the observed values as historical truth.
CREATE TABLE job_file_result_count_history (
    job_file_result_id TEXT PRIMARY KEY
        REFERENCES job_file_results(id),
    job_id TEXT NOT NULL,
    source_document_id TEXT NOT NULL,
    total_rows INTEGER NOT NULL CHECK (total_rows >= 0),
    processed_rows INTEGER NOT NULL CHECK (
        processed_rows >= 0 AND processed_rows <= total_rows
    ),
    row_error_count INTEGER NOT NULL CHECK (
        row_error_count >= 0
        AND processed_rows + row_error_count <= total_rows
    ),
    confidence TEXT NOT NULL CHECK (confidence IN ('EXACT', 'UNVERIFIED')),
    captured_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO job_file_result_count_history (
    job_file_result_id, job_id, source_document_id, total_rows,
    processed_rows, row_error_count, confidence
)
SELECT result.id,
       result.job_id,
       result.source_document_id,
       result.total_rows,
       CASE
           WHEN result.status = 'PARTIAL'
            AND json_extract(
                CASE WHEN json_valid(result.error_json)
                     THEN result.error_json ELSE '{}' END,
                '$.code'
            ) = 'MAPPING_REQUIRED' THEN 0
           ELSE result.processed_rows
       END,
       CASE
           WHEN result.status = 'SUCCESS' THEN 0
           WHEN result.status = 'PARTIAL'
            AND json_extract(
                CASE WHEN json_valid(result.error_json)
                     THEN result.error_json ELSE '{}' END,
                '$.code'
            ) = 'MAPPING_REQUIRED' THEN 0
           ELSE MAX(0, result.total_rows - result.processed_rows)
       END,
       CASE WHEN EXISTS (
           SELECT 1 FROM schema_migrations
           WHERE migration_id IN (
               '0011_task8_round3_integrity',
               '0012_task8_round4_integrity'
           )
       ) THEN 'UNVERIFIED' ELSE 'EXACT' END
FROM job_file_results AS result
JOIN durable_jobs AS job ON job.id = result.job_id
WHERE job.job_type IN ('INGEST', 'PARSE')
  AND result.status IN ('SUCCESS', 'PARTIAL', 'FAILED');

CREATE TRIGGER immutable_job_file_result_count_history_insert
BEFORE INSERT ON job_file_result_count_history
BEGIN SELECT RAISE(ABORT, 'historical job file counts are immutable'); END;

CREATE TRIGGER immutable_job_file_result_count_history_update
BEFORE UPDATE ON job_file_result_count_history
BEGIN SELECT RAISE(ABORT, 'historical job file counts are immutable'); END;

CREATE TRIGGER immutable_job_file_result_count_history_delete
BEFORE DELETE ON job_file_result_count_history
BEGIN SELECT RAISE(ABORT, 'historical job file counts are immutable'); END;
