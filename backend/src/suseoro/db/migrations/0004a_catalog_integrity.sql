PRAGMA defer_foreign_keys = ON;

-- Expand the immutable ingestion ledger for the document and MARC parsers that
-- already exist in Task 4.  Rebuilds keep identifiers and child foreign keys.
DROP TRIGGER validate_source_files_insert;
DROP TRIGGER validate_source_files_update;
DROP TRIGGER validate_source_documents_insert;
DROP TRIGGER validate_source_documents_update;
DROP TRIGGER parser_runs_source_file_insert;
DROP TRIGGER parser_runs_source_file_update;
DROP TRIGGER source_files_parser_runs_delete;
DROP TRIGGER source_files_parser_runs_sha_update;

CREATE TABLE source_files_new (
    id TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL UNIQUE CHECK (
        length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    storage_path TEXT NOT NULL,
    original_filename TEXT,
    detected_format TEXT NOT NULL CHECK (
        detected_format IN (
            'CSV', 'TSV', 'TXT', 'XLS', 'XLSX', 'XLSB', 'ODS',
            'DOCX', 'HWPX', 'PDF', 'HWP', 'MARC', 'UNKNOWN'
        )
    ),
    created_at TEXT NOT NULL
);
INSERT INTO source_files_new SELECT * FROM source_files;
DROP TABLE source_files;
ALTER TABLE source_files_new RENAME TO source_files;

CREATE TABLE source_documents_new (
    id TEXT PRIMARY KEY,
    source_file_id TEXT NOT NULL REFERENCES source_files(id),
    school_id TEXT REFERENCES schools(id),
    role TEXT NOT NULL CHECK (
        role IN (
            'UNKNOWN', 'PURCHASE_REQUEST', 'VENDOR_QUOTE', 'INVENTORY',
            'CATALOG_FULL', 'CATALOG_DELTA_REGISTRATION', 'CATALOG_DELTA_UPDATE'
        )
    ),
    parser_version TEXT NOT NULL,
    template_version TEXT,
    status TEXT NOT NULL CHECK (status IN ('PENDING', 'SUCCESS', 'ROW_ERROR', 'FAILED')),
    detected_format TEXT NOT NULL CHECK (
        detected_format IN (
            'CSV', 'TSV', 'TXT', 'XLS', 'XLSX', 'XLSB', 'ODS',
            'DOCX', 'HWPX', 'PDF', 'HWP', 'MARC', 'UNKNOWN'
        )
    ),
    activation_allowed INTEGER NOT NULL DEFAULT 1 CHECK (activation_allowed IN (0, 1)),
    created_at TEXT NOT NULL,
    completed_at TEXT
);
INSERT INTO source_documents_new (
    id, source_file_id, school_id, role, parser_version, template_version,
    status, detected_format, activation_allowed, created_at, completed_at
)
SELECT id, source_file_id, school_id, role, parser_version, template_version,
       status, detected_format, 1, created_at, completed_at
FROM source_documents;
DROP TABLE source_documents;
ALTER TABLE source_documents_new RENAME TO source_documents;

DROP INDEX idx_parser_runs_cache;
CREATE TABLE parser_runs_new (
    id TEXT PRIMARY KEY,
    source_file_sha256 TEXT NOT NULL CHECK (
        length(source_file_sha256) = 64
        AND source_file_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    parser_version TEXT NOT NULL,
    role TEXT NOT NULL CHECK (
        role IN (
            'UNKNOWN', 'PURCHASE_REQUEST', 'VENDOR_QUOTE', 'INVENTORY',
            'CATALOG_FULL', 'CATALOG_DELTA_REGISTRATION', 'CATALOG_DELTA_UPDATE'
        )
    ),
    status TEXT NOT NULL CHECK (status IN ('PENDING', 'SUCCESS', 'FAILED')),
    result_json TEXT,
    error_json TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE (source_file_sha256, parser_version, role)
);
INSERT INTO parser_runs_new SELECT * FROM parser_runs;
DROP TABLE parser_runs;
ALTER TABLE parser_runs_new RENAME TO parser_runs;

CREATE INDEX idx_source_documents_file_role
    ON source_documents(source_file_id, role, parser_version);
CREATE INDEX idx_source_documents_school_created
    ON source_documents(school_id, created_at DESC);
CREATE UNIQUE INDEX idx_source_documents_unique_parse
    ON source_documents(source_file_id, role, parser_version);
CREATE UNIQUE INDEX idx_parser_runs_cache
    ON parser_runs(source_file_sha256, parser_version, role);

CREATE TRIGGER validate_source_files_insert
BEFORE INSERT ON source_files
FOR EACH ROW WHEN
    NOT (NEW.id GLOB '????????-????-????-????-????????????' AND NEW.id NOT GLOB '*[^0-9A-Fa-f-]*')
    OR NOT ((NEW.created_at GLOB '????-??-??T??:??:??Z' OR NEW.created_at GLOB '????-??-??T??:??:??.[0-9]*Z') AND NEW.created_at NOT GLOB '*[^0-9T:.Z-]*' AND strftime('%Y-%m-%dT%H:%M:%S', NEW.created_at) = substr(NEW.created_at, 1, 19))
BEGIN SELECT RAISE(ABORT, 'invalid source_files identifier or UTC timestamp'); END;

CREATE TRIGGER validate_source_files_update
BEFORE UPDATE ON source_files
FOR EACH ROW WHEN
    NOT (NEW.id GLOB '????????-????-????-????-????????????' AND NEW.id NOT GLOB '*[^0-9A-Fa-f-]*')
    OR NOT ((NEW.created_at GLOB '????-??-??T??:??:??Z' OR NEW.created_at GLOB '????-??-??T??:??:??.[0-9]*Z') AND NEW.created_at NOT GLOB '*[^0-9T:.Z-]*' AND strftime('%Y-%m-%dT%H:%M:%S', NEW.created_at) = substr(NEW.created_at, 1, 19))
BEGIN SELECT RAISE(ABORT, 'invalid source_files identifier or UTC timestamp'); END;

CREATE TRIGGER validate_source_documents_insert
BEFORE INSERT ON source_documents
FOR EACH ROW WHEN
    NOT (NEW.id GLOB '????????-????-????-????-????????????' AND NEW.id NOT GLOB '*[^0-9A-Fa-f-]*')
    OR NOT (NEW.source_file_id GLOB '????????-????-????-????-????????????' AND NEW.source_file_id NOT GLOB '*[^0-9A-Fa-f-]*')
    OR (NEW.school_id IS NOT NULL AND NOT (NEW.school_id GLOB '????????-????-????-????-????????????' AND NEW.school_id NOT GLOB '*[^0-9A-Fa-f-]*'))
    OR NOT ((NEW.created_at GLOB '????-??-??T??:??:??Z' OR NEW.created_at GLOB '????-??-??T??:??:??.[0-9]*Z') AND NEW.created_at NOT GLOB '*[^0-9T:.Z-]*' AND strftime('%Y-%m-%dT%H:%M:%S', NEW.created_at) = substr(NEW.created_at, 1, 19))
    OR (NEW.completed_at IS NOT NULL AND NOT ((NEW.completed_at GLOB '????-??-??T??:??:??Z' OR NEW.completed_at GLOB '????-??-??T??:??:??.[0-9]*Z') AND NEW.completed_at NOT GLOB '*[^0-9T:.Z-]*' AND strftime('%Y-%m-%dT%H:%M:%S', NEW.completed_at) = substr(NEW.completed_at, 1, 19)))
BEGIN SELECT RAISE(ABORT, 'invalid source_documents identifier or UTC timestamp'); END;

CREATE TRIGGER validate_source_documents_update
BEFORE UPDATE ON source_documents
FOR EACH ROW WHEN
    NOT (NEW.id GLOB '????????-????-????-????-????????????' AND NEW.id NOT GLOB '*[^0-9A-Fa-f-]*')
    OR NOT (NEW.source_file_id GLOB '????????-????-????-????-????????????' AND NEW.source_file_id NOT GLOB '*[^0-9A-Fa-f-]*')
    OR (NEW.school_id IS NOT NULL AND NOT (NEW.school_id GLOB '????????-????-????-????-????????????' AND NEW.school_id NOT GLOB '*[^0-9A-Fa-f-]*'))
    OR NOT ((NEW.created_at GLOB '????-??-??T??:??:??Z' OR NEW.created_at GLOB '????-??-??T??:??:??.[0-9]*Z') AND NEW.created_at NOT GLOB '*[^0-9T:.Z-]*' AND strftime('%Y-%m-%dT%H:%M:%S', NEW.created_at) = substr(NEW.created_at, 1, 19))
    OR (NEW.completed_at IS NOT NULL AND NOT ((NEW.completed_at GLOB '????-??-??T??:??:??Z' OR NEW.completed_at GLOB '????-??-??T??:??:??.[0-9]*Z') AND NEW.completed_at NOT GLOB '*[^0-9T:.Z-]*' AND strftime('%Y-%m-%dT%H:%M:%S', NEW.completed_at) = substr(NEW.completed_at, 1, 19)))
BEGIN SELECT RAISE(ABORT, 'invalid source_documents identifier or UTC timestamp'); END;

CREATE TRIGGER parser_runs_source_file_insert
BEFORE INSERT ON parser_runs
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1 FROM source_files WHERE sha256 = NEW.source_file_sha256
)
BEGIN SELECT RAISE(ABORT, 'parser run source file does not exist'); END;

CREATE TRIGGER validate_parser_runs_insert
BEFORE INSERT ON parser_runs
FOR EACH ROW WHEN
    NOT (NEW.id GLOB '????????-????-????-????-????????????' AND NEW.id NOT GLOB '*[^0-9A-Fa-f-]*')
    OR NOT ((NEW.created_at GLOB '????-??-??T??:??:??Z' OR NEW.created_at GLOB '????-??-??T??:??:??.[0-9]*Z') AND NEW.created_at NOT GLOB '*[^0-9T:.Z-]*' AND strftime('%Y-%m-%dT%H:%M:%S', NEW.created_at) = substr(NEW.created_at, 1, 19))
    OR (NEW.completed_at IS NOT NULL AND NOT ((NEW.completed_at GLOB '????-??-??T??:??:??Z' OR NEW.completed_at GLOB '????-??-??T??:??:??.[0-9]*Z') AND NEW.completed_at NOT GLOB '*[^0-9T:.Z-]*' AND strftime('%Y-%m-%dT%H:%M:%S', NEW.completed_at) = substr(NEW.completed_at, 1, 19)))
BEGIN SELECT RAISE(ABORT, 'invalid parser_runs identifier or UTC timestamp'); END;

CREATE TRIGGER validate_parser_runs_update
BEFORE UPDATE ON parser_runs
FOR EACH ROW WHEN
    NOT (NEW.id GLOB '????????-????-????-????-????????????' AND NEW.id NOT GLOB '*[^0-9A-Fa-f-]*')
    OR NOT ((NEW.created_at GLOB '????-??-??T??:??:??Z' OR NEW.created_at GLOB '????-??-??T??:??:??.[0-9]*Z') AND NEW.created_at NOT GLOB '*[^0-9T:.Z-]*' AND strftime('%Y-%m-%dT%H:%M:%S', NEW.created_at) = substr(NEW.created_at, 1, 19))
    OR (NEW.completed_at IS NOT NULL AND NOT ((NEW.completed_at GLOB '????-??-??T??:??:??Z' OR NEW.completed_at GLOB '????-??-??T??:??:??.[0-9]*Z') AND NEW.completed_at NOT GLOB '*[^0-9T:.Z-]*' AND strftime('%Y-%m-%dT%H:%M:%S', NEW.completed_at) = substr(NEW.completed_at, 1, 19)))
BEGIN SELECT RAISE(ABORT, 'invalid parser_runs identifier or UTC timestamp'); END;

CREATE TRIGGER parser_runs_source_file_update
BEFORE UPDATE OF source_file_sha256 ON parser_runs
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1 FROM source_files WHERE sha256 = NEW.source_file_sha256
)
BEGIN SELECT RAISE(ABORT, 'parser run source file does not exist'); END;

CREATE TRIGGER source_files_parser_runs_delete
BEFORE DELETE ON source_files
FOR EACH ROW WHEN EXISTS (
    SELECT 1 FROM parser_runs WHERE source_file_sha256 = OLD.sha256
)
BEGIN SELECT RAISE(ABORT, 'source file has parser runs'); END;

CREATE TRIGGER source_files_parser_runs_sha_update
BEFORE UPDATE OF sha256 ON source_files
FOR EACH ROW WHEN NEW.sha256 <> OLD.sha256 AND EXISTS (
    SELECT 1 FROM parser_runs WHERE source_file_sha256 = OLD.sha256
)
BEGIN SELECT RAISE(ABORT, 'source file has parser runs'); END;

ALTER TABLE catalog_versions ADD COLUMN expected_active_version_id TEXT REFERENCES catalog_versions(id);
ALTER TABLE catalog_versions ADD COLUMN expected_active_item_count INTEGER;
ALTER TABLE catalog_versions ADD COLUMN expected_active_source_type TEXT;

ALTER TABLE durable_jobs ADD COLUMN claim_token TEXT;
ALTER TABLE durable_jobs ADD COLUMN claim_generation INTEGER NOT NULL DEFAULT 0;

ALTER TABLE catalog_delta_batches ADD COLUMN registration_source_document_id TEXT REFERENCES source_documents(id);
ALTER TABLE catalog_delta_batches ADD COLUMN update_source_document_id TEXT REFERENCES source_documents(id);
ALTER TABLE catalog_delta_batches ADD COLUMN requested_start_local_date TEXT;
ALTER TABLE catalog_delta_batches ADD COLUMN registration_total_rows INTEGER NOT NULL DEFAULT 0;
ALTER TABLE catalog_delta_batches ADD COLUMN registration_success_rows INTEGER NOT NULL DEFAULT 0;
ALTER TABLE catalog_delta_batches ADD COLUMN registration_error_rows INTEGER NOT NULL DEFAULT 0;
ALTER TABLE catalog_delta_batches ADD COLUMN update_total_rows INTEGER NOT NULL DEFAULT 0;
ALTER TABLE catalog_delta_batches ADD COLUMN update_success_rows INTEGER NOT NULL DEFAULT 0;
ALTER TABLE catalog_delta_batches ADD COLUMN update_error_rows INTEGER NOT NULL DEFAULT 0;

ALTER TABLE job_file_results ADD COLUMN claim_token TEXT;

CREATE TABLE catalog_delta_row_results (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES catalog_delta_batches(id) ON DELETE CASCADE,
    school_id TEXT NOT NULL REFERENCES schools(id),
    role TEXT NOT NULL CHECK (role IN ('REGISTRATION', 'UPDATE')),
    source_document_id TEXT NOT NULL REFERENCES source_documents(id),
    source_row_id TEXT NOT NULL REFERENCES source_rows(id),
    outcome TEXT NOT NULL CHECK (outcome IN ('APPLIED', 'ROW_ERROR')),
    reason TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (batch_id, role, source_row_id)
);
CREATE INDEX idx_catalog_delta_row_results_batch_role
    ON catalog_delta_row_results(batch_id, role, outcome);

-- FTS is now a trigger-maintained external-content index.  The public name is
-- a read-only view, so application SQL cannot mutate an independent copy.
DROP TABLE holding_search_fts;
CREATE VIRTUAL TABLE holding_search_fts_index USING fts5(
    holding_id UNINDEXED,
    school_id UNINDEXED,
    catalog_version_id UNINDEXED,
    search_text,
    content = 'normalized_works',
    content_rowid = 'rowid',
    tokenize = 'unicode61 remove_diacritics 2'
);
CREATE TRIGGER normalized_works_fts_insert AFTER INSERT ON normalized_works BEGIN
    INSERT INTO holding_search_fts_index(
        rowid, holding_id, school_id, catalog_version_id, search_text
    ) VALUES (
        NEW.rowid, NEW.holding_id, NEW.school_id, NEW.catalog_version_id, NEW.search_text
    );
END;
CREATE TRIGGER normalized_works_fts_delete AFTER DELETE ON normalized_works BEGIN
    INSERT INTO holding_search_fts_index(
        holding_search_fts_index, rowid, holding_id, school_id,
        catalog_version_id, search_text
    ) VALUES (
        'delete', OLD.rowid, OLD.holding_id, OLD.school_id,
        OLD.catalog_version_id, OLD.search_text
    );
END;
CREATE TRIGGER normalized_works_fts_update AFTER UPDATE ON normalized_works BEGIN
    INSERT INTO holding_search_fts_index(
        holding_search_fts_index, rowid, holding_id, school_id,
        catalog_version_id, search_text
    ) VALUES (
        'delete', OLD.rowid, OLD.holding_id, OLD.school_id,
        OLD.catalog_version_id, OLD.search_text
    );
    INSERT INTO holding_search_fts_index(
        rowid, holding_id, school_id, catalog_version_id, search_text
    ) VALUES (
        NEW.rowid, NEW.holding_id, NEW.school_id, NEW.catalog_version_id, NEW.search_text
    );
END;
INSERT INTO holding_search_fts_index(holding_search_fts_index) VALUES ('rebuild');
CREATE VIEW holding_search_fts AS
SELECT rowid, holding_id, school_id, catalog_version_id, search_text
FROM holding_search_fts_index;

CREATE TRIGGER immutable_sealed_holdings_insert
BEFORE INSERT ON holdings
FOR EACH ROW WHEN EXISTS (
    SELECT 1 FROM catalog_versions
    WHERE id = NEW.catalog_version_id AND status <> 'STAGING'
)
BEGIN SELECT RAISE(ABORT, 'sealed catalog holdings are immutable'); END;

DROP TRIGGER immutable_catalog_version_update;
CREATE TRIGGER immutable_catalog_version_update
BEFORE UPDATE ON catalog_versions
FOR EACH ROW WHEN OLD.status IN ('ACTIVE', 'SUPERSEDED') AND NOT (
    OLD.status = 'ACTIVE'
    AND NEW.status = 'SUPERSEDED'
    AND NEW.id = OLD.id
    AND NEW.school_id = OLD.school_id
    AND NEW.source_type = OLD.source_type
    AND NEW.import_mode = OLD.import_mode
    AND NEW.source_document_id IS OLD.source_document_id
    AND NEW.parent_version_id IS OLD.parent_version_id
    AND NEW.item_count = OLD.item_count
    AND NEW.as_of_local_date IS OLD.as_of_local_date
    AND NEW.anomaly_confirmed = OLD.anomaly_confirmed
    AND NEW.created_at = OLD.created_at
    AND NEW.activated_at IS OLD.activated_at
    AND NEW.expected_active_version_id IS OLD.expected_active_version_id
    AND NEW.expected_active_item_count IS OLD.expected_active_item_count
    AND NEW.expected_active_source_type IS OLD.expected_active_source_type
)
BEGIN SELECT RAISE(ABORT, 'activated catalog versions are immutable'); END;

-- Tenant graph constraints protect direct SQL as well as service entry points.
CREATE TRIGGER durable_jobs_workspace_scope_insert
BEFORE INSERT ON durable_jobs
FOR EACH ROW WHEN NEW.workspace_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM acquisition_workspaces
    WHERE id = NEW.workspace_id AND school_id = NEW.school_id
)
BEGIN SELECT RAISE(ABORT, 'durable job workspace scope mismatch'); END;
CREATE TRIGGER durable_jobs_workspace_scope_update
BEFORE UPDATE OF school_id, workspace_id ON durable_jobs
FOR EACH ROW WHEN NEW.workspace_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM acquisition_workspaces
    WHERE id = NEW.workspace_id AND school_id = NEW.school_id
)
BEGIN SELECT RAISE(ABORT, 'durable job workspace scope mismatch'); END;

CREATE TRIGGER acquisition_workspaces_school_scope_update
BEFORE UPDATE OF school_id ON acquisition_workspaces
FOR EACH ROW WHEN NEW.school_id <> OLD.school_id AND EXISTS (
    SELECT 1 FROM durable_jobs WHERE workspace_id = OLD.id
    UNION ALL SELECT 1 FROM recommendations WHERE workspace_id = OLD.id
    UNION ALL SELECT 1 FROM comparison_file_results WHERE workspace_id = OLD.id
    UNION ALL SELECT 1 FROM comparison_row_results WHERE workspace_id = OLD.id
)
BEGIN SELECT RAISE(ABORT, 'workspace school scope is immutable once referenced'); END;

CREATE TRIGGER source_documents_school_scope_update
BEFORE UPDATE OF school_id ON source_documents
FOR EACH ROW WHEN NEW.school_id IS NOT OLD.school_id AND EXISTS (
    SELECT 1 FROM recommendations WHERE source_document_id = OLD.id
    UNION ALL SELECT 1 FROM comparison_file_results WHERE source_document_id = OLD.id
    UNION ALL SELECT 1 FROM comparison_row_results WHERE source_document_id = OLD.id
    UNION ALL SELECT 1 FROM job_file_results WHERE source_document_id = OLD.id
)
BEGIN SELECT RAISE(ABORT, 'source document school scope is immutable once referenced'); END;

CREATE TRIGGER source_rows_document_scope_update
BEFORE UPDATE OF source_document_id ON source_rows
FOR EACH ROW WHEN NEW.source_document_id <> OLD.source_document_id AND EXISTS (
    SELECT 1 FROM recommendations WHERE source_row_id = OLD.id
    UNION ALL SELECT 1 FROM comparison_row_results WHERE source_row_id = OLD.id
)
BEGIN SELECT RAISE(ABORT, 'source row document scope is immutable once referenced'); END;

CREATE TRIGGER catalog_versions_source_scope_insert
BEFORE INSERT ON catalog_versions
FOR EACH ROW WHEN NEW.source_document_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM source_documents
    WHERE id = NEW.source_document_id AND school_id = NEW.school_id
)
BEGIN SELECT RAISE(ABORT, 'catalog version source scope mismatch'); END;
CREATE TRIGGER catalog_versions_source_scope_update
BEFORE UPDATE OF school_id, source_document_id ON catalog_versions
FOR EACH ROW WHEN NEW.source_document_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM source_documents
    WHERE id = NEW.source_document_id AND school_id = NEW.school_id
)
BEGIN SELECT RAISE(ABORT, 'catalog version source scope mismatch'); END;

CREATE TRIGGER holdings_scope_insert
BEFORE INSERT ON holdings
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1 FROM catalog_versions
    WHERE id = NEW.catalog_version_id AND school_id = NEW.school_id
) OR (NEW.source_row_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM source_rows sr JOIN source_documents sd
      ON sd.id = sr.source_document_id
    WHERE sr.id = NEW.source_row_id AND sd.school_id = NEW.school_id
))
BEGIN SELECT RAISE(ABORT, 'holding scope mismatch'); END;
CREATE TRIGGER holdings_scope_update
BEFORE UPDATE OF catalog_version_id, school_id, source_row_id ON holdings
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1 FROM catalog_versions
    WHERE id = NEW.catalog_version_id AND school_id = NEW.school_id
) OR (NEW.source_row_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM source_rows sr JOIN source_documents sd
      ON sd.id = sr.source_document_id
    WHERE sr.id = NEW.source_row_id AND sd.school_id = NEW.school_id
))
BEGIN SELECT RAISE(ABORT, 'holding scope mismatch'); END;

CREATE TRIGGER normalized_works_scope_insert
BEFORE INSERT ON normalized_works
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1 FROM holdings h
    WHERE h.id = NEW.holding_id
      AND h.catalog_version_id = NEW.catalog_version_id
      AND h.school_id = NEW.school_id
)
BEGIN SELECT RAISE(ABORT, 'normalized work scope mismatch'); END;
CREATE TRIGGER normalized_works_scope_update
BEFORE UPDATE OF holding_id, catalog_version_id, school_id ON normalized_works
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1 FROM holdings h
    WHERE h.id = NEW.holding_id
      AND h.catalog_version_id = NEW.catalog_version_id
      AND h.school_id = NEW.school_id
)
BEGIN SELECT RAISE(ABORT, 'normalized work scope mismatch'); END;

CREATE TRIGGER recommendations_scope_insert
BEFORE INSERT ON recommendations
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1
    FROM acquisition_workspaces aw
    JOIN source_documents sd ON sd.id = NEW.source_document_id
    JOIN source_rows sr ON sr.id = NEW.source_row_id
    WHERE aw.id = NEW.workspace_id AND aw.school_id = NEW.school_id
      AND sd.school_id = NEW.school_id
      AND sr.source_document_id = NEW.source_document_id
)
BEGIN SELECT RAISE(ABORT, 'recommendation scope mismatch'); END;
CREATE TRIGGER recommendations_scope_update
BEFORE UPDATE OF school_id, workspace_id, source_document_id, source_row_id
ON recommendations
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1
    FROM acquisition_workspaces aw
    JOIN source_documents sd ON sd.id = NEW.source_document_id
    JOIN source_rows sr ON sr.id = NEW.source_row_id
    WHERE aw.id = NEW.workspace_id AND aw.school_id = NEW.school_id
      AND sd.school_id = NEW.school_id
      AND sr.source_document_id = NEW.source_document_id
)
BEGIN SELECT RAISE(ABORT, 'recommendation scope mismatch'); END;

CREATE TRIGGER candidate_decisions_scope_insert
BEFORE INSERT ON candidate_decisions
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1 FROM recommendations r
    WHERE r.id = NEW.recommendation_id
      AND r.school_id = NEW.school_id AND r.workspace_id = NEW.workspace_id
) OR (NEW.evidence_holding_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM holdings h
    WHERE h.id = NEW.evidence_holding_id AND h.school_id = NEW.school_id
))
BEGIN SELECT RAISE(ABORT, 'candidate decision scope mismatch'); END;
CREATE TRIGGER candidate_decisions_scope_update
BEFORE UPDATE OF school_id, workspace_id, recommendation_id, evidence_holding_id
ON candidate_decisions
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1 FROM recommendations r
    WHERE r.id = NEW.recommendation_id
      AND r.school_id = NEW.school_id AND r.workspace_id = NEW.workspace_id
) OR (NEW.evidence_holding_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM holdings h
    WHERE h.id = NEW.evidence_holding_id AND h.school_id = NEW.school_id
))
BEGIN SELECT RAISE(ABORT, 'candidate decision scope mismatch'); END;

CREATE TRIGGER comparison_file_results_scope_insert
BEFORE INSERT ON comparison_file_results
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1 FROM acquisition_workspaces aw JOIN source_documents sd
      ON sd.id = NEW.source_document_id
    WHERE aw.id = NEW.workspace_id AND aw.school_id = NEW.school_id
      AND sd.school_id = NEW.school_id
)
BEGIN SELECT RAISE(ABORT, 'comparison file scope mismatch'); END;
CREATE TRIGGER comparison_file_results_scope_update
BEFORE UPDATE OF school_id, workspace_id, source_document_id
ON comparison_file_results
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1 FROM acquisition_workspaces aw JOIN source_documents sd
      ON sd.id = NEW.source_document_id
    WHERE aw.id = NEW.workspace_id AND aw.school_id = NEW.school_id
      AND sd.school_id = NEW.school_id
)
BEGIN SELECT RAISE(ABORT, 'comparison file scope mismatch'); END;

CREATE TRIGGER comparison_row_results_scope_insert
BEFORE INSERT ON comparison_row_results
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1
    FROM acquisition_workspaces aw
    JOIN source_documents sd ON sd.id = NEW.source_document_id
    JOIN source_rows sr ON sr.id = NEW.source_row_id
    WHERE aw.id = NEW.workspace_id AND aw.school_id = NEW.school_id
      AND sd.school_id = NEW.school_id
      AND sr.source_document_id = NEW.source_document_id
) OR (NEW.recommendation_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM recommendations r
    WHERE r.id = NEW.recommendation_id
      AND r.school_id = NEW.school_id AND r.workspace_id = NEW.workspace_id
      AND r.source_row_id = NEW.source_row_id
)) OR (NEW.evidence_holding_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM holdings h
    WHERE h.id = NEW.evidence_holding_id AND h.school_id = NEW.school_id
))
BEGIN SELECT RAISE(ABORT, 'comparison row scope mismatch'); END;
CREATE TRIGGER comparison_row_results_scope_update
BEFORE UPDATE OF school_id, workspace_id, source_document_id, source_row_id,
                 recommendation_id, evidence_holding_id
ON comparison_row_results
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1
    FROM acquisition_workspaces aw
    JOIN source_documents sd ON sd.id = NEW.source_document_id
    JOIN source_rows sr ON sr.id = NEW.source_row_id
    WHERE aw.id = NEW.workspace_id AND aw.school_id = NEW.school_id
      AND sd.school_id = NEW.school_id
      AND sr.source_document_id = NEW.source_document_id
) OR (NEW.recommendation_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM recommendations r
    WHERE r.id = NEW.recommendation_id
      AND r.school_id = NEW.school_id AND r.workspace_id = NEW.workspace_id
      AND r.source_row_id = NEW.source_row_id
)) OR (NEW.evidence_holding_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM holdings h
    WHERE h.id = NEW.evidence_holding_id AND h.school_id = NEW.school_id
))
BEGIN SELECT RAISE(ABORT, 'comparison row scope mismatch'); END;

CREATE TRIGGER job_file_results_scope_insert
BEFORE INSERT ON job_file_results
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1 FROM durable_jobs j JOIN source_documents sd
      ON sd.id = NEW.source_document_id
    WHERE j.id = NEW.job_id AND j.school_id = sd.school_id
      AND j.job_type = 'COMPARE' AND j.status = 'RUNNING'
      AND j.claim_token = NEW.claim_token
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
)
BEGIN SELECT RAISE(ABORT, 'job file result scope or claim mismatch'); END;

CREATE TRIGGER catalog_delta_batches_scope_insert
BEFORE INSERT ON catalog_delta_batches
FOR EACH ROW WHEN
    NEW.registration_source_document_id IS NOT NULL AND (
        NEW.update_source_document_id IS NULL
        OR NEW.registration_source_document_id = NEW.update_source_document_id
        OR NOT EXISTS (
            SELECT 1 FROM source_documents r, source_documents u
            WHERE r.id = NEW.registration_source_document_id
              AND u.id = NEW.update_source_document_id
              AND r.school_id = NEW.school_id AND u.school_id = NEW.school_id
              AND r.source_file_id <> u.source_file_id
              AND r.role = 'CATALOG_DELTA_REGISTRATION'
              AND u.role = 'CATALOG_DELTA_UPDATE'
        )
    )
BEGIN SELECT RAISE(ABORT, 'catalog delta source scope mismatch'); END;
CREATE TRIGGER catalog_delta_batches_scope_update
BEFORE UPDATE OF school_id, registration_source_document_id,
                 update_source_document_id ON catalog_delta_batches
FOR EACH ROW WHEN
    NEW.registration_source_document_id IS NOT NULL AND (
        NEW.update_source_document_id IS NULL
        OR NEW.registration_source_document_id = NEW.update_source_document_id
        OR NOT EXISTS (
            SELECT 1 FROM source_documents r, source_documents u
            WHERE r.id = NEW.registration_source_document_id
              AND u.id = NEW.update_source_document_id
              AND r.school_id = NEW.school_id AND u.school_id = NEW.school_id
              AND r.source_file_id <> u.source_file_id
              AND r.role = 'CATALOG_DELTA_REGISTRATION'
              AND u.role = 'CATALOG_DELTA_UPDATE'
        )
    )
BEGIN SELECT RAISE(ABORT, 'catalog delta source scope mismatch'); END;

CREATE TRIGGER catalog_delta_row_results_scope_insert
BEFORE INSERT ON catalog_delta_row_results
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1 FROM catalog_delta_batches b
    JOIN source_documents sd ON sd.id = NEW.source_document_id
    JOIN source_rows sr ON sr.id = NEW.source_row_id
    WHERE b.id = NEW.batch_id AND b.school_id = NEW.school_id
      AND sd.school_id = NEW.school_id
      AND sr.source_document_id = NEW.source_document_id
      AND (
          (NEW.role = 'REGISTRATION' AND b.registration_source_document_id = sd.id)
          OR (NEW.role = 'UPDATE' AND b.update_source_document_id = sd.id)
      )
)
BEGIN SELECT RAISE(ABORT, 'catalog delta row scope mismatch'); END;
CREATE TRIGGER catalog_delta_row_results_scope_update
BEFORE UPDATE ON catalog_delta_row_results
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1 FROM catalog_delta_batches b
    JOIN source_documents sd ON sd.id = NEW.source_document_id
    JOIN source_rows sr ON sr.id = NEW.source_row_id
    WHERE b.id = NEW.batch_id AND b.school_id = NEW.school_id
      AND sd.school_id = NEW.school_id
      AND sr.source_document_id = NEW.source_document_id
      AND (
          (NEW.role = 'REGISTRATION' AND b.registration_source_document_id = sd.id)
          OR (NEW.role = 'UPDATE' AND b.update_source_document_id = sd.id)
      )
)
BEGIN SELECT RAISE(ABORT, 'catalog delta row scope mismatch'); END;

CREATE TRIGGER catalog_source_state_scope_insert
BEFORE INSERT ON catalog_source_state
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1 FROM catalog_versions cv
    WHERE cv.id = NEW.active_version_id AND cv.school_id = NEW.school_id
      AND cv.source_type = NEW.source_type
)
BEGIN SELECT RAISE(ABORT, 'catalog source state scope mismatch'); END;
CREATE TRIGGER catalog_source_state_scope_update
BEFORE UPDATE ON catalog_source_state
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1 FROM catalog_versions cv
    WHERE cv.id = NEW.active_version_id AND cv.school_id = NEW.school_id
      AND cv.source_type = NEW.source_type
)
BEGIN SELECT RAISE(ABORT, 'catalog source state scope mismatch'); END;

-- Once a document participates in a catalog version or delta batch, its file,
-- parser metadata, and logical rows are sealed as provenance.
CREATE TRIGGER immutable_catalog_source_document_update
BEFORE UPDATE ON source_documents
FOR EACH ROW WHEN EXISTS (
    SELECT 1 FROM catalog_versions WHERE source_document_id = OLD.id
    UNION ALL
    SELECT 1 FROM catalog_delta_batches
    WHERE registration_source_document_id = OLD.id OR update_source_document_id = OLD.id
)
BEGIN SELECT RAISE(ABORT, 'catalog source document is immutable'); END;
CREATE TRIGGER immutable_catalog_source_document_delete
BEFORE DELETE ON source_documents
FOR EACH ROW WHEN EXISTS (
    SELECT 1 FROM catalog_versions WHERE source_document_id = OLD.id
    UNION ALL
    SELECT 1 FROM catalog_delta_batches
    WHERE registration_source_document_id = OLD.id OR update_source_document_id = OLD.id
)
BEGIN SELECT RAISE(ABORT, 'catalog source document is immutable'); END;
CREATE TRIGGER immutable_catalog_source_rows_insert
BEFORE INSERT ON source_rows
FOR EACH ROW WHEN EXISTS (
    SELECT 1 FROM catalog_versions WHERE source_document_id = NEW.source_document_id
    UNION ALL
    SELECT 1 FROM catalog_delta_batches
    WHERE registration_source_document_id = NEW.source_document_id
       OR update_source_document_id = NEW.source_document_id
)
BEGIN SELECT RAISE(ABORT, 'catalog source rows are immutable'); END;
CREATE TRIGGER immutable_catalog_source_rows_update
BEFORE UPDATE ON source_rows
FOR EACH ROW WHEN EXISTS (
    SELECT 1 FROM catalog_versions WHERE source_document_id = OLD.source_document_id
    UNION ALL
    SELECT 1 FROM catalog_delta_batches
    WHERE registration_source_document_id = OLD.source_document_id
       OR update_source_document_id = OLD.source_document_id
)
BEGIN SELECT RAISE(ABORT, 'catalog source rows are immutable'); END;
CREATE TRIGGER immutable_catalog_source_rows_delete
BEFORE DELETE ON source_rows
FOR EACH ROW WHEN EXISTS (
    SELECT 1 FROM catalog_versions WHERE source_document_id = OLD.source_document_id
    UNION ALL
    SELECT 1 FROM catalog_delta_batches
    WHERE registration_source_document_id = OLD.source_document_id
       OR update_source_document_id = OLD.source_document_id
)
BEGIN SELECT RAISE(ABORT, 'catalog source rows are immutable'); END;
CREATE TRIGGER immutable_catalog_source_file_update
BEFORE UPDATE ON source_files
FOR EACH ROW WHEN EXISTS (
    SELECT 1 FROM source_documents sd
    WHERE sd.source_file_id = OLD.id AND (
        EXISTS (SELECT 1 FROM catalog_versions WHERE source_document_id = sd.id)
        OR EXISTS (
            SELECT 1 FROM catalog_delta_batches
            WHERE registration_source_document_id = sd.id OR update_source_document_id = sd.id
        )
    )
)
BEGIN SELECT RAISE(ABORT, 'catalog source file is immutable'); END;
CREATE TRIGGER immutable_catalog_source_file_delete
BEFORE DELETE ON source_files
FOR EACH ROW WHEN EXISTS (
    SELECT 1 FROM source_documents sd
    WHERE sd.source_file_id = OLD.id AND (
        EXISTS (SELECT 1 FROM catalog_versions WHERE source_document_id = sd.id)
        OR EXISTS (
            SELECT 1 FROM catalog_delta_batches
            WHERE registration_source_document_id = sd.id OR update_source_document_id = sd.id
        )
    )
)
BEGIN SELECT RAISE(ABORT, 'catalog source file is immutable'); END;
