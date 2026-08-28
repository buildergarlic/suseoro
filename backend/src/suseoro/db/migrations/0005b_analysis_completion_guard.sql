-- Only the durable COMPARE completion boundary may publish candidate review.
CREATE TRIGGER workflow_analysis_completion_guard
BEFORE UPDATE OF status ON acquisition_workspaces
FOR EACH ROW
WHEN OLD.status = 'ANALYZING' AND NEW.status = 'CANDIDATE_REVIEW'
 AND NOT EXISTS (
    SELECT 1
    FROM durable_jobs job
    WHERE job.school_id = OLD.school_id
      AND job.workspace_id = OLD.id
      AND job.job_type = 'COMPARE'
      AND job.status = 'SUCCEEDED'
      AND job.stage = 'COMPLETED'
      AND job.progress_total IS NOT NULL
      AND job.progress_current = job.progress_total
      AND job.updated_at = NEW.updated_at
      AND json_type(job.payload_json, '$.source_document_ids') = 'array'
      AND json_array_length(job.payload_json, '$.source_document_ids') > 0
      AND NOT EXISTS (
          SELECT 1
          FROM json_each(job.payload_json, '$.source_document_ids') source
          WHERE NOT EXISTS (
              SELECT 1
              FROM comparison_file_results result
              WHERE result.school_id = OLD.school_id
                AND result.workspace_id = OLD.id
                AND result.source_document_id = source.value
                AND result.status IN ('SUCCESS', 'PARTIAL')
          )
      )
 )
BEGIN
    SELECT RAISE(ABORT, 'analysis completion required');
END;
