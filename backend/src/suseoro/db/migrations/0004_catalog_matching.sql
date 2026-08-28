ALTER TABLE durable_jobs ADD COLUMN stage TEXT NOT NULL DEFAULT 'QUEUED';

CREATE TABLE catalog_versions (
    id TEXT PRIMARY KEY,
    school_id TEXT NOT NULL REFERENCES schools(id),
    source_type TEXT NOT NULL CHECK (
        source_type IN ('DLS_MARC', 'DLS_EXCEL', 'DLS_API')
    ),
    import_mode TEXT NOT NULL CHECK (import_mode IN ('FULL_SNAPSHOT', 'DELTA')),
    status TEXT NOT NULL CHECK (
        status IN ('STAGING', 'ACTIVE', 'SUPERSEDED', 'FAILED')
    ),
    source_document_id TEXT REFERENCES source_documents(id),
    parent_version_id TEXT REFERENCES catalog_versions(id),
    item_count INTEGER NOT NULL DEFAULT 0 CHECK (item_count >= 0),
    as_of_local_date TEXT,
    anomaly_confirmed INTEGER NOT NULL DEFAULT 0
        CHECK (anomaly_confirmed IN (0, 1)),
    created_at TEXT NOT NULL,
    activated_at TEXT
);
CREATE UNIQUE INDEX idx_catalog_versions_one_active_school
    ON catalog_versions(school_id) WHERE status = 'ACTIVE';
CREATE INDEX idx_catalog_versions_school_created
    ON catalog_versions(school_id, created_at DESC);

CREATE TABLE catalog_source_state (
    school_id TEXT PRIMARY KEY REFERENCES schools(id),
    source_type TEXT NOT NULL CHECK (
        source_type IN ('DLS_MARC', 'DLS_EXCEL', 'DLS_API')
    ),
    active_version_id TEXT NOT NULL REFERENCES catalog_versions(id),
    watermark_local_date TEXT,
    last_full_snapshot_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE holdings (
    id TEXT PRIMARY KEY,
    stable_id TEXT NOT NULL,
    catalog_version_id TEXT NOT NULL REFERENCES catalog_versions(id),
    school_id TEXT NOT NULL REFERENCES schools(id),
    source_item_id TEXT NOT NULL,
    source_row_id TEXT REFERENCES source_rows(id),
    isbn_input TEXT,
    isbn13 TEXT,
    original_title TEXT NOT NULL,
    original_subtitle TEXT,
    original_authors_json TEXT NOT NULL,
    original_publisher TEXT,
    original_volume TEXT,
    original_edition TEXT,
    original_series TEXT,
    publication_date TEXT,
    price INTEGER,
    pages INTEGER,
    kdc TEXT,
    registration_number TEXT,
    call_number TEXT,
    location TEXT,
    holding_status TEXT NOT NULL CHECK (
        holding_status IN ('AVAILABLE', 'WITHDRAWN', 'MISSING', 'UNCERTAIN')
    ),
    raw_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (catalog_version_id, source_item_id)
);
CREATE INDEX idx_holdings_school_isbn
    ON holdings(school_id, isbn13, catalog_version_id, holding_status);
CREATE INDEX idx_holdings_version_source_item
    ON holdings(catalog_version_id, source_item_id);
CREATE INDEX idx_holdings_school_stable
    ON holdings(school_id, stable_id, catalog_version_id);

CREATE TABLE normalized_works (
    id TEXT PRIMARY KEY,
    holding_id TEXT NOT NULL UNIQUE REFERENCES holdings(id) ON DELETE CASCADE,
    catalog_version_id TEXT NOT NULL REFERENCES catalog_versions(id),
    school_id TEXT NOT NULL REFERENCES schools(id),
    title_key TEXT NOT NULL,
    subtitle_key TEXT NOT NULL,
    author_key TEXT NOT NULL,
    publisher_key TEXT NOT NULL,
    volume_key TEXT NOT NULL,
    edition_key TEXT NOT NULL,
    series_key TEXT NOT NULL,
    search_text TEXT NOT NULL
);
CREATE INDEX idx_normalized_works_exact_title_author
    ON normalized_works(
        school_id, title_key, author_key, catalog_version_id
    );
CREATE INDEX idx_normalized_works_volume_edition
    ON normalized_works(
        school_id, volume_key, edition_key, catalog_version_id
    );

CREATE VIRTUAL TABLE holding_search_fts USING fts5(
    holding_id UNINDEXED,
    school_id UNINDEXED,
    catalog_version_id UNINDEXED,
    search_text,
    tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TABLE recommendations (
    id TEXT PRIMARY KEY,
    school_id TEXT NOT NULL REFERENCES schools(id),
    workspace_id TEXT NOT NULL REFERENCES acquisition_workspaces(id),
    source_document_id TEXT NOT NULL REFERENCES source_documents(id),
    source_row_id TEXT NOT NULL REFERENCES source_rows(id),
    isbn_input TEXT,
    isbn13 TEXT,
    original_title TEXT NOT NULL,
    original_subtitle TEXT,
    original_authors_json TEXT NOT NULL,
    original_publisher TEXT,
    original_volume TEXT,
    original_edition TEXT,
    original_series TEXT,
    original_json TEXT NOT NULL,
    title_key TEXT NOT NULL,
    subtitle_key TEXT NOT NULL,
    author_key TEXT NOT NULL,
    publisher_key TEXT NOT NULL,
    volume_key TEXT NOT NULL,
    edition_key TEXT NOT NULL,
    series_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (workspace_id, source_row_id)
);
CREATE INDEX idx_recommendations_workspace
    ON recommendations(workspace_id, created_at);
CREATE INDEX idx_recommendations_school_isbn
    ON recommendations(school_id, isbn13);

CREATE TABLE candidate_decisions (
    id TEXT PRIMARY KEY,
    school_id TEXT NOT NULL REFERENCES schools(id),
    workspace_id TEXT NOT NULL REFERENCES acquisition_workspaces(id),
    recommendation_id TEXT NOT NULL UNIQUE REFERENCES recommendations(id),
    outcome TEXT NOT NULL CHECK (
        outcome IN ('CANDIDATE', 'NEEDS_REVIEW', 'EXCLUDED')
    ),
    reason TEXT NOT NULL,
    evidence_holding_id TEXT REFERENCES holdings(id),
    score REAL,
    row_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX idx_candidate_decisions_workspace_outcome
    ON candidate_decisions(workspace_id, outcome);
CREATE INDEX idx_candidate_decisions_evidence
    ON candidate_decisions(evidence_holding_id);

CREATE TABLE comparison_file_results (
    id TEXT PRIMARY KEY,
    school_id TEXT NOT NULL REFERENCES schools(id),
    workspace_id TEXT NOT NULL REFERENCES acquisition_workspaces(id),
    source_document_id TEXT NOT NULL REFERENCES source_documents(id),
    status TEXT NOT NULL CHECK (status IN ('SUCCESS', 'PARTIAL', 'FAILED')),
    total_rows INTEGER NOT NULL CHECK (total_rows >= 0),
    candidate_count INTEGER NOT NULL DEFAULT 0 CHECK (candidate_count >= 0),
    review_count INTEGER NOT NULL DEFAULT 0 CHECK (review_count >= 0),
    excluded_count INTEGER NOT NULL DEFAULT 0 CHECK (excluded_count >= 0),
    row_error_count INTEGER NOT NULL DEFAULT 0 CHECK (row_error_count >= 0),
    error_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (workspace_id, source_document_id)
);
CREATE INDEX idx_comparison_file_results_workspace
    ON comparison_file_results(workspace_id, status);

CREATE TABLE comparison_row_results (
    id TEXT PRIMARY KEY,
    school_id TEXT NOT NULL REFERENCES schools(id),
    workspace_id TEXT NOT NULL REFERENCES acquisition_workspaces(id),
    source_document_id TEXT NOT NULL REFERENCES source_documents(id),
    source_row_id TEXT NOT NULL REFERENCES source_rows(id),
    recommendation_id TEXT REFERENCES recommendations(id),
    outcome TEXT NOT NULL CHECK (
        outcome IN ('CANDIDATE', 'NEEDS_REVIEW', 'EXCLUDED', 'ROW_ERROR')
    ),
    reason TEXT NOT NULL,
    evidence_holding_id TEXT REFERENCES holdings(id),
    score REAL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (workspace_id, source_row_id)
);
CREATE INDEX idx_comparison_row_results_workspace_outcome
    ON comparison_row_results(workspace_id, outcome);

CREATE TABLE catalog_delta_batches (
    id TEXT PRIMARY KEY,
    school_id TEXT NOT NULL REFERENCES schools(id),
    batch_key TEXT NOT NULL,
    registration_sha256 TEXT NOT NULL,
    registration_parser_version TEXT NOT NULL,
    registration_status TEXT NOT NULL CHECK (
        registration_status IN ('PENDING', 'SUCCESS', 'FAILED')
    ),
    update_sha256 TEXT NOT NULL,
    update_parser_version TEXT NOT NULL,
    update_status TEXT NOT NULL CHECK (
        update_status IN ('PENDING', 'SUCCESS', 'FAILED')
    ),
    through_local_date TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('PENDING', 'PARTIAL_FAILURE', 'SUCCESS', 'FAILED')
    ),
    catalog_version_id TEXT REFERENCES catalog_versions(id),
    error_json TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE (school_id, batch_key)
);
CREATE INDEX idx_catalog_delta_batches_school_date
    ON catalog_delta_batches(school_id, through_local_date DESC);

CREATE TABLE job_file_results (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES durable_jobs(id) ON DELETE CASCADE,
    source_document_id TEXT NOT NULL REFERENCES source_documents(id),
    status TEXT NOT NULL CHECK (status IN ('PENDING', 'SUCCESS', 'PARTIAL', 'FAILED')),
    total_rows INTEGER NOT NULL DEFAULT 0 CHECK (total_rows >= 0),
    processed_rows INTEGER NOT NULL DEFAULT 0 CHECK (
        processed_rows >= 0 AND processed_rows <= total_rows
    ),
    error_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (job_id, source_document_id)
);
CREATE INDEX idx_job_file_results_job_status
    ON job_file_results(job_id, status);

CREATE TRIGGER immutable_active_holdings_update
BEFORE UPDATE ON holdings
FOR EACH ROW WHEN EXISTS (
    SELECT 1 FROM catalog_versions
    WHERE id = OLD.catalog_version_id AND status <> 'STAGING'
)
BEGIN SELECT RAISE(ABORT, 'active catalog holdings are immutable'); END;

CREATE TRIGGER immutable_active_holdings_delete
BEFORE DELETE ON holdings
FOR EACH ROW WHEN EXISTS (
    SELECT 1 FROM catalog_versions
    WHERE id = OLD.catalog_version_id AND status <> 'STAGING'
)
BEGIN SELECT RAISE(ABORT, 'active catalog holdings are immutable'); END;

CREATE TRIGGER immutable_active_normalized_works_insert
BEFORE INSERT ON normalized_works
FOR EACH ROW WHEN EXISTS (
    SELECT 1 FROM catalog_versions
    WHERE id = NEW.catalog_version_id AND status <> 'STAGING'
)
BEGIN SELECT RAISE(ABORT, 'active normalized works are immutable'); END;

CREATE TRIGGER immutable_active_normalized_works_update
BEFORE UPDATE ON normalized_works
FOR EACH ROW WHEN EXISTS (
    SELECT 1 FROM catalog_versions
    WHERE id = OLD.catalog_version_id AND status <> 'STAGING'
)
BEGIN SELECT RAISE(ABORT, 'active normalized works are immutable'); END;

CREATE TRIGGER immutable_active_normalized_works_delete
BEFORE DELETE ON normalized_works
FOR EACH ROW WHEN EXISTS (
    SELECT 1 FROM catalog_versions
    WHERE id = OLD.catalog_version_id AND status <> 'STAGING'
)
BEGIN SELECT RAISE(ABORT, 'active normalized works are immutable'); END;

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
)
BEGIN SELECT RAISE(ABORT, 'activated catalog versions are immutable'); END;

CREATE TRIGGER immutable_catalog_version_delete
BEFORE DELETE ON catalog_versions
FOR EACH ROW WHEN OLD.status IN ('ACTIVE', 'SUPERSEDED')
BEGIN SELECT RAISE(ABORT, 'activated catalog versions are immutable'); END;
