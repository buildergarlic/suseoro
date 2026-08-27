CREATE TABLE source_files (
    id TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL UNIQUE CHECK (
        length(sha256) = 64
        AND sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    storage_path TEXT NOT NULL,
    original_filename TEXT,
    detected_format TEXT NOT NULL CHECK (
        detected_format IN ('CSV', 'TSV', 'TXT', 'XLS', 'XLSX', 'XLSB', 'ODS', 'UNKNOWN')
    ),
    created_at TEXT NOT NULL
);

CREATE TABLE source_documents (
    id TEXT PRIMARY KEY,
    source_file_id TEXT NOT NULL REFERENCES source_files(id),
    school_id TEXT REFERENCES schools(id),
    role TEXT NOT NULL CHECK (
        role IN ('UNKNOWN', 'PURCHASE_REQUEST', 'VENDOR_QUOTE', 'INVENTORY')
    ),
    parser_version TEXT NOT NULL,
    template_version TEXT,
    status TEXT NOT NULL CHECK (status IN ('PENDING', 'SUCCESS', 'ROW_ERROR', 'FAILED')),
    detected_format TEXT NOT NULL CHECK (
        detected_format IN ('CSV', 'TSV', 'TXT', 'XLS', 'XLSX', 'XLSB', 'ODS', 'UNKNOWN')
    ),
    created_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE INDEX idx_source_documents_file_role
    ON source_documents(source_file_id, role, parser_version);
CREATE INDEX idx_source_documents_school_created
    ON source_documents(school_id, created_at DESC);

CREATE TABLE source_rows (
    id TEXT PRIMARY KEY,
    source_document_id TEXT NOT NULL REFERENCES source_documents(id) ON DELETE CASCADE,
    sheet_name TEXT,
    source_row INTEGER NOT NULL CHECK (source_row >= 0),
    status TEXT NOT NULL CHECK (status IN ('SUCCESS', 'ROW_ERROR')),
    raw_json TEXT NOT NULL,
    fields_json TEXT NOT NULL,
    warnings_json TEXT NOT NULL DEFAULT '[]',
    error_code TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_source_rows_provenance
    ON source_rows(source_document_id, sheet_name, source_row);
CREATE INDEX idx_source_rows_status
    ON source_rows(source_document_id, status);

CREATE TABLE parser_runs (
    id TEXT PRIMARY KEY,
    source_file_sha256 TEXT NOT NULL CHECK (
        length(source_file_sha256) = 64
        AND source_file_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    parser_version TEXT NOT NULL,
    role TEXT NOT NULL CHECK (
        role IN ('UNKNOWN', 'PURCHASE_REQUEST', 'VENDOR_QUOTE', 'INVENTORY')
    ),
    status TEXT NOT NULL CHECK (status IN ('PENDING', 'SUCCESS', 'FAILED')),
    result_json TEXT,
    error_json TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE (source_file_sha256, parser_version, role)
);
CREATE UNIQUE INDEX idx_parser_runs_cache
    ON parser_runs(source_file_sha256, parser_version, role);

CREATE TABLE mapping_templates (
    id TEXT PRIMARY KEY,
    school_id TEXT NOT NULL REFERENCES schools(id),
    vendor_scope TEXT NOT NULL DEFAULT '*',
    role TEXT NOT NULL CHECK (
        role IN ('UNKNOWN', 'PURCHASE_REQUEST', 'VENDOR_QUOTE', 'INVENTORY')
    ),
    signature TEXT NOT NULL CHECK (
        length(signature) = 64
        AND signature NOT GLOB '*[^0-9a-f]*'
    ),
    normalized_headers_json TEXT NOT NULL,
    mapping_json TEXT NOT NULL,
    required_fields_json TEXT NOT NULL,
    template_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (school_id, vendor_scope, role, signature)
);
CREATE INDEX idx_mapping_templates_scope
    ON mapping_templates(school_id, vendor_scope, role);

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

CREATE TRIGGER validate_source_rows_insert
BEFORE INSERT ON source_rows
FOR EACH ROW WHEN
    NOT (NEW.id GLOB '????????-????-????-????-????????????' AND NEW.id NOT GLOB '*[^0-9A-Fa-f-]*')
    OR NOT (NEW.source_document_id GLOB '????????-????-????-????-????????????' AND NEW.source_document_id NOT GLOB '*[^0-9A-Fa-f-]*')
    OR NOT ((NEW.created_at GLOB '????-??-??T??:??:??Z' OR NEW.created_at GLOB '????-??-??T??:??:??.[0-9]*Z') AND NEW.created_at NOT GLOB '*[^0-9T:.Z-]*' AND strftime('%Y-%m-%dT%H:%M:%S', NEW.created_at) = substr(NEW.created_at, 1, 19))
BEGIN SELECT RAISE(ABORT, 'invalid source_rows identifier or UTC timestamp'); END;

CREATE TRIGGER validate_source_rows_update
BEFORE UPDATE ON source_rows
FOR EACH ROW WHEN
    NOT (NEW.id GLOB '????????-????-????-????-????????????' AND NEW.id NOT GLOB '*[^0-9A-Fa-f-]*')
    OR NOT (NEW.source_document_id GLOB '????????-????-????-????-????????????' AND NEW.source_document_id NOT GLOB '*[^0-9A-Fa-f-]*')
    OR NOT ((NEW.created_at GLOB '????-??-??T??:??:??Z' OR NEW.created_at GLOB '????-??-??T??:??:??.[0-9]*Z') AND NEW.created_at NOT GLOB '*[^0-9T:.Z-]*' AND strftime('%Y-%m-%dT%H:%M:%S', NEW.created_at) = substr(NEW.created_at, 1, 19))
BEGIN SELECT RAISE(ABORT, 'invalid source_rows identifier or UTC timestamp'); END;

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

CREATE TRIGGER validate_mapping_templates_insert
BEFORE INSERT ON mapping_templates
FOR EACH ROW WHEN
    NOT (NEW.id GLOB '????????-????-????-????-????????????' AND NEW.id NOT GLOB '*[^0-9A-Fa-f-]*')
    OR NOT (NEW.school_id GLOB '????????-????-????-????-????????????' AND NEW.school_id NOT GLOB '*[^0-9A-Fa-f-]*')
    OR NOT ((NEW.created_at GLOB '????-??-??T??:??:??Z' OR NEW.created_at GLOB '????-??-??T??:??:??.[0-9]*Z') AND NEW.created_at NOT GLOB '*[^0-9T:.Z-]*' AND strftime('%Y-%m-%dT%H:%M:%S', NEW.created_at) = substr(NEW.created_at, 1, 19))
    OR NOT ((NEW.updated_at GLOB '????-??-??T??:??:??Z' OR NEW.updated_at GLOB '????-??-??T??:??:??.[0-9]*Z') AND NEW.updated_at NOT GLOB '*[^0-9T:.Z-]*' AND strftime('%Y-%m-%dT%H:%M:%S', NEW.updated_at) = substr(NEW.updated_at, 1, 19))
BEGIN SELECT RAISE(ABORT, 'invalid mapping_templates identifier or UTC timestamp'); END;

CREATE TRIGGER validate_mapping_templates_update
BEFORE UPDATE ON mapping_templates
FOR EACH ROW WHEN
    NOT (NEW.id GLOB '????????-????-????-????-????????????' AND NEW.id NOT GLOB '*[^0-9A-Fa-f-]*')
    OR NOT (NEW.school_id GLOB '????????-????-????-????-????????????' AND NEW.school_id NOT GLOB '*[^0-9A-Fa-f-]*')
    OR NOT ((NEW.created_at GLOB '????-??-??T??:??:??Z' OR NEW.created_at GLOB '????-??-??T??:??:??.[0-9]*Z') AND NEW.created_at NOT GLOB '*[^0-9T:.Z-]*' AND strftime('%Y-%m-%dT%H:%M:%S', NEW.created_at) = substr(NEW.created_at, 1, 19))
    OR NOT ((NEW.updated_at GLOB '????-??-??T??:??:??Z' OR NEW.updated_at GLOB '????-??-??T??:??:??.[0-9]*Z') AND NEW.updated_at NOT GLOB '*[^0-9T:.Z-]*' AND strftime('%Y-%m-%dT%H:%M:%S', NEW.updated_at) = substr(NEW.updated_at, 1, 19))
BEGIN SELECT RAISE(ABORT, 'invalid mapping_templates identifier or UTC timestamp'); END;
