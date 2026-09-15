import type { components } from "../api/types";
import type { SuseoroApi } from "../api/client";

type Schemas = components["schemas"];

export type User = Schemas["UserResponse"];
export type Workspace = Schemas["WorkspaceResponse"];
export type Candidate = Schemas["CandidateResponse"];
export type Source = Schemas["SourceResponse"];
export type Job = Schemas["JobResponse"];
export type Upload = Schemas["UploadResponse"];
export type CandidatePage = Schemas["CandidatePage"];
export type CandidateChanges = Schemas["CandidateChanges"];
export type SourceMapping = Schemas["SourceMapping"];

export interface Versioned<T> {
  data: T;
  etag: string;
}

export type FixtureApi = SuseoroApi;

export class FixtureApiError extends Error {
  readonly status: number;
  readonly detail: Schemas["ApiErrorDetail"];

  constructor(
    status: number,
    code: string,
    message: string,
    fields: Schemas["ApiErrorField"][] = [],
  ) {
    super(message);
    this.status = status;
    this.detail = {
      code,
      message,
      request_id: "550e8400-e29b-41d4-a716-446655440999",
      fields,
    };
  }
}

export const operator: User = {
  id: "550e8400-e29b-41d4-a716-446655440102",
  school_id: "550e8400-e29b-41d4-a716-446655440100",
  username: "operator",
  display_name: "김사서",
  roles: ["OPERATOR"],
};

export const reviewer: User = {
  id: "550e8400-e29b-41d4-a716-446655440103",
  school_id: "550e8400-e29b-41d4-a716-446655440100",
  username: "reviewer",
  display_name: "이검토",
  roles: ["REVIEWER"],
};

export function workspace(
  id: string,
  name: string,
  status: string,
  updatedAt = "2026-08-29T08:05:00.000000Z",
): Workspace {
  return {
    id,
    name,
    status,
    row_version: 1,
    created_at: "2026-08-28T01:00:00.000000Z",
    updated_at: updatedAt,
  };
}

export function candidate(
  id: string,
  title: string,
  outcome: string,
  reason: string | null = null,
): Candidate {
  return {
    id,
    workspace_id: "workspace-candidates",
    title,
    authors: ["글쓴이"],
    isbn13: "9788937464010",
    edition: null,
    outcome,
    reason,
    quantity: 1,
    unit_price: 12_000,
    row_version: 1,
    updated_at: "2026-08-29T08:05:00.000000Z",
  };
}

export const sourceFixture: Source = {
  id: "source-mapping",
  filename: "처음보는양식.csv",
  sha256: "a".repeat(64),
  size_bytes: 120,
  role: "PURCHASE_REQUEST",
  status: "PENDING",
  detected_format: "CSV",
  mapping: {},
  vendor_scope: "*",
  remember_template: false,
  requested_start_local_date: null,
  requested_through_local_date: null,
  parsed_config_version: null,
  row_version: 1,
  created_at: "2026-08-29T08:05:00.000000Z",
  completed_at: null,
  latest_job_id: null,
  latest_result: null,
};

export const idleJob: Job = {
  id: "job-default",
  workspace_id: "workspace-draft",
  type: "INGEST",
  status: "SUCCEEDED",
  stage: "COMPLETE",
  progress_current: 1,
  progress_total: 1,
  error: null,
  retry_count: 0,
  items: [],
};

export function createFixtureApi(
  overrides: Partial<FixtureApi> = {},
): FixtureApi {
  const defaultWorkspace = workspace(
    "workspace-draft",
    "2026학년도 1차 수서",
    "DRAFT",
  );
  return {
    getCurrentUser: async () => operator,
    login: async () => operator,
    logout: async () => undefined,
    listWorkspaces: async () => ({ items: [defaultWorkspace], next_cursor: null }),
    getWorkspace: async (_workspaceId) => ({
      data: defaultWorkspace,
      etag: '"1"',
    }),
    listSources: async (_workspaceId) => ({ items: [], next_cursor: null }),
    listUploadRepairs: async (_workspaceId) => ({ items: [], next_cursor: null }),
    listWorkspaceJobs: async (_workspaceId) => ({ items: [], next_cursor: null }),
    uploadSources: async (_workspaceId, input) => ({
      job_id: "job-default",
      items: input.files.map((file, index) => ({
        filename: file.name,
        status: "ACCEPTED",
        source_id: `source-${index + 1}`,
        procurement_import_id: null,
        error: null,
        repair_obligation_id: null,
        repair_generation: null,
      })),
    }),
    getJob: async (_jobId) => idleJob,
    listProcurementImports: async () => ({ items: [], next_cursor: null }),
    composeProcurementImport: async (importId, version) => ({
      import_id: importId,
      kind: "QUOTE",
      status: "IMPORTED",
      result_id: "quote-1",
      state: "QUOTE_REVIEW",
      row_version: version + 1,
    }),
    retryJob: async (jobId) => ({
      id: jobId,
      workspace_id: "workspace-draft",
      type: "INGEST",
      status: "QUEUED",
      stage: "QUEUED",
      progress_current: 0,
      progress_total: 1,
      error: null,
      retry_count: 1,
    }),
    cancelJob: async (jobId) => ({
      id: jobId,
      workspace_id: "workspace-draft",
      type: "INGEST",
      status: "CANCEL_REQUESTED",
      stage: "PARSING",
      progress_current: 0,
      progress_total: 1,
      error: null,
      retry_count: 0,
    }),
    parseSource: async (_sourceId) => ({
      job_id: "parse-job",
      status: "QUEUED",
    }),
    getSource: async (_sourceId) => ({ data: sourceFixture, etag: '"1"' }),
    updateSourceMapping: async (_sourceId, input, version) => ({
      data: {
        ...sourceFixture,
        role: input.role,
        mapping: input.mapping ?? {},
        row_version: version + 1,
      },
      etag: `"${version + 1}"`,
    }),
    createComparisonJob: async (_workspaceId, _sourceDocumentIds, version) => ({
      job_id: "comparison-job",
      status: "QUEUED",
      workspace_status: "ANALYZING",
      row_version: version + 1,
    }),
    listCandidates: async (_workspaceId, _filters) => ({
      workspace_revision: 0,
      items: [],
      next_cursor: null,
      total_count: 0,
      summary: {
        total_count: 0,
        candidate_count: 0,
        needs_review_count: 0,
        excluded_count: 0,
        unresolved_count: 0,
        expected_total_won: 0,
      },
    }),
    getCandidate: async (_candidateId, _workspaceId) => ({
      data: candidate("candidate-ok", "새로 살 책", "CANDIDATE"),
      etag: '"1"',
    }),
    lockCandidate: async (candidateId, _workspaceId) => ({
      candidate_id: candidateId,
      actor_id: operator.id,
      expires_at: new Date(Date.now() + 120_000).toISOString(),
    }),
    updateCandidate: async (candidateId, input, version) => ({
      data: {
        id: candidateId,
        outcome: input.changes.outcome ?? "CANDIDATE",
        quantity: input.changes.quantity ?? 1,
        unit_price: input.changes.unit_price ?? 12_000,
        row_version: version + 1,
      },
      etag: `"${version + 1}"`,
    }),
    requestApproval: async (_workspaceId, input, version) => ({
      revision_id: "approval-1",
      revision_number: 1,
      sha256: "b".repeat(64),
      expected_total_won: 12_000,
      budget_won: input.budget_won,
      candidate_collection_revision: input.candidate_collection_revision,
      state: "APPROVAL_PENDING",
      row_version: version + 1,
    }),
    listApprovals: async () => ({ items: [], next_cursor: null }),
    getApproval: async (revisionId) => ({
      revision_id: revisionId,
      revision_number: 1,
      sha256: "b".repeat(64),
      payload: {
        budget_won: 12_000,
        candidate_collection_revision: 0,
        candidates: [],
        expected_total_won: 12_000,
      },
      metadata: {
        candidate_count: 0,
        catalog_as_of_local_date: null,
        unresolved_complete: true,
        source_counts_verified: true,
        auto_excluded_count: 0,
        auto_exclusions: [],
        previous_revision: {
          revision_number: null,
          added_count: 0,
          removed_count: 0,
          quantity_changed_count: 0,
          price_changed_count: 0,
        },
        request_reason: "승인 요청",
        requested_by_display_name: operator.display_name,
        created_at: "2026-08-29T08:05:00.000000Z",
        decision: null,
        decision_reason: null,
      },
    }),
    approveApproval: async (revisionId, _workspaceId, _reason, version) => ({
      revision_id: revisionId,
      decision: "APPROVED",
      state: "APPROVED",
      row_version: version + 1,
    }),
    requestApprovalChanges: async (revisionId, _workspaceId, _reason, version) => ({
      revision_id: revisionId,
      decision: "REJECTED",
      state: "CHANGES_REQUESTED",
      row_version: version + 1,
    }),
    listQuotes: async () => ({ items: [], next_cursor: null }),
    getQuote: async (quoteId) => ({
      quote_id: quoteId,
      approval_revision_id: "approval-1",
      vendor_name: "서점",
      created_at: "2026-08-29T08:05:00.000000Z",
      revision_number: 1,
      state: "QUOTE_REVIEW",
      row_version: 1,
      total_won: 0,
      list_total_won: 0,
      discount_won: 0,
      budget_overrun_won: 0,
      out_of_stock_count: 0,
      missing_price_count: 0,
      list_mismatch_count: 0,
      needs_review_count: 0,
      unmatched_count: 0,
      requires_reapproval: false,
      reconciliation: { duplicate_isbns: [], price_conflicts: [] },
      rows: [],
    }),
    createQuote: async (_workspaceId, _input, version) => ({
      quote_id: "quote-1",
      revision_number: 1,
      state: "QUOTE_REVIEW",
      row_version: version + 1,
      total_won: 0,
      list_total_won: 0,
      discount_won: 0,
      budget_overrun_won: 0,
      out_of_stock_count: 0,
      missing_price_count: 0,
      list_mismatch_count: 0,
      needs_review_count: 0,
      unmatched_count: 0,
      requires_reapproval: false,
      reconciliation: { duplicate_isbns: [], price_conflicts: [] },
      rows: [],
    }),
    getCurrentOrder: async () => ({ order: null }),
    createOrder: async (_workspaceId, _input, version) => ({
      status: "READY",
      state: "ORDER_READY",
      row_version: version + 1,
      revision_id: "order-1",
      revision_number: 1,
      artifact_id: "artifact-1",
      sha256: "c".repeat(64),
      size_bytes: 100,
      artifacts: [],
      allocations: [],
      validation_id: null,
      diagnostics: null,
      external_send_performed: false,
    }),
    downloadOrder: async (orderRevisionId) => ({
      blob: new Blob(["order"]),
      filename: `order-${orderRevisionId}.xlsx`,
    }),
    markOrderSent: async (_orderRevisionId, _workspaceId, _reason, version) => ({
      transmission_id: "transmission-1",
      state: "ORDER_SENT",
      row_version: version + 1,
      external_send_performed: false,
    }),
    listDeliveries: async () => ({
      items: [],
      next_cursor: null,
      differences: [],
      differences_truncated: false,
    }),
    listReceivingDifferences: async () => ({ items: [], next_cursor: null }),
    getReceivingStatus: async () => ({
      order_revision_id: null,
      active_session_id: null,
      delivery_count: 0,
      ordered_quantity: 0,
      delivered_quantity: 0,
      scanned_quantity: 0,
      unresolved_difference_count: 0,
      can_complete: false,
      blocking_reasons: ["전달한 발주 버전이 아직 없습니다."],
      rows: [],
    }),
    createDelivery: async (_workspaceId, _input, version) => ({
      delivery_batch_id: "delivery-1",
      delivery_number: 1,
      received_quantity: 0,
      differences: [],
      state: "RECEIVING",
      row_version: version + 1,
    }),
    startScanSession: async (_workspaceId, _input, version) => ({
      session_id: "scan-session-1",
      state: "RECEIVING",
      row_version: version + 1,
    }),
    recordScan: async (_sessionId, input) => ({
      event_id: "scan-event-1",
      code: "NORMAL",
      isbn13: input.isbn.replaceAll("-", ""),
      scanned_quantity: 1,
      order_row_id: input.expected_order_row_id ?? null,
    }),
    setReceivingDisposition: async (differenceId, input, version) => ({
      difference_id: differenceId,
      disposition: input.disposition,
      row_version: version + 1,
    }),
    completeReceiving: async (_workspaceId, _reason, version) => ({
      state: "COMPLETED",
      row_version: version + 1,
    }),
    ...overrides,
  };
}
