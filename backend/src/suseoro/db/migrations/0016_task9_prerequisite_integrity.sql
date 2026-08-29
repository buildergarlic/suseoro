-- A pre-0011 mixed PARTIAL row cannot prove its success/error split from the
-- legacy aggregate columns alone.  Preserve those immutable captures and add
-- a companion snapshot that may override the public values only when a single
-- complete parse generation corroborates them.
CREATE TABLE job_file_result_count_evidence (
    job_file_result_id TEXT PRIMARY KEY REFERENCES job_file_results(id),
    source_document_id TEXT NOT NULL REFERENCES source_documents(id),
    successful_rows INTEGER NOT NULL CHECK (successful_rows >= 0),
    error_rows INTEGER NOT NULL CHECK (error_rows >= 0),
    evidence_rows INTEGER NOT NULL CHECK (
        evidence_rows = successful_rows + error_rows
    ),
    source_completed_at TEXT,
    result_updated_at TEXT NOT NULL,
    confidence TEXT NOT NULL CHECK (confidence IN ('EXACT', 'UNVERIFIED')),
    captured_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO job_file_result_count_evidence (
    job_file_result_id, source_document_id, successful_rows, error_rows,
    evidence_rows, source_completed_at, result_updated_at, confidence
)
SELECT result.id,
       result.source_document_id,
       SUM(CASE WHEN source_row.status = 'SUCCESS' THEN 1 ELSE 0 END),
       SUM(CASE WHEN source_row.status = 'ROW_ERROR' THEN 1 ELSE 0 END),
       COUNT(source_row.id),
       document.completed_at,
       result.updated_at,
       CASE
           WHEN COUNT(source_row.id) = history.total_rows
            AND SUM(CASE WHEN source_row.status = 'ROW_ERROR' THEN 1 ELSE 0 END) > 0
            AND (
                history.processed_rows = history.total_rows
                OR (
                    SUM(CASE WHEN source_row.status = 'SUCCESS' THEN 1 ELSE 0 END)
                        = history.processed_rows
                    AND SUM(CASE WHEN source_row.status = 'ROW_ERROR' THEN 1 ELSE 0 END)
                        = history.row_error_count
                )
            )
            AND document.completed_at IS NOT NULL
            AND SUM(CASE WHEN source_row.created_at = document.completed_at
                         THEN 0 ELSE 1 END) = 0
            AND result.updated_at >= document.completed_at
            AND NOT EXISTS (
                SELECT 1
                FROM job_file_results AS newer
                JOIN durable_jobs AS newer_job ON newer_job.id = newer.job_id
                WHERE newer.source_document_id = result.source_document_id
                  AND newer.id <> result.id
                  AND newer_job.job_type IN ('INGEST', 'PARSE')
                  AND newer.updated_at >= result.updated_at
            )
           THEN 'EXACT'
           ELSE 'UNVERIFIED'
       END
FROM job_file_result_count_history AS history
JOIN job_file_results AS result ON result.id = history.job_file_result_id
JOIN durable_jobs AS job ON job.id = result.job_id
JOIN source_documents AS document ON document.id = result.source_document_id
LEFT JOIN source_rows AS source_row
  ON source_row.source_document_id = result.source_document_id
WHERE history.confidence = 'EXACT'
  AND result.status = 'PARTIAL'
  AND history.total_rows > 0
  AND COALESCE(json_extract(
        CASE WHEN json_valid(result.error_json)
             THEN result.error_json ELSE '{}' END,
        '$.code'
      ), '') <> 'MAPPING_REQUIRED'
  AND job.job_type IN ('INGEST', 'PARSE')
GROUP BY result.id;

CREATE TRIGGER immutable_job_file_result_count_evidence_insert
BEFORE INSERT ON job_file_result_count_evidence
BEGIN SELECT RAISE(ABORT, 'historical job file count evidence is immutable'); END;

CREATE TRIGGER immutable_job_file_result_count_evidence_update
BEFORE UPDATE ON job_file_result_count_evidence
BEGIN SELECT RAISE(ABORT, 'historical job file count evidence is immutable'); END;

CREATE TRIGGER immutable_job_file_result_count_evidence_delete
BEFORE DELETE ON job_file_result_count_evidence
BEGIN SELECT RAISE(ABORT, 'historical job file count evidence is immutable'); END;

-- Seal the exact candidate collection version alongside every new approval.
ALTER TABLE approval_revisions
    ADD COLUMN candidate_collection_revision INTEGER NOT NULL DEFAULT 0
        CHECK (candidate_collection_revision >= 0);

-- The original sealing guard predates the collection revision column.  Replace
-- it so the newly bound value cannot be changed as part of the sealing UPDATE.
DROP TRIGGER immutable_sealed_approval_revision_update;
CREATE TRIGGER immutable_sealed_approval_revision_update
BEFORE UPDATE ON approval_revisions
FOR EACH ROW WHEN OLD.sealed_at IS NOT NULL OR NOT (
    OLD.sealed_at IS NULL AND NEW.sealed_at IS NOT NULL
    AND NEW.id = OLD.id AND NEW.school_id = OLD.school_id
    AND NEW.workspace_id = OLD.workspace_id
    AND NEW.revision_number = OLD.revision_number
    AND NEW.canonical_json = OLD.canonical_json AND NEW.sha256 = OLD.sha256
    AND NEW.budget_won = OLD.budget_won
    AND NEW.expected_total_won = OLD.expected_total_won
    AND NEW.created_by_user_id = OLD.created_by_user_id
    AND NEW.reason = OLD.reason AND NEW.created_at = OLD.created_at
    AND NEW.candidate_collection_revision = OLD.candidate_collection_revision
)
BEGIN SELECT RAISE(ABORT, 'approval revision is immutable'); END;
