ALTER TABLE source_documents
    ADD COLUMN parsed_config_version INTEGER;

UPDATE source_documents
SET parsed_config_version = (
    SELECT config.row_version
    FROM source_configurations AS config
    WHERE config.source_document_id = source_documents.id
)
WHERE status IN ('SUCCESS', 'ROW_ERROR')
  AND completed_at IS NOT NULL
  AND EXISTS (
      SELECT 1
      FROM source_configurations AS config
      WHERE config.source_document_id = source_documents.id
        AND completed_at >= config.updated_at
  );

ALTER TABLE upload_repair_obligations
    ADD COLUMN pending_source_document_id TEXT REFERENCES source_documents(id);

CREATE UNIQUE INDEX idx_upload_repair_pending_source
    ON upload_repair_obligations(pending_source_document_id)
    WHERE pending_source_document_id IS NOT NULL;

ALTER TABLE job_file_results
    ADD COLUMN row_error_count INTEGER NOT NULL DEFAULT 0
        CHECK (row_error_count >= 0);

UPDATE job_file_results
SET row_error_count = MIN(
        total_rows,
        (
            SELECT COUNT(*)
            FROM source_rows AS source_row
            WHERE source_row.source_document_id = job_file_results.source_document_id
              AND source_row.status = 'ROW_ERROR'
        )
    ),
    processed_rows = MAX(
        0,
        total_rows - MIN(
            total_rows,
            (
                SELECT COUNT(*)
                FROM source_rows AS source_row
                WHERE source_row.source_document_id = job_file_results.source_document_id
                  AND source_row.status = 'ROW_ERROR'
            )
        )
    )
WHERE job_id IN (
    SELECT id FROM durable_jobs WHERE job_type IN ('INGEST', 'PARSE')
);

DROP TRIGGER IF EXISTS immutable_terminal_source_document_update;

CREATE TRIGGER immutable_terminal_source_document_update
BEFORE UPDATE OF status, activation_allowed, parsed_config_version, completed_at
ON source_documents
FOR EACH ROW WHEN OLD.status <> 'PENDING' AND NOT (
    NEW.status = 'PENDING'
    AND NEW.activation_allowed = OLD.activation_allowed
    AND NEW.completed_at IS NULL
    AND NEW.parsed_config_version IS NULL
)
AND (
    NEW.status <> OLD.status
    OR NEW.activation_allowed <> OLD.activation_allowed
    OR NEW.parsed_config_version IS NOT OLD.parsed_config_version
    OR NEW.completed_at IS NOT OLD.completed_at
)
BEGIN SELECT RAISE(ABORT, 'parsed source document state is immutable'); END;

CREATE TRIGGER job_file_result_counts_insert
BEFORE INSERT ON job_file_results
FOR EACH ROW WHEN NEW.processed_rows + NEW.row_error_count > NEW.total_rows
BEGIN SELECT RAISE(ABORT, 'job file row counts exceed total rows'); END;

CREATE TRIGGER job_file_result_counts_update
BEFORE UPDATE OF total_rows, processed_rows, row_error_count ON job_file_results
FOR EACH ROW WHEN NEW.processed_rows + NEW.row_error_count > NEW.total_rows
BEGIN SELECT RAISE(ABORT, 'job file row counts exceed total rows'); END;

CREATE TRIGGER upload_repair_pending_source_scope_insert
BEFORE INSERT ON upload_repair_obligations
FOR EACH ROW WHEN NEW.pending_source_document_id IS NOT NULL AND NOT EXISTS (
    SELECT 1
    FROM source_documents AS source
    JOIN workspace_sources AS link
      ON link.source_document_id = source.id
    WHERE source.id = NEW.pending_source_document_id
      AND source.school_id = NEW.school_id
      AND link.school_id = NEW.school_id
      AND link.workspace_id = NEW.workspace_id
)
BEGIN SELECT RAISE(ABORT, 'upload repair pending source scope mismatch'); END;

CREATE TRIGGER upload_repair_pending_source_scope_update
BEFORE UPDATE OF pending_source_document_id ON upload_repair_obligations
FOR EACH ROW WHEN NEW.pending_source_document_id IS NOT NULL AND NOT EXISTS (
    SELECT 1
    FROM source_documents AS source
    JOIN workspace_sources AS link
      ON link.source_document_id = source.id
    WHERE source.id = NEW.pending_source_document_id
      AND source.school_id = NEW.school_id
      AND link.school_id = NEW.school_id
      AND link.workspace_id = NEW.workspace_id
)
BEGIN SELECT RAISE(ABORT, 'upload repair pending source scope mismatch'); END;
