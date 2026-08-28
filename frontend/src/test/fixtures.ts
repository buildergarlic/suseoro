import type { components } from "../api/types";

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

export interface FixtureApi {
  getCurrentUser(): Promise<User>;
  login(input: Schemas["LoginRequest"]): Promise<User>;
  logout(): Promise<void>;
  listWorkspaces(): Promise<Schemas["WorkspacePage"]>;
  getWorkspace(workspaceId: string): Promise<Versioned<Workspace>>;
  listSources(workspaceId: string): Promise<Schemas["SourcePage"]>;
  listUploadRepairs(workspaceId: string): Promise<Schemas["UploadRepairPage"]>;
  listWorkspaceJobs(
    workspaceId: string,
    filters?: { type?: string; status?: string; cursor?: string; limit?: number },
  ): Promise<Schemas["JobPage"]>;
  uploadSources(
    workspaceId: string,
    input: {
      files: File[];
      role: Schemas["DocumentRole"];
      vendorScope?: string;
      requestedStartLocalDate?: string;
      requestedThroughLocalDate?: string;
      repairObligationId?: string;
      repairGeneration?: number;
      replacementSourceId?: string;
    },
  ): Promise<Upload>;
  getJob(jobId: string): Promise<Job>;
  retryJob(jobId: string): Promise<Schemas["JobCommandResponse"]>;
  cancelJob(jobId: string): Promise<Schemas["JobCommandResponse"]>;
  parseSource(sourceId: string): Promise<Schemas["QueuedJobResponse"]>;
  getSource(sourceId: string): Promise<Versioned<Source>>;
  updateSourceMapping(
    sourceId: string,
    input: SourceMapping,
    version: number,
  ): Promise<Versioned<Source>>;
  createComparisonJob(
    workspaceId: string,
    sourceDocumentIds: string[],
    version: number,
  ): Promise<Schemas["ComparisonJobResponse"]>;
  listCandidates(
    workspaceId: string,
    filters: { outcome: string; search?: string; cursor?: string; limit?: number },
  ): Promise<CandidatePage>;
  getCandidate(candidateId: string, workspaceId: string): Promise<Versioned<Candidate>>;
  lockCandidate(
    candidateId: string,
    workspaceId: string,
  ): Promise<Schemas["CandidateLockResponse"]>;
  updateCandidate(
    candidateId: string,
    input: {
      workspace_id: string;
      changes: CandidateChanges;
      reason: string;
    },
    version: number,
  ): Promise<Versioned<Schemas["CandidateMutationResponse"]>>;
  requestApproval(
    workspaceId: string,
    input: Schemas["ApprovalRequest"],
    version: number,
  ): Promise<Schemas["ApprovalRequestResponse"]>;
}

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
        error: null,
        repair_obligation_id: null,
        repair_generation: null,
      })),
    }),
    getJob: async (_jobId) => idleJob,
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
      expires_at: "2026-08-29T08:07:00.000000Z",
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
      state: "APPROVAL_PENDING",
      row_version: version + 1,
    }),
    ...overrides,
  };
}
