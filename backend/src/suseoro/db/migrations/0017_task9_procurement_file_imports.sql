-- QUOTE/DELIVERY is the durable procurement document role. Both documents
-- intentionally reuse VENDOR_QUOTE in the common parser because their
-- canonical row fields and mapping behavior are identical.
CREATE TABLE procurement_source_imports (
    id TEXT PRIMARY KEY,
    school_id TEXT NOT NULL REFERENCES schools(id),
    workspace_id TEXT NOT NULL REFERENCES acquisition_workspaces(id),
    source_document_id TEXT NOT NULL UNIQUE REFERENCES source_documents(id),
    kind TEXT NOT NULL CHECK (kind IN ('QUOTE', 'DELIVERY')),
    target_revision_id TEXT NOT NULL,
    vendor_name TEXT NOT NULL CHECK (length(trim(vendor_name)) > 0),
    reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    actor_id TEXT NOT NULL REFERENCES users(id),
    status TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'IMPORTED')),
    result_id TEXT,
    response_json TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    CHECK (
        (status = 'PENDING' AND result_id IS NULL AND response_json IS NULL
            AND completed_at IS NULL)
        OR
        (status = 'IMPORTED' AND result_id IS NOT NULL AND response_json IS NOT NULL
            AND completed_at IS NOT NULL AND json_valid(response_json))
    )
);

CREATE INDEX idx_procurement_source_imports_workspace
    ON procurement_source_imports(school_id, workspace_id, created_at DESC, id DESC);

CREATE TABLE procurement_source_import_rows (
    import_id TEXT NOT NULL REFERENCES procurement_source_imports(id) ON DELETE RESTRICT,
    source_row_id TEXT NOT NULL UNIQUE REFERENCES source_rows(id) ON DELETE RESTRICT,
    result_row_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (import_id, source_row_id)
);

CREATE TRIGGER procurement_source_import_scope_insert
BEFORE INSERT ON procurement_source_imports
FOR EACH ROW WHEN
    NOT EXISTS (
        SELECT 1
        FROM source_documents AS document
        JOIN workspace_sources AS link
          ON link.source_document_id = document.id
         AND link.school_id = document.school_id
        JOIN acquisition_workspaces AS workspace
          ON workspace.id = link.workspace_id
         AND workspace.school_id = link.school_id
        JOIN users AS actor
          ON actor.id = NEW.actor_id AND actor.school_id = NEW.school_id
        WHERE document.id = NEW.source_document_id
          AND document.school_id = NEW.school_id
          AND document.role = 'VENDOR_QUOTE'
          AND link.workspace_id = NEW.workspace_id
          AND workspace.school_id = NEW.school_id
    )
    OR (
        NEW.kind = 'QUOTE'
        AND NOT EXISTS (
            SELECT 1 FROM approval_revisions
            WHERE id = NEW.target_revision_id
              AND school_id = NEW.school_id
              AND workspace_id = NEW.workspace_id
              AND sealed_at IS NOT NULL
        )
    )
    OR (
        NEW.kind = 'DELIVERY'
        AND NOT EXISTS (
            SELECT 1 FROM order_revisions
            WHERE id = NEW.target_revision_id
              AND school_id = NEW.school_id
              AND workspace_id = NEW.workspace_id
              AND sealed_at IS NOT NULL
        )
    )
BEGIN SELECT RAISE(ABORT, 'procurement source import scope mismatch'); END;

CREATE TRIGGER procurement_source_import_identity_update
BEFORE UPDATE ON procurement_source_imports
FOR EACH ROW WHEN
    NEW.id != OLD.id
    OR NEW.school_id != OLD.school_id
    OR NEW.workspace_id != OLD.workspace_id
    OR NEW.source_document_id != OLD.source_document_id
    OR NEW.kind != OLD.kind
    OR NEW.target_revision_id != OLD.target_revision_id
    OR NEW.vendor_name != OLD.vendor_name
    OR NEW.reason != OLD.reason
    OR NEW.actor_id != OLD.actor_id
    OR NEW.created_at != OLD.created_at
    OR OLD.status = 'IMPORTED'
    OR NOT (
        OLD.status = 'PENDING'
        AND NEW.status = 'IMPORTED'
        AND NEW.result_id IS NOT NULL
        AND NEW.response_json IS NOT NULL
        AND json_valid(NEW.response_json)
        AND NEW.completed_at IS NOT NULL
    )
BEGIN SELECT RAISE(ABORT, 'procurement source import is immutable'); END;

CREATE TRIGGER procurement_source_import_delete
BEFORE DELETE ON procurement_source_imports
FOR EACH ROW
BEGIN SELECT RAISE(ABORT, 'procurement source import is immutable'); END;

CREATE TRIGGER procurement_source_import_row_scope_insert
BEFORE INSERT ON procurement_source_import_rows
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1
    FROM procurement_source_imports AS imported
    JOIN source_rows AS source_row
      ON source_row.source_document_id = imported.source_document_id
    WHERE imported.id = NEW.import_id
      AND imported.status = 'IMPORTED'
      AND source_row.id = NEW.source_row_id
      AND source_row.status = 'SUCCESS'
)
BEGIN SELECT RAISE(ABORT, 'procurement import row scope mismatch'); END;

CREATE TRIGGER procurement_source_import_row_update
BEFORE UPDATE ON procurement_source_import_rows
FOR EACH ROW
BEGIN SELECT RAISE(ABORT, 'procurement import row is immutable'); END;

CREATE TRIGGER procurement_source_import_row_delete
BEFORE DELETE ON procurement_source_import_rows
FOR EACH ROW
BEGIN SELECT RAISE(ABORT, 'procurement import row is immutable'); END;
