-- Bind legacy UNKNOWN repair contracts once, and allow terminal comparison
-- output to be invalidated before an explicit source correction.
DROP TRIGGER IF EXISTS upload_repair_scope_update;

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
    OR (
        (
            NEW.role != OLD.role
            OR NEW.vendor_scope != OLD.vendor_scope
            OR NEW.requested_start_local_date IS NOT OLD.requested_start_local_date
            OR NEW.requested_through_local_date IS NOT OLD.requested_through_local_date
        )
        AND NOT (
            OLD.role = 'UNKNOWN'
            AND NEW.generation > OLD.generation
            AND NEW.role != 'UNKNOWN'
        )
    )
    OR NEW.error_code != OLD.error_code
    OR NEW.generation < OLD.generation
BEGIN SELECT RAISE(ABORT, 'upload repair identity is immutable'); END;

DROP TRIGGER IF EXISTS workflow_state_transition_guard;

CREATE TRIGGER workflow_state_transition_guard
BEFORE UPDATE OF status ON acquisition_workspaces
FOR EACH ROW WHEN OLD.status <> NEW.status AND NOT (
    (OLD.status = 'DRAFT' AND NEW.status = 'ANALYZING')
    OR (OLD.status = 'ANALYZING' AND NEW.status = 'CANDIDATE_REVIEW')
    OR (
        OLD.status = 'ANALYZING' AND NEW.status = 'DRAFT'
        AND EXISTS (
            SELECT 1 FROM durable_jobs job
            WHERE job.school_id = OLD.school_id
              AND job.workspace_id = OLD.id
              AND job.job_type = 'COMPARE'
              AND job.status IN ('FAILED', 'PARTIAL', 'CANCELLED')
        )
        AND NOT EXISTS (
            SELECT 1 FROM durable_jobs job
            WHERE job.school_id = OLD.school_id
              AND job.workspace_id = OLD.id
              AND job.job_type = 'COMPARE'
              AND job.status IN ('QUEUED', 'RUNNING', 'CANCEL_REQUESTED')
        )
        AND NOT EXISTS (
            SELECT 1 FROM approval_rows approval
            WHERE approval.school_id = OLD.school_id
              AND approval.workspace_id = OLD.id
        )
    )
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
