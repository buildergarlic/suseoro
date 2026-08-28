ALTER TABLE source_documents ADD COLUMN requested_start_local_date TEXT;
ALTER TABLE source_documents ADD COLUMN requested_through_local_date TEXT;
ALTER TABLE job_file_results ADD COLUMN claim_generation INTEGER;

DROP TRIGGER job_file_results_scope_insert;
DROP TRIGGER job_file_results_scope_update;
CREATE TRIGGER job_file_results_scope_insert
BEFORE INSERT ON job_file_results
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1 FROM durable_jobs j JOIN source_documents sd
      ON sd.id = NEW.source_document_id
    WHERE j.id = NEW.job_id AND j.school_id = sd.school_id
      AND j.job_type = 'COMPARE' AND j.status = 'RUNNING'
      AND j.claim_token = NEW.claim_token
      AND j.claim_generation = NEW.claim_generation
)
BEGIN SELECT RAISE(ABORT, 'job file result scope or claim mismatch'); END;
CREATE TRIGGER job_file_results_scope_update
BEFORE UPDATE ON job_file_results
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1 FROM durable_jobs j JOIN source_documents sd
      ON sd.id = NEW.source_document_id
    WHERE j.id = NEW.job_id AND j.school_id = sd.school_id
      AND j.job_type = 'COMPARE' AND j.status = 'RUNNING'
      AND j.claim_token = NEW.claim_token
      AND j.claim_generation = NEW.claim_generation
)
BEGIN SELECT RAISE(ABORT, 'job file result scope or claim mismatch'); END;

-- Delta provenance includes the exact inclusive Asia/Seoul export window.
CREATE TRIGGER catalog_delta_document_window_insert
BEFORE INSERT ON source_documents
FOR EACH ROW WHEN NEW.role IN (
    'CATALOG_DELTA_REGISTRATION', 'CATALOG_DELTA_UPDATE'
) AND (
    NEW.requested_start_local_date IS NULL
    OR NEW.requested_through_local_date IS NULL
    OR date(NEW.requested_start_local_date) IS NULL
    OR date(NEW.requested_through_local_date) IS NULL
    OR NEW.requested_start_local_date > NEW.requested_through_local_date
)
BEGIN SELECT RAISE(ABORT, 'catalog delta source window is required'); END;

CREATE TRIGGER catalog_delta_document_window_update
BEFORE UPDATE ON source_documents
FOR EACH ROW WHEN NEW.role IN (
    'CATALOG_DELTA_REGISTRATION', 'CATALOG_DELTA_UPDATE'
) AND (
    NEW.requested_start_local_date IS NULL
    OR NEW.requested_through_local_date IS NULL
    OR date(NEW.requested_start_local_date) IS NULL
    OR date(NEW.requested_through_local_date) IS NULL
    OR NEW.requested_start_local_date > NEW.requested_through_local_date
)
BEGIN SELECT RAISE(ABORT, 'catalog delta source window is required'); END;

CREATE TRIGGER catalog_delta_batches_window_insert
BEFORE INSERT ON catalog_delta_batches
FOR EACH ROW WHEN NEW.registration_source_document_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM source_documents r, source_documents u
    WHERE r.id = NEW.registration_source_document_id
      AND u.id = NEW.update_source_document_id
      AND r.requested_start_local_date = NEW.requested_start_local_date
      AND u.requested_start_local_date = NEW.requested_start_local_date
      AND r.requested_through_local_date = NEW.through_local_date
      AND u.requested_through_local_date = NEW.through_local_date
)
BEGIN SELECT RAISE(ABORT, 'catalog delta batch window mismatch'); END;

CREATE TRIGGER catalog_delta_batches_window_update
BEFORE UPDATE OF registration_source_document_id, update_source_document_id,
                 requested_start_local_date, through_local_date
ON catalog_delta_batches
FOR EACH ROW WHEN NEW.registration_source_document_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM source_documents r, source_documents u
    WHERE r.id = NEW.registration_source_document_id
      AND u.id = NEW.update_source_document_id
      AND r.requested_start_local_date = NEW.requested_start_local_date
      AND u.requested_start_local_date = NEW.requested_start_local_date
      AND r.requested_through_local_date = NEW.through_local_date
      AND u.requested_through_local_date = NEW.through_local_date
)
BEGIN SELECT RAISE(ABORT, 'catalog delta batch window mismatch'); END;

-- Parent and tenant identity links never change after insertion. Mutable
-- lifecycle fields (status, counts, progress, names) remain available.
CREATE TRIGGER immutable_source_files_identity_update
BEFORE UPDATE OF id, sha256, size_bytes, storage_path, detected_format ON source_files
FOR EACH ROW WHEN
    NEW.id <> OLD.id
    OR (
        NEW.sha256 <> OLD.sha256
        AND NOT EXISTS (
            SELECT 1 FROM parser_runs WHERE source_file_sha256 = OLD.sha256
        )
    )
    OR NEW.size_bytes <> OLD.size_bytes OR NEW.storage_path <> OLD.storage_path
    OR NEW.detected_format <> OLD.detected_format
BEGIN SELECT RAISE(ABORT, 'source file identity is immutable'); END;

CREATE TRIGGER immutable_source_documents_identity_update
BEFORE UPDATE OF id, source_file_id, school_id, role, parser_version,
                 detected_format, requested_start_local_date,
                 requested_through_local_date
ON source_documents
FOR EACH ROW WHEN
    NEW.id <> OLD.id OR NEW.source_file_id <> OLD.source_file_id
    OR NEW.school_id IS NOT OLD.school_id OR NEW.role <> OLD.role
    OR NEW.parser_version <> OLD.parser_version
    OR NEW.detected_format <> OLD.detected_format
    OR NEW.requested_start_local_date IS NOT OLD.requested_start_local_date
    OR NEW.requested_through_local_date IS NOT OLD.requested_through_local_date
BEGIN SELECT RAISE(ABORT, 'source document identity is immutable'); END;

CREATE TRIGGER immutable_terminal_source_document_update
BEFORE UPDATE OF status, activation_allowed ON source_documents
FOR EACH ROW WHEN OLD.status <> 'PENDING' AND (
    NEW.status <> OLD.status OR NEW.activation_allowed <> OLD.activation_allowed
)
BEGIN SELECT RAISE(ABORT, 'parsed source document state is immutable'); END;

CREATE TRIGGER immutable_source_rows_identity_update
BEFORE UPDATE OF id, source_document_id, source_row ON source_rows
FOR EACH ROW WHEN
    NEW.id <> OLD.id OR NEW.source_document_id <> OLD.source_document_id
    OR NEW.source_row <> OLD.source_row
BEGIN SELECT RAISE(ABORT, 'source row identity is immutable'); END;

CREATE TRIGGER immutable_catalog_versions_identity_update
BEFORE UPDATE OF id, school_id, source_type, import_mode, source_document_id,
                 parent_version_id
ON catalog_versions
FOR EACH ROW WHEN
    NEW.id <> OLD.id OR NEW.school_id <> OLD.school_id
    OR NEW.source_type <> OLD.source_type OR NEW.import_mode <> OLD.import_mode
    OR NEW.source_document_id IS NOT OLD.source_document_id
    OR NEW.parent_version_id IS NOT OLD.parent_version_id
BEGIN SELECT RAISE(ABORT, 'catalog version identity is immutable'); END;

CREATE TRIGGER immutable_holdings_identity_update
BEFORE UPDATE OF id, catalog_version_id, school_id, source_item_id ON holdings
FOR EACH ROW WHEN
    NEW.id <> OLD.id OR NEW.catalog_version_id <> OLD.catalog_version_id
    OR NEW.school_id <> OLD.school_id OR NEW.source_item_id <> OLD.source_item_id
BEGIN SELECT RAISE(ABORT, 'holding identity is immutable'); END;

CREATE TRIGGER immutable_normalized_works_identity_update
BEFORE UPDATE OF id, holding_id, catalog_version_id, school_id ON normalized_works
FOR EACH ROW WHEN
    NEW.id <> OLD.id OR NEW.holding_id <> OLD.holding_id
    OR NEW.catalog_version_id <> OLD.catalog_version_id
    OR NEW.school_id <> OLD.school_id
BEGIN SELECT RAISE(ABORT, 'normalized work identity is immutable'); END;

CREATE TRIGGER immutable_workspaces_identity_update
BEFORE UPDATE OF id, school_id ON acquisition_workspaces
FOR EACH ROW WHEN NEW.id <> OLD.id OR NEW.school_id <> OLD.school_id
BEGIN SELECT RAISE(ABORT, 'workspace identity is immutable'); END;

CREATE TRIGGER immutable_durable_jobs_identity_update
BEFORE UPDATE OF id, school_id, workspace_id, job_type ON durable_jobs
FOR EACH ROW WHEN
    NEW.id <> OLD.id OR NEW.school_id <> OLD.school_id
    OR NEW.workspace_id IS NOT OLD.workspace_id OR NEW.job_type <> OLD.job_type
BEGIN SELECT RAISE(ABORT, 'durable job identity is immutable'); END;
