ALTER TABLE candidate_decisions
    ADD COLUMN quantity INTEGER NOT NULL DEFAULT 1 CHECK (quantity >= 0);
ALTER TABLE candidate_decisions
    ADD COLUMN unit_price INTEGER NOT NULL DEFAULT 0 CHECK (unit_price >= 0);
ALTER TABLE candidate_decisions
    ADD COLUMN modified_by_user_id TEXT REFERENCES users(id);

CREATE TABLE approval_revisions (
    id TEXT PRIMARY KEY,
    school_id TEXT NOT NULL REFERENCES schools(id),
    workspace_id TEXT NOT NULL REFERENCES acquisition_workspaces(id),
    revision_number INTEGER NOT NULL CHECK (revision_number > 0),
    canonical_json TEXT NOT NULL,
    sha256 TEXT NOT NULL CHECK (
        length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    budget_won INTEGER NOT NULL CHECK (budget_won >= 0),
    expected_total_won INTEGER NOT NULL CHECK (expected_total_won >= 0),
    created_by_user_id TEXT NOT NULL REFERENCES users(id),
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    sealed_at TEXT,
    UNIQUE (workspace_id, revision_number)
);
CREATE INDEX idx_approval_revisions_workspace
    ON approval_revisions(workspace_id, revision_number DESC);

CREATE TABLE approval_rows (
    id TEXT PRIMARY KEY,
    approval_revision_id TEXT NOT NULL REFERENCES approval_revisions(id),
    school_id TEXT NOT NULL REFERENCES schools(id),
    workspace_id TEXT NOT NULL REFERENCES acquisition_workspaces(id),
    candidate_id TEXT NOT NULL REFERENCES candidate_decisions(id),
    recommendation_id TEXT NOT NULL REFERENCES recommendations(id),
    isbn13 TEXT,
    title TEXT NOT NULL,
    author TEXT NOT NULL,
    edition TEXT,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    unit_price INTEGER NOT NULL CHECK (unit_price >= 0),
    line_total_won INTEGER NOT NULL CHECK (line_total_won >= 0),
    created_at TEXT NOT NULL,
    UNIQUE (approval_revision_id, candidate_id)
);
CREATE INDEX idx_approval_rows_revision_isbn
    ON approval_rows(approval_revision_id, isbn13);

CREATE TABLE approval_comments (
    id TEXT PRIMARY KEY,
    approval_revision_id TEXT NOT NULL REFERENCES approval_revisions(id),
    school_id TEXT NOT NULL REFERENCES schools(id),
    actor_id TEXT NOT NULL REFERENCES users(id),
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE approval_decisions (
    id TEXT PRIMARY KEY,
    approval_revision_id TEXT NOT NULL UNIQUE REFERENCES approval_revisions(id),
    school_id TEXT NOT NULL REFERENCES schools(id),
    workspace_id TEXT NOT NULL REFERENCES acquisition_workspaces(id),
    actor_id TEXT NOT NULL REFERENCES users(id),
    decision TEXT NOT NULL CHECK (decision IN ('APPROVED', 'REJECTED')),
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE vendor_quotes (
    id TEXT PRIMARY KEY,
    school_id TEXT NOT NULL REFERENCES schools(id),
    workspace_id TEXT NOT NULL REFERENCES acquisition_workspaces(id),
    approval_revision_id TEXT NOT NULL REFERENCES approval_revisions(id),
    vendor_name TEXT NOT NULL,
    total_won INTEGER NOT NULL DEFAULT 0 CHECK (total_won >= 0),
    list_total_won INTEGER NOT NULL DEFAULT 0 CHECK (list_total_won >= 0),
    discount_won INTEGER NOT NULL DEFAULT 0,
    budget_overrun_won INTEGER NOT NULL DEFAULT 0 CHECK (budget_overrun_won >= 0),
    out_of_stock_count INTEGER NOT NULL DEFAULT 0 CHECK (out_of_stock_count >= 0),
    missing_price_count INTEGER NOT NULL DEFAULT 0 CHECK (missing_price_count >= 0),
    list_mismatch_count INTEGER NOT NULL DEFAULT 0 CHECK (list_mismatch_count >= 0),
    needs_review_count INTEGER NOT NULL DEFAULT 0 CHECK (needs_review_count >= 0),
    unmatched_count INTEGER NOT NULL DEFAULT 0 CHECK (unmatched_count >= 0),
    requires_reapproval INTEGER NOT NULL DEFAULT 0 CHECK (requires_reapproval IN (0, 1)),
    reason TEXT NOT NULL,
    created_by_user_id TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    sealed_at TEXT,
    row_version INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX idx_vendor_quotes_workspace
    ON vendor_quotes(workspace_id, created_at DESC);

CREATE TABLE vendor_quote_rows (
    id TEXT PRIMARY KEY,
    quote_id TEXT NOT NULL REFERENCES vendor_quotes(id),
    approval_row_id TEXT REFERENCES approval_rows(id),
    match_status TEXT NOT NULL CHECK (
        match_status IN ('MATCHED_ISBN', 'NEEDS_REVIEW', 'UNMATCHED')
    ),
    isbn13 TEXT,
    title TEXT NOT NULL,
    author TEXT NOT NULL,
    publisher TEXT,
    edition TEXT,
    quantity INTEGER NOT NULL CHECK (quantity >= 0),
    unit_price INTEGER CHECK (unit_price >= 0),
    list_price INTEGER CHECK (list_price >= 0),
    out_of_stock INTEGER NOT NULL DEFAULT 0 CHECK (out_of_stock IN (0, 1)),
    line_total_won INTEGER NOT NULL DEFAULT 0 CHECK (line_total_won >= 0),
    created_at TEXT NOT NULL
);
CREATE INDEX idx_vendor_quote_rows_quote ON vendor_quote_rows(quote_id);

CREATE TABLE generated_artifacts (
    id TEXT PRIMARY KEY,
    school_id TEXT NOT NULL REFERENCES schools(id),
    workspace_id TEXT REFERENCES acquisition_workspaces(id),
    artifact_type TEXT NOT NULL,
    storage_path TEXT NOT NULL UNIQUE,
    sha256 TEXT NOT NULL CHECK (
        length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    content_bytes BLOB NOT NULL,
    created_by_user_id TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL
);

CREATE TABLE order_revisions (
    id TEXT PRIMARY KEY,
    school_id TEXT NOT NULL REFERENCES schools(id),
    workspace_id TEXT NOT NULL REFERENCES acquisition_workspaces(id),
    approval_revision_id TEXT NOT NULL REFERENCES approval_revisions(id),
    quote_id TEXT NOT NULL REFERENCES vendor_quotes(id),
    artifact_id TEXT NOT NULL UNIQUE REFERENCES generated_artifacts(id),
    revision_number INTEGER NOT NULL CHECK (revision_number > 0),
    reason TEXT NOT NULL,
    created_by_user_id TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    sealed_at TEXT,
    UNIQUE (workspace_id, revision_number)
);
CREATE INDEX idx_order_revisions_workspace
    ON order_revisions(workspace_id, revision_number DESC);

CREATE TABLE order_rows (
    id TEXT PRIMARY KEY,
    order_revision_id TEXT NOT NULL REFERENCES order_revisions(id),
    approval_row_id TEXT NOT NULL REFERENCES approval_rows(id),
    isbn13 TEXT,
    title TEXT NOT NULL,
    author TEXT NOT NULL,
    publisher TEXT,
    edition TEXT,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    unit_price INTEGER NOT NULL CHECK (unit_price >= 0),
    line_total_won INTEGER NOT NULL CHECK (line_total_won >= 0),
    created_at TEXT NOT NULL,
    UNIQUE (order_revision_id, approval_row_id)
);

CREATE TABLE order_transmissions (
    id TEXT PRIMARY KEY,
    order_revision_id TEXT NOT NULL REFERENCES order_revisions(id),
    school_id TEXT NOT NULL REFERENCES schools(id),
    workspace_id TEXT NOT NULL REFERENCES acquisition_workspaces(id),
    actor_id TEXT NOT NULL REFERENCES users(id),
    reason TEXT NOT NULL,
    sent_at TEXT NOT NULL
);

CREATE TABLE delivery_batches (
    id TEXT PRIMARY KEY,
    school_id TEXT NOT NULL REFERENCES schools(id),
    workspace_id TEXT NOT NULL REFERENCES acquisition_workspaces(id),
    order_revision_id TEXT NOT NULL REFERENCES order_revisions(id),
    delivery_number INTEGER NOT NULL CHECK (delivery_number > 0),
    reason TEXT NOT NULL,
    created_by_user_id TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    sealed_at TEXT,
    UNIQUE (workspace_id, delivery_number)
);

CREATE TABLE delivery_rows (
    id TEXT PRIMARY KEY,
    delivery_batch_id TEXT NOT NULL REFERENCES delivery_batches(id),
    isbn13 TEXT,
    title TEXT NOT NULL,
    author TEXT,
    edition TEXT,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    unit_price INTEGER CHECK (unit_price >= 0),
    created_at TEXT NOT NULL
);

CREATE TABLE scan_sessions (
    id TEXT PRIMARY KEY,
    school_id TEXT NOT NULL REFERENCES schools(id),
    workspace_id TEXT NOT NULL REFERENCES acquisition_workspaces(id),
    order_revision_id TEXT NOT NULL REFERENCES order_revisions(id),
    actor_id TEXT NOT NULL REFERENCES users(id),
    started_at TEXT NOT NULL,
    ended_at TEXT,
    row_version INTEGER NOT NULL DEFAULT 1
);
CREATE UNIQUE INDEX idx_scan_sessions_one_active_workspace
    ON scan_sessions(workspace_id) WHERE ended_at IS NULL;

CREATE TABLE scan_events (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES scan_sessions(id),
    school_id TEXT NOT NULL REFERENCES schools(id),
    workspace_id TEXT NOT NULL REFERENCES acquisition_workspaces(id),
    actor_id TEXT NOT NULL REFERENCES users(id),
    order_row_id TEXT REFERENCES order_rows(id),
    isbn13 TEXT,
    result_code TEXT NOT NULL CHECK (
        result_code IN ('NORMAL', 'OVER', 'UNORDERED', 'EDITION_MISMATCH')
    ),
    scanned_quantity INTEGER NOT NULL DEFAULT 0 CHECK (scanned_quantity >= 0),
    idempotency_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (session_id, idempotency_key)
);

CREATE TABLE receiving_differences (
    id TEXT PRIMARY KEY,
    school_id TEXT NOT NULL REFERENCES schools(id),
    workspace_id TEXT NOT NULL REFERENCES acquisition_workspaces(id),
    order_revision_id TEXT NOT NULL REFERENCES order_revisions(id),
    order_row_id TEXT REFERENCES order_rows(id),
    kind TEXT NOT NULL CHECK (
        kind IN ('MISSING', 'OVER', 'QUANTITY', 'UNIT_PRICE', 'ISBN', 'EDITION', 'UNORDERED')
    ),
    reference_key TEXT NOT NULL,
    details_json TEXT NOT NULL,
    disposition TEXT CHECK (
        disposition IN (
            'VENDOR_CONFIRM', 'RETURN_PLANNED',
            'ADDITIONAL_DELIVERY_PLANNED', 'ACCEPTED'
        )
    ),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    row_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX idx_receiving_differences_active
    ON receiving_differences(workspace_id, active, disposition);

-- Tenant graph constraints also protect direct SQL access.
CREATE TRIGGER candidate_modified_actor_scope_insert
BEFORE INSERT ON candidate_decisions
FOR EACH ROW WHEN NEW.modified_by_user_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM users u
    WHERE u.id = NEW.modified_by_user_id AND u.school_id = NEW.school_id
)
BEGIN SELECT RAISE(ABORT, 'candidate modifier scope mismatch'); END;
CREATE TRIGGER candidate_modified_actor_scope_update
BEFORE UPDATE OF modified_by_user_id, school_id ON candidate_decisions
FOR EACH ROW WHEN NEW.modified_by_user_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM users u
    WHERE u.id = NEW.modified_by_user_id AND u.school_id = NEW.school_id
)
BEGIN SELECT RAISE(ABORT, 'candidate modifier scope mismatch'); END;

CREATE TRIGGER approval_revision_scope_insert
BEFORE INSERT ON approval_revisions
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1 FROM acquisition_workspaces aw
    JOIN users u ON u.id = NEW.created_by_user_id
    WHERE aw.id = NEW.workspace_id AND aw.school_id = NEW.school_id
      AND u.school_id = NEW.school_id
)
BEGIN SELECT RAISE(ABORT, 'approval revision scope mismatch'); END;

CREATE TRIGGER approval_row_scope_insert
BEFORE INSERT ON approval_rows
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1 FROM approval_revisions ar
    JOIN candidate_decisions cd ON cd.id = NEW.candidate_id
    JOIN recommendations r ON r.id = NEW.recommendation_id
    WHERE ar.id = NEW.approval_revision_id
      AND ar.school_id = NEW.school_id AND ar.workspace_id = NEW.workspace_id
      AND cd.school_id = NEW.school_id AND cd.workspace_id = NEW.workspace_id
      AND cd.recommendation_id = r.id
)
BEGIN SELECT RAISE(ABORT, 'approval row scope mismatch'); END;

CREATE TRIGGER approval_comment_scope_insert
BEFORE INSERT ON approval_comments
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1 FROM approval_revisions ar
    JOIN users u ON u.id = NEW.actor_id
    WHERE ar.id = NEW.approval_revision_id
      AND ar.school_id = NEW.school_id AND u.school_id = NEW.school_id
)
BEGIN SELECT RAISE(ABORT, 'approval comment scope mismatch'); END;
CREATE TRIGGER approval_decision_scope_insert
BEFORE INSERT ON approval_decisions
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1 FROM approval_revisions ar
    JOIN users u ON u.id = NEW.actor_id
    WHERE ar.id = NEW.approval_revision_id
      AND ar.school_id = NEW.school_id AND ar.workspace_id = NEW.workspace_id
      AND u.school_id = NEW.school_id
)
BEGIN SELECT RAISE(ABORT, 'approval decision scope mismatch'); END;

CREATE TRIGGER immutable_sealed_approval_revision_update
BEFORE UPDATE ON approval_revisions
FOR EACH ROW WHEN OLD.sealed_at IS NOT NULL OR NOT (
    OLD.sealed_at IS NULL AND NEW.sealed_at IS NOT NULL
    AND NEW.id = OLD.id AND NEW.school_id = OLD.school_id
    AND NEW.workspace_id = OLD.workspace_id
    AND NEW.revision_number = OLD.revision_number
    AND NEW.canonical_json = OLD.canonical_json AND NEW.sha256 = OLD.sha256
    AND NEW.budget_won = OLD.budget_won
    AND NEW.expected_total_won = OLD.expected_total_won
    AND NEW.created_by_user_id = OLD.created_by_user_id
    AND NEW.reason = OLD.reason AND NEW.created_at = OLD.created_at
)
BEGIN SELECT RAISE(ABORT, 'approval revision is immutable'); END;
CREATE TRIGGER immutable_approval_revision_delete
BEFORE DELETE ON approval_revisions
BEGIN SELECT RAISE(ABORT, 'approval revision is immutable'); END;
CREATE TRIGGER immutable_approval_rows_insert
BEFORE INSERT ON approval_rows
FOR EACH ROW WHEN EXISTS (
    SELECT 1 FROM approval_revisions
    WHERE id = NEW.approval_revision_id AND sealed_at IS NOT NULL
)
BEGIN SELECT RAISE(ABORT, 'approval rows are immutable'); END;
CREATE TRIGGER immutable_approval_rows_update
BEFORE UPDATE ON approval_rows
BEGIN SELECT RAISE(ABORT, 'approval rows are immutable'); END;
CREATE TRIGGER immutable_approval_rows_delete
BEFORE DELETE ON approval_rows
BEGIN SELECT RAISE(ABORT, 'approval rows are immutable'); END;

CREATE TRIGGER quote_scope_insert
BEFORE INSERT ON vendor_quotes
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1 FROM approval_revisions ar
    WHERE ar.id = NEW.approval_revision_id AND ar.sealed_at IS NOT NULL
      AND ar.school_id = NEW.school_id AND ar.workspace_id = NEW.workspace_id
)
BEGIN SELECT RAISE(ABORT, 'quote scope mismatch'); END;
CREATE TRIGGER quote_row_scope_insert
BEFORE INSERT ON vendor_quote_rows
FOR EACH ROW WHEN NEW.approval_row_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM vendor_quotes vq
    JOIN approval_rows ar ON ar.id = NEW.approval_row_id
    WHERE vq.id = NEW.quote_id
      AND ar.approval_revision_id = vq.approval_revision_id
)
BEGIN SELECT RAISE(ABORT, 'quote row scope mismatch'); END;
CREATE TRIGGER immutable_sealed_quote_update
BEFORE UPDATE ON vendor_quotes
FOR EACH ROW WHEN OLD.sealed_at IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'vendor quote is immutable'); END;
CREATE TRIGGER immutable_quote_delete
BEFORE DELETE ON vendor_quotes
BEGIN SELECT RAISE(ABORT, 'vendor quote is immutable'); END;
CREATE TRIGGER immutable_quote_rows_insert
BEFORE INSERT ON vendor_quote_rows
FOR EACH ROW WHEN EXISTS (
    SELECT 1 FROM vendor_quotes WHERE id = NEW.quote_id AND sealed_at IS NOT NULL
)
BEGIN SELECT RAISE(ABORT, 'vendor quote rows are immutable'); END;
CREATE TRIGGER immutable_quote_rows_update
BEFORE UPDATE ON vendor_quote_rows
BEGIN SELECT RAISE(ABORT, 'vendor quote rows are immutable'); END;
CREATE TRIGGER immutable_quote_rows_delete
BEFORE DELETE ON vendor_quote_rows
BEGIN SELECT RAISE(ABORT, 'vendor quote rows are immutable'); END;

CREATE TRIGGER immutable_artifact_update
BEFORE UPDATE ON generated_artifacts
BEGIN SELECT RAISE(ABORT, 'artifact is immutable'); END;
CREATE TRIGGER immutable_artifact_delete
BEFORE DELETE ON generated_artifacts
BEGIN SELECT RAISE(ABORT, 'artifact is immutable'); END;
CREATE TRIGGER artifact_scope_insert
BEFORE INSERT ON generated_artifacts
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1 FROM users u
    WHERE u.id = NEW.created_by_user_id AND u.school_id = NEW.school_id
) OR (NEW.workspace_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM acquisition_workspaces aw
    WHERE aw.id = NEW.workspace_id AND aw.school_id = NEW.school_id
))
BEGIN SELECT RAISE(ABORT, 'artifact scope mismatch'); END;

CREATE TRIGGER order_scope_insert
BEFORE INSERT ON order_revisions
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1 FROM approval_revisions ar
    JOIN vendor_quotes vq ON vq.id = NEW.quote_id
    JOIN generated_artifacts ga ON ga.id = NEW.artifact_id
    WHERE ar.id = NEW.approval_revision_id
      AND ar.school_id = NEW.school_id AND ar.workspace_id = NEW.workspace_id
      AND vq.approval_revision_id = ar.id
      AND vq.school_id = NEW.school_id AND vq.workspace_id = NEW.workspace_id
      AND ga.school_id = NEW.school_id AND ga.workspace_id = NEW.workspace_id
)
BEGIN SELECT RAISE(ABORT, 'order revision scope mismatch'); END;
CREATE TRIGGER order_row_scope_insert
BEFORE INSERT ON order_rows
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1 FROM order_revisions ors
    JOIN approval_rows ar ON ar.id = NEW.approval_row_id
    WHERE ors.id = NEW.order_revision_id
      AND ar.approval_revision_id = ors.approval_revision_id
)
BEGIN SELECT RAISE(ABORT, 'order row scope mismatch'); END;
CREATE TRIGGER order_transmission_scope_insert
BEFORE INSERT ON order_transmissions
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1 FROM order_revisions ors
    JOIN users u ON u.id = NEW.actor_id
    WHERE ors.id = NEW.order_revision_id
      AND ors.school_id = NEW.school_id AND ors.workspace_id = NEW.workspace_id
      AND u.school_id = NEW.school_id
)
BEGIN SELECT RAISE(ABORT, 'order transmission scope mismatch'); END;
CREATE TRIGGER immutable_sealed_order_update
BEFORE UPDATE ON order_revisions
FOR EACH ROW WHEN OLD.sealed_at IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'order revision is immutable'); END;
CREATE TRIGGER immutable_order_delete
BEFORE DELETE ON order_revisions
BEGIN SELECT RAISE(ABORT, 'order revision is immutable'); END;
CREATE TRIGGER immutable_order_rows_insert
BEFORE INSERT ON order_rows
FOR EACH ROW WHEN EXISTS (
    SELECT 1 FROM order_revisions WHERE id = NEW.order_revision_id AND sealed_at IS NOT NULL
)
BEGIN SELECT RAISE(ABORT, 'order rows are immutable'); END;
CREATE TRIGGER immutable_order_rows_update
BEFORE UPDATE ON order_rows
BEGIN SELECT RAISE(ABORT, 'order rows are immutable'); END;
CREATE TRIGGER immutable_order_rows_delete
BEFORE DELETE ON order_rows
BEGIN SELECT RAISE(ABORT, 'order rows are immutable'); END;

CREATE TRIGGER immutable_delivery_rows_update
BEFORE UPDATE ON delivery_rows
BEGIN SELECT RAISE(ABORT, 'delivery rows are immutable'); END;
CREATE TRIGGER delivery_batch_scope_insert
BEFORE INSERT ON delivery_batches
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1 FROM order_revisions ors
    JOIN users u ON u.id = NEW.created_by_user_id
    WHERE ors.id = NEW.order_revision_id
      AND ors.school_id = NEW.school_id AND ors.workspace_id = NEW.workspace_id
      AND u.school_id = NEW.school_id
)
BEGIN SELECT RAISE(ABORT, 'delivery batch scope mismatch'); END;
CREATE TRIGGER immutable_delivery_rows_delete
BEFORE DELETE ON delivery_rows
BEGIN SELECT RAISE(ABORT, 'delivery rows are immutable'); END;
CREATE TRIGGER immutable_sealed_delivery_batch_update
BEFORE UPDATE ON delivery_batches
FOR EACH ROW WHEN OLD.sealed_at IS NOT NULL OR NOT (
    OLD.sealed_at IS NULL AND NEW.sealed_at IS NOT NULL
    AND NEW.id = OLD.id AND NEW.school_id = OLD.school_id
    AND NEW.workspace_id = OLD.workspace_id
    AND NEW.order_revision_id = OLD.order_revision_id
    AND NEW.delivery_number = OLD.delivery_number
    AND NEW.reason = OLD.reason
    AND NEW.created_by_user_id = OLD.created_by_user_id
    AND NEW.created_at = OLD.created_at
)
BEGIN SELECT RAISE(ABORT, 'delivery batch is immutable'); END;
CREATE TRIGGER immutable_delivery_batch_delete
BEFORE DELETE ON delivery_batches
BEGIN SELECT RAISE(ABORT, 'delivery batch is immutable'); END;
CREATE TRIGGER immutable_delivery_rows_insert
BEFORE INSERT ON delivery_rows
FOR EACH ROW WHEN EXISTS (
    SELECT 1 FROM delivery_batches
    WHERE id = NEW.delivery_batch_id AND sealed_at IS NOT NULL
)
BEGIN SELECT RAISE(ABORT, 'delivery rows are immutable'); END;

CREATE TRIGGER immutable_scan_events_update
BEFORE UPDATE ON scan_events
BEGIN SELECT RAISE(ABORT, 'scan events are immutable'); END;

CREATE TRIGGER scan_session_scope_insert
BEFORE INSERT ON scan_sessions
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1 FROM order_revisions ors
    JOIN users u ON u.id = NEW.actor_id
    WHERE ors.id = NEW.order_revision_id
      AND ors.school_id = NEW.school_id AND ors.workspace_id = NEW.workspace_id
      AND u.school_id = NEW.school_id
)
BEGIN SELECT RAISE(ABORT, 'scan session scope mismatch'); END;
CREATE TRIGGER scan_session_scope_update
BEFORE UPDATE OF school_id, workspace_id, order_revision_id, actor_id ON scan_sessions
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1 FROM order_revisions ors
    JOIN users u ON u.id = NEW.actor_id
    WHERE ors.id = NEW.order_revision_id
      AND ors.school_id = NEW.school_id AND ors.workspace_id = NEW.workspace_id
      AND u.school_id = NEW.school_id
)
BEGIN SELECT RAISE(ABORT, 'scan session scope mismatch'); END;
CREATE TRIGGER scan_event_scope_insert
BEFORE INSERT ON scan_events
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1 FROM scan_sessions ss
    JOIN users u ON u.id = NEW.actor_id
    WHERE ss.id = NEW.session_id
      AND ss.school_id = NEW.school_id AND ss.workspace_id = NEW.workspace_id
      AND u.school_id = NEW.school_id
      AND (NEW.order_row_id IS NULL OR EXISTS (
          SELECT 1 FROM order_rows ors
          WHERE ors.id = NEW.order_row_id
            AND ors.order_revision_id = ss.order_revision_id
      ))
)
BEGIN SELECT RAISE(ABORT, 'scan event scope mismatch'); END;
CREATE TRIGGER receiving_difference_scope_insert
BEFORE INSERT ON receiving_differences
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1 FROM order_revisions ors
    WHERE ors.id = NEW.order_revision_id
      AND ors.school_id = NEW.school_id AND ors.workspace_id = NEW.workspace_id
      AND (NEW.order_row_id IS NULL OR EXISTS (
          SELECT 1 FROM order_rows orow
          WHERE orow.id = NEW.order_row_id
            AND orow.order_revision_id = ors.id
      ))
)
BEGIN SELECT RAISE(ABORT, 'receiving difference scope mismatch'); END;
CREATE TRIGGER receiving_difference_scope_update
BEFORE UPDATE OF school_id, workspace_id, order_revision_id, order_row_id
ON receiving_differences
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1 FROM order_revisions ors
    WHERE ors.id = NEW.order_revision_id
      AND ors.school_id = NEW.school_id AND ors.workspace_id = NEW.workspace_id
      AND (NEW.order_row_id IS NULL OR EXISTS (
          SELECT 1 FROM order_rows orow
          WHERE orow.id = NEW.order_row_id
            AND orow.order_revision_id = ors.id
      ))
)
BEGIN SELECT RAISE(ABORT, 'receiving difference scope mismatch'); END;
CREATE TRIGGER immutable_scan_events_delete
BEFORE DELETE ON scan_events
BEGIN SELECT RAISE(ABORT, 'scan events are immutable'); END;

CREATE TRIGGER immutable_approval_comments_update
BEFORE UPDATE ON approval_comments
BEGIN SELECT RAISE(ABORT, 'approval comments are immutable'); END;
CREATE TRIGGER immutable_approval_comments_delete
BEFORE DELETE ON approval_comments
BEGIN SELECT RAISE(ABORT, 'approval comments are immutable'); END;
CREATE TRIGGER immutable_approval_decisions_update
BEFORE UPDATE ON approval_decisions
BEGIN SELECT RAISE(ABORT, 'approval decisions are immutable'); END;
CREATE TRIGGER immutable_approval_decisions_delete
BEFORE DELETE ON approval_decisions
BEGIN SELECT RAISE(ABORT, 'approval decisions are immutable'); END;
CREATE TRIGGER immutable_order_transmissions_update
BEFORE UPDATE ON order_transmissions
BEGIN SELECT RAISE(ABORT, 'order transmissions are immutable'); END;
CREATE TRIGGER immutable_order_transmissions_delete
BEFORE DELETE ON order_transmissions
BEGIN SELECT RAISE(ABORT, 'order transmissions are immutable'); END;
