PRAGMA defer_foreign_keys = ON;

UPDATE acquisition_workspaces
SET status = CASE status
    WHEN 'DATA_PREPARATION' THEN 'DRAFT'
    WHEN 'COMPARING' THEN 'ANALYZING'
    WHEN 'REVISION_REQUESTED' THEN 'CHANGES_REQUESTED'
    WHEN 'QUOTE_ADJUSTMENT' THEN 'QUOTE_REVIEW'
    WHEN 'AWAITING_DELIVERY' THEN 'ORDER_SENT'
    WHEN 'COMPLETE' THEN 'COMPLETED'
    ELSE status
END;

ALTER TABLE vendor_quotes
    ADD COLUMN revision_number INTEGER NOT NULL DEFAULT 1 CHECK (revision_number > 0);
ALTER TABLE vendor_quotes
    ADD COLUMN parent_quote_id TEXT;
ALTER TABLE vendor_quotes
    ADD COLUMN reconciliation_json TEXT NOT NULL DEFAULT '{}';
DROP TRIGGER immutable_sealed_quote_update;
UPDATE vendor_quotes
SET revision_number = (
    SELECT COUNT(*) FROM vendor_quotes earlier
    WHERE earlier.workspace_id = vendor_quotes.workspace_id
      AND earlier.vendor_name = vendor_quotes.vendor_name
      AND (
          earlier.created_at < vendor_quotes.created_at
          OR (
              earlier.created_at = vendor_quotes.created_at
              AND earlier.id <= vendor_quotes.id
          )
      )
);
CREATE UNIQUE INDEX idx_vendor_quotes_vendor_revision
    ON vendor_quotes(workspace_id, vendor_name, revision_number);
CREATE TRIGGER immutable_sealed_quote_update
BEFORE UPDATE ON vendor_quotes
FOR EACH ROW WHEN OLD.sealed_at IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'vendor quote is immutable'); END;

DROP TRIGGER immutable_quote_rows_insert;
DROP TRIGGER immutable_quote_rows_update;
DROP TRIGGER immutable_quote_rows_delete;
DROP TRIGGER quote_row_scope_insert;
DROP INDEX idx_vendor_quote_rows_quote;
ALTER TABLE vendor_quote_rows RENAME TO vendor_quote_rows_0005;
CREATE TABLE vendor_quote_rows (
    id TEXT PRIMARY KEY,
    quote_id TEXT NOT NULL REFERENCES vendor_quotes(id),
    approval_row_id TEXT REFERENCES approval_rows(id),
    match_status TEXT NOT NULL CHECK (
        match_status IN (
            'MATCHED_ISBN', 'MATCHED_MANUAL', 'NEEDS_REVIEW', 'UNMATCHED'
        )
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
INSERT INTO vendor_quote_rows (
    id, quote_id, approval_row_id, match_status, isbn13, title, author,
    publisher, edition, quantity, unit_price, list_price, out_of_stock,
    line_total_won, created_at
)
SELECT id, quote_id, approval_row_id, match_status, isbn13, title, author,
       publisher, edition, quantity, unit_price, list_price, out_of_stock,
       line_total_won, created_at
FROM vendor_quote_rows_0005
;
DROP TABLE vendor_quote_rows_0005;
CREATE INDEX idx_vendor_quote_rows_quote ON vendor_quote_rows(quote_id);
CREATE TRIGGER quote_row_scope_insert
BEFORE INSERT ON vendor_quote_rows
FOR EACH ROW WHEN NEW.approval_row_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM vendor_quotes vq
    JOIN approval_rows ar ON ar.id = NEW.approval_row_id
    WHERE vq.id = NEW.quote_id
      AND ar.approval_revision_id = vq.approval_revision_id
)
BEGIN SELECT RAISE(ABORT, 'quote row scope mismatch'); END;
CREATE TRIGGER quote_row_positive_quantity_insert
BEFORE INSERT ON vendor_quote_rows
FOR EACH ROW WHEN NEW.quantity <= 0
BEGIN SELECT RAISE(ABORT, 'quote row quantity must be positive'); END;
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

DROP TRIGGER receiving_difference_scope_insert;
DROP TRIGGER receiving_difference_scope_update;
DROP INDEX idx_receiving_differences_active;
ALTER TABLE receiving_differences RENAME TO receiving_differences_0005;
CREATE TABLE receiving_differences (
    id TEXT PRIMARY KEY,
    school_id TEXT NOT NULL REFERENCES schools(id),
    workspace_id TEXT NOT NULL REFERENCES acquisition_workspaces(id),
    order_revision_id TEXT NOT NULL REFERENCES order_revisions(id),
    order_row_id TEXT REFERENCES order_rows(id),
    kind TEXT NOT NULL CHECK (
        kind IN (
            'MISSING', 'OVER', 'QUANTITY', 'UNIT_PRICE',
            'ISBN', 'EDITION', 'UNORDERED'
        )
    ),
    reference_key TEXT NOT NULL,
    details_json TEXT NOT NULL,
    disposition TEXT CHECK (
        disposition IN (
            'VENDOR_CHECK', 'RETURN_PLANNED',
            'ADDITIONAL_DELIVERY', 'ACCEPTED'
        )
    ),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    row_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
INSERT INTO receiving_differences (
    id, school_id, workspace_id, order_revision_id, order_row_id, kind,
    reference_key, details_json, disposition, active, row_version,
    created_at, updated_at
)
SELECT id, school_id, workspace_id, order_revision_id, order_row_id, kind,
       reference_key, details_json,
       CASE disposition
           WHEN 'VENDOR_CONFIRM' THEN 'VENDOR_CHECK'
           WHEN 'ADDITIONAL_DELIVERY_PLANNED' THEN 'ADDITIONAL_DELIVERY'
           ELSE disposition
       END,
       active, row_version, created_at, updated_at
FROM receiving_differences_0005;
DROP TABLE receiving_differences_0005;
CREATE INDEX idx_receiving_differences_active
    ON receiving_differences(workspace_id, active, disposition);
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

CREATE TABLE approval_cancellations (
    id TEXT PRIMARY KEY,
    approval_revision_id TEXT NOT NULL UNIQUE REFERENCES approval_revisions(id),
    school_id TEXT NOT NULL REFERENCES schools(id),
    workspace_id TEXT NOT NULL REFERENCES acquisition_workspaces(id),
    actor_id TEXT NOT NULL REFERENCES users(id),
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TRIGGER approval_cancellation_scope_insert
BEFORE INSERT ON approval_cancellations
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1 FROM approval_revisions ar
    JOIN users u ON u.id = NEW.actor_id
    WHERE ar.id = NEW.approval_revision_id
      AND ar.school_id = NEW.school_id AND ar.workspace_id = NEW.workspace_id
      AND u.school_id = NEW.school_id
)
BEGIN SELECT RAISE(ABORT, 'approval cancellation scope mismatch'); END;
CREATE TRIGGER immutable_approval_cancellations_update
BEFORE UPDATE ON approval_cancellations
BEGIN SELECT RAISE(ABORT, 'approval cancellation is immutable'); END;
CREATE TRIGGER immutable_approval_cancellations_delete
BEFORE DELETE ON approval_cancellations
BEGIN SELECT RAISE(ABORT, 'approval cancellation is immutable'); END;

CREATE TABLE vendor_quote_manual_mappings (
    id TEXT PRIMARY KEY,
    source_quote_id TEXT NOT NULL REFERENCES vendor_quotes(id),
    result_quote_id TEXT NOT NULL UNIQUE REFERENCES vendor_quotes(id),
    source_quote_row_id TEXT NOT NULL REFERENCES vendor_quote_rows(id),
    approval_row_id TEXT NOT NULL REFERENCES approval_rows(id),
    school_id TEXT NOT NULL REFERENCES schools(id),
    workspace_id TEXT NOT NULL REFERENCES acquisition_workspaces(id),
    actor_id TEXT NOT NULL REFERENCES users(id),
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TRIGGER immutable_manual_quote_mappings_update
BEFORE UPDATE ON vendor_quote_manual_mappings
BEGIN SELECT RAISE(ABORT, 'manual quote mapping is immutable'); END;
CREATE TRIGGER immutable_manual_quote_mappings_delete
BEFORE DELETE ON vendor_quote_manual_mappings
BEGIN SELECT RAISE(ABORT, 'manual quote mapping is immutable'); END;

CREATE TABLE order_validation_results (
    id TEXT PRIMARY KEY,
    school_id TEXT NOT NULL REFERENCES schools(id),
    workspace_id TEXT NOT NULL REFERENCES acquisition_workspaces(id),
    approval_revision_id TEXT NOT NULL REFERENCES approval_revisions(id),
    diagnostics_json TEXT NOT NULL,
    actor_id TEXT NOT NULL REFERENCES users(id),
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TRIGGER immutable_order_validation_update
BEFORE UPDATE ON order_validation_results
BEGIN SELECT RAISE(ABORT, 'order validation is immutable'); END;
CREATE TRIGGER immutable_order_validation_delete
BEFORE DELETE ON order_validation_results
BEGIN SELECT RAISE(ABORT, 'order validation is immutable'); END;

CREATE TABLE workflow_order_templates (
    id TEXT PRIMARY KEY,
    school_id TEXT NOT NULL REFERENCES schools(id),
    vendor_name TEXT NOT NULL,
    template_version INTEGER NOT NULL CHECK (template_version > 0),
    columns_json TEXT NOT NULL,
    created_by_user_id TEXT NOT NULL REFERENCES users(id),
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (school_id, vendor_name, template_version)
);
CREATE INDEX idx_workflow_order_templates_scope
    ON workflow_order_templates(school_id, vendor_name, template_version DESC);
CREATE TRIGGER immutable_workflow_order_templates_update
BEFORE UPDATE ON workflow_order_templates
BEGIN SELECT RAISE(ABORT, 'order template is immutable'); END;
CREATE TRIGGER immutable_workflow_order_templates_delete
BEFORE DELETE ON workflow_order_templates
BEGIN SELECT RAISE(ABORT, 'order template is immutable'); END;

CREATE TABLE order_allocations (
    id TEXT PRIMARY KEY,
    order_revision_id TEXT NOT NULL REFERENCES order_revisions(id),
    quote_id TEXT NOT NULL REFERENCES vendor_quotes(id),
    quote_row_id TEXT NOT NULL REFERENCES vendor_quote_rows(id),
    candidate_id TEXT NOT NULL REFERENCES candidate_decisions(id),
    vendor_name TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    unit_price INTEGER NOT NULL CHECK (unit_price >= 0),
    line_total_won INTEGER NOT NULL CHECK (line_total_won >= 0),
    created_at TEXT NOT NULL,
    UNIQUE (order_revision_id, quote_row_id)
);
CREATE TRIGGER immutable_order_allocations_update
BEFORE UPDATE ON order_allocations
BEGIN SELECT RAISE(ABORT, 'order allocation is immutable'); END;
CREATE TRIGGER immutable_order_allocations_delete
BEFORE DELETE ON order_allocations
BEGIN SELECT RAISE(ABORT, 'order allocation is immutable'); END;

CREATE TABLE order_vendor_artifacts (
    id TEXT PRIMARY KEY,
    order_revision_id TEXT NOT NULL REFERENCES order_revisions(id),
    quote_id TEXT NOT NULL REFERENCES vendor_quotes(id),
    vendor_name TEXT NOT NULL,
    artifact_id TEXT NOT NULL UNIQUE REFERENCES generated_artifacts(id),
    created_at TEXT NOT NULL,
    UNIQUE (order_revision_id, quote_id)
);
CREATE TRIGGER immutable_order_vendor_artifacts_update
BEFORE UPDATE ON order_vendor_artifacts
BEGIN SELECT RAISE(ABORT, 'order vendor artifact is immutable'); END;
CREATE TRIGGER immutable_order_vendor_artifacts_delete
BEFORE DELETE ON order_vendor_artifacts
BEGIN SELECT RAISE(ABORT, 'order vendor artifact is immutable'); END;

CREATE TABLE workspace_current_orders (
    workspace_id TEXT PRIMARY KEY REFERENCES acquisition_workspaces(id),
    school_id TEXT NOT NULL REFERENCES schools(id),
    order_revision_id TEXT NOT NULL UNIQUE REFERENCES order_revisions(id),
    transmission_id TEXT REFERENCES order_transmissions(id),
    row_version INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);
CREATE TRIGGER workspace_current_order_scope_insert
BEFORE INSERT ON workspace_current_orders
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1 FROM order_revisions ors
    WHERE ors.id = NEW.order_revision_id
      AND ors.workspace_id = NEW.workspace_id AND ors.school_id = NEW.school_id
      AND (NEW.transmission_id IS NULL OR EXISTS (
          SELECT 1 FROM order_transmissions ot
          WHERE ot.id = NEW.transmission_id AND ot.order_revision_id = ors.id
      ))
)
BEGIN SELECT RAISE(ABORT, 'current order scope mismatch'); END;
CREATE TRIGGER workspace_current_order_scope_update
BEFORE UPDATE ON workspace_current_orders
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1 FROM order_revisions ors
    WHERE ors.id = NEW.order_revision_id
      AND ors.workspace_id = NEW.workspace_id AND ors.school_id = NEW.school_id
      AND (NEW.transmission_id IS NULL OR EXISTS (
          SELECT 1 FROM order_transmissions ot
          WHERE ot.id = NEW.transmission_id AND ot.order_revision_id = ors.id
      ))
)
BEGIN SELECT RAISE(ABORT, 'current order scope mismatch'); END;

INSERT INTO workspace_current_orders (
    workspace_id, school_id, order_revision_id, transmission_id, updated_at
)
SELECT ors.workspace_id, ors.school_id, ors.id,
       (
           SELECT ot.id FROM order_transmissions ot
           WHERE ot.order_revision_id = ors.id
           ORDER BY ot.sent_at DESC, ot.id DESC LIMIT 1
       ),
       ors.created_at
FROM order_revisions ors
WHERE ors.sealed_at IS NOT NULL
  AND ors.revision_number = (
      SELECT MAX(current.revision_number)
      FROM order_revisions current
      WHERE current.workspace_id = ors.workspace_id AND current.sealed_at IS NOT NULL
  );

CREATE TRIGGER workflow_state_insert_guard
BEFORE INSERT ON acquisition_workspaces
FOR EACH ROW WHEN NEW.status NOT IN (
    'DRAFT', 'ANALYZING', 'CANDIDATE_REVIEW', 'APPROVAL_PENDING',
    'CHANGES_REQUESTED', 'APPROVED', 'QUOTE_REVIEW', 'ORDER_READY',
    'ORDER_SENT', 'RECEIVING', 'COMPLETED'
)
BEGIN SELECT RAISE(ABORT, 'workflow state code'); END;

CREATE TRIGGER workflow_state_transition_guard
BEFORE UPDATE OF status ON acquisition_workspaces
FOR EACH ROW WHEN OLD.status <> NEW.status AND NOT (
    (OLD.status = 'DRAFT' AND NEW.status = 'ANALYZING')
    OR (OLD.status = 'ANALYZING' AND NEW.status = 'CANDIDATE_REVIEW')
    OR (
        OLD.status = 'CANDIDATE_REVIEW' AND NEW.status = 'APPROVAL_PENDING'
        AND EXISTS (
            SELECT 1 FROM approval_revisions ar
            WHERE ar.workspace_id = OLD.id AND ar.school_id = OLD.school_id
              AND ar.sealed_at IS NOT NULL
              AND ar.revision_number = (
                  SELECT MAX(latest.revision_number) FROM approval_revisions latest
                  WHERE latest.workspace_id = OLD.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM approval_decisions ad
                  WHERE ad.approval_revision_id = ar.id
              )
        )
    )
    OR (
        OLD.status = 'APPROVAL_PENDING' AND NEW.status = 'CANDIDATE_REVIEW'
        AND EXISTS (
            SELECT 1 FROM approval_cancellations ac
            JOIN approval_revisions ar ON ar.id = ac.approval_revision_id
            WHERE ac.workspace_id = OLD.id
              AND ar.revision_number = (
                  SELECT MAX(latest.revision_number) FROM approval_revisions latest
                  WHERE latest.workspace_id = OLD.id
              )
        )
    )
    OR (
        OLD.status = 'APPROVAL_PENDING' AND NEW.status = 'CHANGES_REQUESTED'
        AND EXISTS (
            SELECT 1 FROM approval_decisions ad
            JOIN approval_revisions ar ON ar.id = ad.approval_revision_id
            WHERE ad.workspace_id = OLD.id AND ad.decision = 'REJECTED'
              AND ar.revision_number = (
                  SELECT MAX(latest.revision_number) FROM approval_revisions latest
                  WHERE latest.workspace_id = OLD.id
              )
        )
    )
    OR (
        OLD.status = 'CHANGES_REQUESTED' AND NEW.status = 'APPROVAL_PENDING'
        AND EXISTS (
            SELECT 1 FROM approval_revisions ar
            WHERE ar.workspace_id = OLD.id AND ar.sealed_at IS NOT NULL
              AND ar.revision_number = (
                  SELECT MAX(latest.revision_number) FROM approval_revisions latest
                  WHERE latest.workspace_id = OLD.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM approval_decisions ad
                  WHERE ad.approval_revision_id = ar.id
              )
        )
    )
    OR (
        OLD.status = 'APPROVAL_PENDING' AND NEW.status = 'APPROVED'
        AND EXISTS (
            SELECT 1 FROM approval_decisions ad
            JOIN approval_revisions ar ON ar.id = ad.approval_revision_id
            WHERE ad.workspace_id = OLD.id AND ad.decision = 'APPROVED'
              AND ar.revision_number = (
                  SELECT MAX(latest.revision_number) FROM approval_revisions latest
                  WHERE latest.workspace_id = OLD.id
              )
        )
    )
    OR (
        OLD.status = 'APPROVED' AND NEW.status = 'QUOTE_REVIEW'
        AND EXISTS (
            SELECT 1 FROM vendor_quotes vq
            WHERE vq.workspace_id = OLD.id AND vq.sealed_at IS NOT NULL
        )
    )
    OR (
        OLD.status IN ('APPROVED', 'QUOTE_REVIEW', 'ORDER_READY', 'ORDER_SENT', 'RECEIVING')
        AND NEW.status = 'APPROVAL_PENDING'
        AND EXISTS (
            SELECT 1 FROM approval_revisions ar
            WHERE ar.workspace_id = OLD.id AND ar.sealed_at IS NOT NULL
              AND ar.revision_number = (
                  SELECT MAX(latest.revision_number) FROM approval_revisions latest
                  WHERE latest.workspace_id = OLD.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM approval_decisions ad
                  WHERE ad.approval_revision_id = ar.id
              )
        )
    )
    OR (
        OLD.status IN ('QUOTE_REVIEW', 'ORDER_SENT', 'RECEIVING')
        AND NEW.status = 'ORDER_READY'
        AND EXISTS (
            SELECT 1 FROM workspace_current_orders current
            JOIN order_revisions ors ON ors.id = current.order_revision_id
            WHERE current.workspace_id = OLD.id
              AND current.transmission_id IS NULL AND ors.sealed_at IS NOT NULL
        )
    )
    OR (
        OLD.status = 'ORDER_READY' AND NEW.status = 'ORDER_SENT'
        AND EXISTS (
            SELECT 1 FROM workspace_current_orders current
            WHERE current.workspace_id = OLD.id AND current.transmission_id IS NOT NULL
        )
    )
    OR (
        OLD.status = 'ORDER_SENT' AND NEW.status = 'RECEIVING'
        AND EXISTS (
            SELECT 1 FROM workspace_current_orders current
            WHERE current.workspace_id = OLD.id AND current.transmission_id IS NOT NULL
              AND (
                  EXISTS (
                      SELECT 1 FROM delivery_batches db
                      WHERE db.workspace_id = OLD.id
                        AND db.order_revision_id = current.order_revision_id
                        AND db.sealed_at IS NOT NULL
                  )
                  OR EXISTS (
                      SELECT 1 FROM scan_sessions ss
                      WHERE ss.workspace_id = OLD.id
                        AND ss.order_revision_id = current.order_revision_id
                        AND ss.ended_at IS NULL
                  )
              )
        )
    )
    OR (
        OLD.status = 'RECEIVING' AND NEW.status = 'COMPLETED'
        AND NOT EXISTS (
            SELECT 1 FROM receiving_differences rd
            JOIN workspace_current_orders current
              ON current.workspace_id = rd.workspace_id
             AND current.order_revision_id = rd.order_revision_id
            WHERE rd.workspace_id = OLD.id AND rd.active = 1
              AND rd.disposition IS NULL
        )
    )
)
BEGIN SELECT RAISE(ABORT, 'workflow transition guard'); END;
