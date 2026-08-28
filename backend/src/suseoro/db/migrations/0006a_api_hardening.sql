-- Task 7 forward-only API/operations hardening.
ALTER TABLE source_configurations ADD COLUMN vendor_scope TEXT NOT NULL DEFAULT '*';
ALTER TABLE source_configurations ADD COLUMN remember_template INTEGER NOT NULL DEFAULT 0
    CHECK (remember_template IN (0, 1));

ALTER TABLE parser_runs ADD COLUMN claim_token TEXT;
ALTER TABLE parser_runs ADD COLUMN claim_generation INTEGER NOT NULL DEFAULT 0;

DROP TRIGGER job_file_results_scope_insert;
DROP TRIGGER job_file_results_scope_update;
CREATE TRIGGER job_file_results_scope_insert
BEFORE INSERT ON job_file_results
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1 FROM durable_jobs j JOIN source_documents sd
      ON sd.id = NEW.source_document_id
    WHERE j.id = NEW.job_id AND j.school_id = sd.school_id
      AND j.job_type IN ('COMPARE', 'INGEST', 'PARSE') AND j.status = 'RUNNING'
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
      AND j.job_type IN ('COMPARE', 'INGEST', 'PARSE') AND j.status = 'RUNNING'
      AND j.claim_token = NEW.claim_token
      AND j.claim_generation = NEW.claim_generation
)
BEGIN SELECT RAISE(ABORT, 'job file result scope or claim mismatch'); END;

CREATE TABLE legacy_v1_workspaces (
    id TEXT PRIMARY KEY,
    school_id TEXT NOT NULL REFERENCES schools(id),
    display_name TEXT NOT NULL,
    source_copy_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    is_read_only INTEGER NOT NULL DEFAULT 1 CHECK (is_read_only = 1),
    imported_at TEXT NOT NULL,
    UNIQUE (school_id, sha256)
);
CREATE INDEX idx_legacy_v1_workspaces_school
    ON legacy_v1_workspaces(school_id, imported_at DESC, id DESC);

CREATE TABLE v1_catalog_candidates (
    id TEXT PRIMARY KEY,
    school_id TEXT NOT NULL REFERENCES schools(id),
    source_copy_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    row_count INTEGER NOT NULL CHECK (row_count > 0),
    status TEXT NOT NULL DEFAULT 'PENDING_CONFIRMATION' CHECK (
        status IN ('PENDING_CONFIRMATION', 'ACTIVATED', 'REJECTED')
    ),
    catalog_version_id TEXT REFERENCES catalog_versions(id),
    created_at TEXT NOT NULL,
    activated_at TEXT,
    UNIQUE (school_id, sha256)
);
CREATE INDEX idx_v1_catalog_candidates_school_status
    ON v1_catalog_candidates(school_id, status, created_at DESC, id DESC);
