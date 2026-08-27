CREATE TABLE schools (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    single_operator_mode INTEGER NOT NULL DEFAULT 0 CHECK (single_operator_mode IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE users (
    id TEXT PRIMARY KEY,
    school_id TEXT NOT NULL REFERENCES schools(id),
    username TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    display_name TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (school_id, username)
);
CREATE INDEX idx_users_school_id ON users(school_id);

CREATE TABLE user_roles (
    school_id TEXT NOT NULL REFERENCES schools(id),
    user_id TEXT NOT NULL REFERENCES users(id),
    role TEXT NOT NULL CHECK (role IN ('OPERATOR', 'REVIEWER')),
    created_at TEXT NOT NULL,
    PRIMARY KEY (school_id, user_id, role)
);
CREATE INDEX idx_user_roles_user_id ON user_roles(user_id);

CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    school_id TEXT NOT NULL REFERENCES schools(id),
    user_id TEXT NOT NULL REFERENCES users(id),
    token_digest TEXT NOT NULL UNIQUE,
    csrf_token_digest TEXT,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_sessions_user_id ON sessions(user_id);
CREATE INDEX idx_sessions_expires_at ON sessions(expires_at);

CREATE TABLE acquisition_workspaces (
    id TEXT PRIMARY KEY,
    school_id TEXT NOT NULL REFERENCES schools(id),
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    row_version INTEGER NOT NULL DEFAULT 1,
    created_by_user_id TEXT REFERENCES users(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX idx_acquisition_workspaces_school_status
    ON acquisition_workspaces(school_id, status);

CREATE TABLE idempotency_keys (
    id TEXT PRIMARY KEY,
    school_id TEXT NOT NULL REFERENCES schools(id),
    actor_id TEXT NOT NULL REFERENCES users(id),
    route TEXT NOT NULL,
    key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    response_status INTEGER,
    response_body TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (school_id, actor_id, route, key)
);
CREATE INDEX idx_idempotency_keys_created_at ON idempotency_keys(created_at);

CREATE TABLE audit_events (
    id TEXT PRIMARY KEY,
    school_id TEXT REFERENCES schools(id),
    actor_id TEXT REFERENCES users(id),
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT,
    before_json TEXT,
    after_json TEXT,
    request_id TEXT,
    occurred_at TEXT NOT NULL
);
CREATE INDEX idx_audit_events_school_occurred_at
    ON audit_events(school_id, occurred_at DESC);
CREATE INDEX idx_audit_events_entity ON audit_events(entity_type, entity_id);

CREATE TABLE durable_jobs (
    id TEXT PRIMARY KEY,
    school_id TEXT NOT NULL REFERENCES schools(id),
    workspace_id TEXT REFERENCES acquisition_workspaces(id),
    job_type TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    progress_current INTEGER NOT NULL DEFAULT 0,
    progress_total INTEGER,
    cancel_requested_at TEXT,
    heartbeat_at TEXT,
    error_json TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX idx_durable_jobs_school_status ON durable_jobs(school_id, status);
CREATE INDEX idx_durable_jobs_workspace_id ON durable_jobs(workspace_id);
