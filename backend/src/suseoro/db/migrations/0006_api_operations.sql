-- Durable API event replay and workspace/source presentation state.
CREATE TABLE api_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    school_id TEXT NOT NULL REFERENCES schools(id),
    workspace_id TEXT REFERENCES acquisition_workspaces(id),
    event_type TEXT NOT NULL,
    deduplication_key TEXT,
    data_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_api_events_scope_replay
    ON api_events(school_id, workspace_id, id);
CREATE UNIQUE INDEX idx_api_events_idempotent_side_effect
    ON api_events(school_id, event_type, deduplication_key)
    WHERE deduplication_key IS NOT NULL;

CREATE TABLE workspace_sources (
    workspace_id TEXT NOT NULL REFERENCES acquisition_workspaces(id),
    source_document_id TEXT NOT NULL REFERENCES source_documents(id),
    school_id TEXT NOT NULL REFERENCES schools(id),
    created_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, source_document_id)
);
CREATE INDEX idx_workspace_sources_listing
    ON workspace_sources(school_id, workspace_id, created_at DESC, source_document_id DESC);

CREATE TABLE source_configurations (
    source_document_id TEXT PRIMARY KEY REFERENCES source_documents(id),
    school_id TEXT NOT NULL REFERENCES schools(id),
    role TEXT NOT NULL,
    mapping_json TEXT NOT NULL DEFAULT '{}',
    row_version INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);

CREATE TRIGGER workspace_source_scope_insert
BEFORE INSERT ON workspace_sources
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1 FROM acquisition_workspaces workspace
    JOIN source_documents source ON source.id = NEW.source_document_id
    WHERE workspace.id = NEW.workspace_id
      AND workspace.school_id = NEW.school_id
      AND source.school_id = NEW.school_id
)
BEGIN SELECT RAISE(ABORT, 'workspace source scope mismatch'); END;

CREATE TRIGGER api_event_scope_insert
BEFORE INSERT ON api_events
FOR EACH ROW WHEN NEW.workspace_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM acquisition_workspaces
    WHERE id = NEW.workspace_id AND school_id = NEW.school_id
)
BEGIN SELECT RAISE(ABORT, 'api event scope mismatch'); END;
