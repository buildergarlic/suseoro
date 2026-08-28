CREATE TABLE upload_repair_obligations (
    id TEXT PRIMARY KEY,
    school_id TEXT NOT NULL REFERENCES schools(id),
    workspace_id TEXT NOT NULL REFERENCES acquisition_workspaces(id),
    upload_claim_id TEXT REFERENCES upload_idempotency_claims(id) ON DELETE RESTRICT,
    legacy_idempotency_id TEXT REFERENCES idempotency_keys(id) ON DELETE RESTRICT,
    actor_id TEXT NOT NULL REFERENCES users(id),
    file_index INTEGER NOT NULL CHECK (file_index >= 0),
    filename TEXT NOT NULL CHECK (length(filename) > 0),
    content_sha256 TEXT CHECK (
        content_sha256 IS NULL
        OR (
            length(content_sha256) = 64
            AND content_sha256 NOT GLOB '*[^0-9a-f]*'
        )
    ),
    size_bytes INTEGER CHECK (size_bytes IS NULL OR size_bytes >= 0),
    role TEXT NOT NULL CHECK (role IN (
        'UNKNOWN', 'PURCHASE_REQUEST', 'VENDOR_QUOTE', 'INVENTORY',
        'CATALOG_FULL', 'CATALOG_DELTA_REGISTRATION', 'CATALOG_DELTA_UPDATE'
    )),
    vendor_scope TEXT NOT NULL DEFAULT '*' CHECK (length(vendor_scope) > 0),
    requested_start_local_date TEXT,
    requested_through_local_date TEXT,
    error_code TEXT NOT NULL CHECK (length(error_code) > 0),
    status TEXT NOT NULL CHECK (status IN ('UNRESOLVED', 'REPAIRING', 'RESOLVED')),
    generation INTEGER NOT NULL DEFAULT 0 CHECK (generation >= 0),
    active_upload_claim_id TEXT REFERENCES upload_idempotency_claims(id),
    resolved_source_document_id TEXT REFERENCES source_documents(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (upload_claim_id, file_index),
    CHECK (
        (upload_claim_id IS NOT NULL AND legacy_idempotency_id IS NULL)
        OR (upload_claim_id IS NULL AND legacy_idempotency_id IS NOT NULL)
    ),
    CHECK (
        (status = 'UNRESOLVED' AND resolved_source_document_id IS NULL)
        OR (status = 'REPAIRING' AND active_upload_claim_id IS NOT NULL)
        OR (status = 'RESOLVED' AND resolved_source_document_id IS NOT NULL)
    )
);

INSERT INTO upload_repair_obligations (
    id, school_id, workspace_id, upload_claim_id, actor_id, file_index,
    filename, content_sha256, size_bytes, role, vendor_scope, error_code,
    status, created_at, updated_at
)
SELECT
    claim.id || ':repair:' || item.key,
    claim.school_id,
    replace(
        replace(claim.route, 'POST /api/v2/workspaces/', ''),
        '/sources',
        ''
    ),
    claim.id,
    claim.actor_id,
    CAST(item.key AS INTEGER),
    json_extract(item.value, '$.filename'),
    json_extract(
        claim.request_metadata_json,
        '$.files[' || item.key || '].sha256'
    ),
    json_extract(
        claim.request_metadata_json,
        '$.files[' || item.key || '].size_bytes'
    ),
    'UNKNOWN',
    '*',
    json_extract(item.value, '$.error.code'),
    'UNRESOLVED',
    claim.created_at,
    claim.updated_at
FROM upload_idempotency_claims AS claim,
     json_each(claim.response_body, '$.items') AS item
WHERE claim.state = 'COMPLETED'
  AND claim.route LIKE 'POST /api/v2/workspaces/%/sources'
  AND json_extract(item.value, '$.status') = 'FAILED'
  AND json_extract(item.value, '$.filename') IS NOT NULL
  AND json_extract(item.value, '$.error.code') IS NOT NULL
  AND EXISTS (
      SELECT 1
      FROM acquisition_workspaces AS workspace
      WHERE workspace.id = replace(
          replace(claim.route, 'POST /api/v2/workspaces/', ''),
          '/sources',
          ''
      )
        AND workspace.school_id = claim.school_id
  );

INSERT INTO upload_repair_obligations (
    id, school_id, workspace_id, legacy_idempotency_id, actor_id, file_index,
    filename, content_sha256, size_bytes, role, vendor_scope, error_code,
    status, created_at, updated_at
)
SELECT
    legacy.id || ':legacy-repair:' || item.key,
    legacy.school_id,
    replace(
        replace(legacy.route, 'POST /api/v2/workspaces/', ''),
        '/sources',
        ''
    ),
    legacy.id,
    legacy.actor_id,
    CAST(item.key AS INTEGER),
    json_extract(item.value, '$.filename'),
    NULL,
    NULL,
    'UNKNOWN',
    '*',
    json_extract(item.value, '$.error.code'),
    'UNRESOLVED',
    legacy.created_at,
    legacy.created_at
FROM idempotency_keys AS legacy,
     json_each(legacy.response_body, '$.items') AS item
WHERE legacy.response_status IS NOT NULL
  AND legacy.response_body IS NOT NULL
  AND legacy.route LIKE 'POST /api/v2/workspaces/%/sources'
  AND json_extract(item.value, '$.status') = 'FAILED'
  AND json_extract(item.value, '$.filename') IS NOT NULL
  AND json_extract(item.value, '$.error.code') IS NOT NULL
  AND NOT EXISTS (
      SELECT 1
      FROM upload_idempotency_claims AS claim
      WHERE claim.school_id = legacy.school_id
        AND claim.actor_id = legacy.actor_id
        AND claim.route = legacy.route
        AND claim.key = legacy.key
  )
  AND EXISTS (
      SELECT 1
      FROM acquisition_workspaces AS workspace
      WHERE workspace.id = replace(
          replace(legacy.route, 'POST /api/v2/workspaces/', ''),
          '/sources',
          ''
      )
        AND workspace.school_id = legacy.school_id
  );

CREATE INDEX idx_upload_repair_workspace_status
    ON upload_repair_obligations(school_id, workspace_id, status, created_at);

CREATE UNIQUE INDEX idx_upload_repair_legacy_item
    ON upload_repair_obligations(legacy_idempotency_id, file_index)
    WHERE legacy_idempotency_id IS NOT NULL;

CREATE TRIGGER upload_repair_scope_insert
BEFORE INSERT ON upload_repair_obligations
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1
    FROM acquisition_workspaces workspace
    JOIN users actor
      ON actor.id = NEW.actor_id AND actor.school_id = workspace.school_id
    WHERE workspace.id = NEW.workspace_id
      AND workspace.school_id = NEW.school_id
      AND (
          EXISTS (
              SELECT 1 FROM upload_idempotency_claims claim
              WHERE claim.id = NEW.upload_claim_id
                AND claim.school_id = workspace.school_id
          )
          OR EXISTS (
              SELECT 1 FROM idempotency_keys legacy
              WHERE legacy.id = NEW.legacy_idempotency_id
                AND legacy.school_id = workspace.school_id
                AND legacy.actor_id = NEW.actor_id
          )
      )
)
BEGIN SELECT RAISE(ABORT, 'upload repair scope mismatch'); END;

CREATE TRIGGER upload_repair_scope_update
BEFORE UPDATE ON upload_repair_obligations
FOR EACH ROW WHEN
    NEW.id != OLD.id
    OR NEW.school_id != OLD.school_id
    OR NEW.workspace_id != OLD.workspace_id
    OR NEW.upload_claim_id IS NOT OLD.upload_claim_id
    OR NEW.legacy_idempotency_id IS NOT OLD.legacy_idempotency_id
    OR NEW.actor_id != OLD.actor_id
    OR NEW.file_index != OLD.file_index
    OR NEW.filename != OLD.filename
    OR NEW.content_sha256 IS NOT OLD.content_sha256
    OR NEW.size_bytes IS NOT OLD.size_bytes
    OR NEW.role != OLD.role
    OR NEW.vendor_scope != OLD.vendor_scope
    OR NEW.requested_start_local_date IS NOT OLD.requested_start_local_date
    OR NEW.requested_through_local_date IS NOT OLD.requested_through_local_date
    OR NEW.error_code != OLD.error_code
    OR NEW.generation < OLD.generation
BEGIN SELECT RAISE(ABORT, 'upload repair identity is immutable'); END;
