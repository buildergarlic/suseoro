import type { components } from "./types";

type Schemas = components["schemas"];

export type User = Schemas["UserResponse"];
export type Workspace = Schemas["WorkspaceResponse"];
export type Candidate = Schemas["CandidateResponse"];
export type Source = Schemas["SourceResponse"];
export type Job = Schemas["JobResponse"];

export interface Versioned<T> {
  data: T;
  etag: string;
}

export interface MutationOptions {
  commandKey?: string;
  requestId?: string;
}

export interface UploadInput {
  files: File[];
  role: Schemas["DocumentRole"];
  vendorScope?: string;
  requestedStartLocalDate?: string;
  requestedThroughLocalDate?: string;
  repairObligationId?: string;
  repairGeneration?: number;
  confirmRepairConfiguration?: boolean;
  replacementSourceId?: string;
}

export interface CandidateFilters {
  outcome: string;
  search?: string;
  cursor?: string;
  limit?: number;
}

export interface WorkspaceJobFilters {
  type?: string;
  status?: string;
  cursor?: string;
  limit?: number;
}

export interface SuseoroApi {
  getCurrentUser(): Promise<User>;
  login(input: Schemas["LoginRequest"]): Promise<User>;
  logout(): Promise<void>;
  listWorkspaces(): Promise<Schemas["WorkspacePage"]>;
  getWorkspace(workspaceId: string): Promise<Versioned<Workspace>>;
  listSources(workspaceId: string): Promise<Schemas["SourcePage"]>;
  listUploadRepairs(workspaceId: string): Promise<Schemas["UploadRepairPage"]>;
  listWorkspaceJobs(
    workspaceId: string,
    filters?: WorkspaceJobFilters,
  ): Promise<Schemas["JobPage"]>;
  uploadSources(
    workspaceId: string,
    input: UploadInput,
  ): Promise<Schemas["UploadResponse"]>;
  getJob(jobId: string): Promise<Job>;
  retryJob(jobId: string): Promise<Schemas["JobCommandResponse"]>;
  cancelJob(jobId: string): Promise<Schemas["JobCommandResponse"]>;
  parseSource(sourceId: string): Promise<Schemas["QueuedJobResponse"]>;
  getSource(sourceId: string): Promise<Versioned<Source>>;
  updateSourceMapping(
    sourceId: string,
    input: Schemas["SourceMapping"],
    version: number,
  ): Promise<Versioned<Source>>;
  createComparisonJob(
    workspaceId: string,
    sourceDocumentIds: string[],
    version: number,
  ): Promise<Schemas["ComparisonJobResponse"]>;
  listCandidates(
    workspaceId: string,
    filters: CandidateFilters,
  ): Promise<Schemas["CandidatePage"]>;
  getCandidate(
    candidateId: string,
    workspaceId: string,
  ): Promise<Versioned<Candidate>>;
  lockCandidate(
    candidateId: string,
    workspaceId: string,
  ): Promise<Schemas["CandidateLockResponse"]>;
  updateCandidate(
    candidateId: string,
    input: Schemas["CandidateUpdate"],
    version: number,
    options?: MutationOptions,
  ): Promise<Versioned<Schemas["CandidateMutationResponse"]>>;
  requestApproval(
    workspaceId: string,
    input: Schemas["ApprovalRequest"],
    version: number,
  ): Promise<Schemas["ApprovalRequestResponse"]>;
}

export class ApiClientError extends Error {
  readonly status: number;
  readonly detail: Schemas["ApiErrorDetail"];

  constructor(status: number, detail: Schemas["ApiErrorDetail"]) {
    super(detail.message);
    this.name = "ApiClientError";
    this.status = status;
    this.detail = detail;
  }
}

export class OfflineMutationError extends Error {
  constructor() {
    super("연결이 끊겨 상태를 변경할 수 없습니다.");
    this.name = "OfflineMutationError";
  }
}

interface RequestOptions<TBody = unknown> {
  method?: "GET" | "POST" | "PATCH";
  body?: TBody;
  form?: FormData;
  version?: number;
  mutation?: boolean;
  csrf?: boolean;
  command?: MutationOptions;
  logicalAction?: string;
  logicalPayload?: unknown;
  validateResponse?: (value: unknown) => boolean;
}

function objectValue(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function stringField(value: Record<string, unknown>, field: string): boolean {
  return typeof value[field] === "string";
}

function numberField(value: Record<string, unknown>, field: string): boolean {
  return typeof value[field] === "number" && Number.isFinite(value[field]);
}

function validUser(value: unknown): boolean {
  return (
    objectValue(value) &&
    stringField(value, "id") &&
    stringField(value, "school_id") &&
    stringField(value, "username") &&
    stringField(value, "display_name") &&
    Array.isArray(value.roles)
  );
}

function validUpload(value: unknown): boolean {
  return (
    objectValue(value) &&
    (value.job_id === null || typeof value.job_id === "string") &&
    Array.isArray(value.items) &&
    value.items.every(
      (item) =>
        objectValue(item) &&
        stringField(item, "filename") &&
        stringField(item, "status") &&
        (item.source_id === null || typeof item.source_id === "string"),
    )
  );
}

function validJobCommand(value: unknown): boolean {
  return (
    objectValue(value) &&
    stringField(value, "id") &&
    stringField(value, "type") &&
    stringField(value, "status") &&
    stringField(value, "stage") &&
    numberField(value, "progress_current") &&
    numberField(value, "progress_total") &&
    numberField(value, "retry_count")
  );
}

function validQueuedJob(value: unknown): boolean {
  return objectValue(value) && stringField(value, "job_id") && stringField(value, "status");
}

function validVersionedSource(value: unknown): boolean {
  return (
    objectValue(value) &&
    stringField(value, "id") &&
    stringField(value, "role") &&
    objectValue(value.mapping) &&
    numberField(value, "row_version")
  );
}

function validComparison(value: unknown): boolean {
  return (
    objectValue(value) &&
    stringField(value, "job_id") &&
    stringField(value, "status") &&
    stringField(value, "workspace_status") &&
    numberField(value, "row_version")
  );
}

function validCandidateLock(value: unknown): boolean {
  return (
    objectValue(value) &&
    stringField(value, "candidate_id") &&
    stringField(value, "actor_id") &&
    stringField(value, "expires_at")
  );
}

function validCandidateMutation(value: unknown): boolean {
  return (
    objectValue(value) &&
    stringField(value, "id") &&
    stringField(value, "outcome") &&
    numberField(value, "quantity") &&
    numberField(value, "row_version") &&
    (value.unit_price === null || numberField(value, "unit_price"))
  );
}

function validApproval(value: unknown): boolean {
  return (
    objectValue(value) &&
    stringField(value, "revision_id") &&
    numberField(value, "revision_number") &&
    stringField(value, "sha256") &&
    numberField(value, "expected_total_won") &&
    numberField(value, "budget_won") &&
    stringField(value, "state") &&
    numberField(value, "row_version")
  );
}

function commandId(): string {
  return crypto.randomUUID();
}

function stableValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(stableValue);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([key, item]) => [key, stableValue(item)]),
    );
  }
  return value;
}

async function payloadFingerprint(value: unknown): Promise<string> {
  const bytes = new TextEncoder().encode(JSON.stringify(stableValue(value)));
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
}

function csrfToken(): string | null {
  for (const part of document.cookie.split(";")) {
    const [name, ...value] = part.trim().split("=");
    if (name === "suseoro_csrf") {
      return decodeURIComponent(value.join("="));
    }
  }
  return null;
}

function quoteVersion(version: number): string {
  return `"${version}"`;
}

function fallbackDetail(status: number): Schemas["ApiErrorDetail"] {
  return {
    code: "HTTP_ERROR",
    message:
      status >= 500
        ? "잠시 문제가 생겼습니다. 잠시 후 다시 시도해 주세요."
        : "요청을 처리하지 못했습니다. 입력 내용을 확인해 주세요.",
    request_id: "unknown",
    fields: [],
  };
}

function errorDetail(
  status: number,
  body: unknown,
): Schemas["ApiErrorDetail"] {
  if (
    typeof body === "object" &&
    body !== null &&
    "detail" in body &&
    typeof body.detail === "object" &&
    body.detail !== null &&
    "code" in body.detail &&
    "message" in body.detail &&
    "request_id" in body.detail &&
    "fields" in body.detail
  ) {
    return body.detail as Schemas["ApiErrorDetail"];
  }
  return fallbackDetail(status);
}

async function responseBody(
  response: Response,
): Promise<{ body: unknown; valid: boolean }> {
  if (response.status === 204) return { body: undefined, valid: true };
  const text = await response.text();
  if (!text) return { body: undefined, valid: false };
  try {
    return { body: JSON.parse(text) as unknown, valid: true };
  } catch {
    return { body: undefined, valid: false };
  }
}

export function createApiClient(baseUrl = ""): SuseoroApi {
  const logicalCommands = new Map<
    string,
    { commandKey: string; active: number; ambiguous: boolean }
  >();
  const fileDigests = new WeakMap<File, Promise<string>>();

  async function fileDigest(file: File): Promise<string> {
    const existing = fileDigests.get(file);
    if (existing) return await existing;
    const pending = file.arrayBuffer().then(async (bytes) => {
      const digest = await crypto.subtle.digest("SHA-256", bytes);
      return Array.from(new Uint8Array(digest), (byte) =>
        byte.toString(16).padStart(2, "0"),
      ).join("");
    });
    fileDigests.set(file, pending);
    return await pending;
  }

  async function request<T>(
    path: string,
    options: RequestOptions = {},
  ): Promise<{ data: T; etag: string | null }> {
    const method = options.method ?? "GET";
    if (options.mutation && navigator.onLine === false) {
      throw new OfflineMutationError();
    }
    let logicalCommand:
      | {
          scope: string;
          commandKey: string;
          resolvesAmbiguity: boolean;
        }
      | undefined;
    if (
      options.mutation &&
      !options.command?.commandKey &&
      options.logicalAction
    ) {
      const fingerprint = await payloadFingerprint(options.logicalPayload);
      const scope = `${options.logicalAction}:${fingerprint}`;
      const current = logicalCommands.get(scope);
      const commandKey = current?.commandKey ?? commandId();
      const resolvesAmbiguity = Boolean(
        current?.ambiguous && current.active === 0,
      );
      logicalCommands.set(scope, {
        commandKey,
        active: (current?.active ?? 0) + 1,
        ambiguous: current?.ambiguous ?? false,
      });
      logicalCommand = {
        scope,
        commandKey,
        resolvesAmbiguity,
      };
    }
    const headers = new Headers({ Accept: "application/json" });
    if (options.form === undefined && options.body !== undefined) {
      headers.set("Content-Type", "application/json");
    }
    if (options.mutation) {
      headers.set(
        "Idempotency-Key",
        options.command?.commandKey ?? logicalCommand?.commandKey ?? commandId(),
      );
      headers.set("X-Request-ID", options.command?.requestId ?? commandId());
    }
    if (options.csrf) {
      const token = csrfToken();
      if (token) headers.set("X-CSRF-Token", token);
    }
    if (options.version !== undefined) {
      headers.set("If-Match", quoteVersion(options.version));
    }
    const settleLogicalCommand = (conclusive: boolean) => {
      if (!logicalCommand) return;
      const current = logicalCommands.get(logicalCommand.scope);
      if (current?.commandKey !== logicalCommand.commandKey) return;
      current.active = Math.max(0, current.active - 1);
      if (!conclusive) current.ambiguous = true;
      if (conclusive && logicalCommand.resolvesAmbiguity) {
        current.ambiguous = false;
      }
      if (conclusive && current.active === 0 && !current.ambiguous) {
        logicalCommands.delete(logicalCommand.scope);
      }
    };
    let response: Response;
    try {
      response = await fetch(`${baseUrl}${path}`, {
        method,
        credentials: "include",
        headers,
        body:
          options.form ??
          (options.body === undefined ? undefined : JSON.stringify(options.body)),
      });
    } catch (error) {
      settleLogicalCommand(false);
      throw error;
    }
    let parsed: Awaited<ReturnType<typeof responseBody>>;
    try {
      parsed = await responseBody(response);
    } catch (error) {
      settleLogicalCommand(false);
      throw error;
    }
    const body = parsed.body;
    if (!response.ok) {
      const failure = new ApiClientError(
        response.status,
        errorDetail(response.status, body),
      );
      const retryable =
        response.status >= 500 ||
        (response.status === 409 &&
          failure.detail.code === "IDEMPOTENCY_REQUEST_IN_PROGRESS");
      settleLogicalCommand(!retryable);
      throw failure;
    }
    if (!parsed.valid || (options.validateResponse && !options.validateResponse(body))) {
      settleLogicalCommand(false);
      throw new Error(
        "서버 응답을 확인할 수 없습니다. 같은 요청으로 다시 시도해 주세요.",
      );
    }
    settleLogicalCommand(true);
    return { data: body as T, etag: response.headers.get("ETag") };
  }

  function versioned<T extends { row_version: number }>(
    result: { data: T; etag: string | null },
  ): Versioned<T> {
    return {
      data: result.data,
      etag: result.etag ?? quoteVersion(result.data.row_version),
    };
  }

  async function collectCursorPages<T extends { id: string }, TPage extends {
    items: T[];
    next_cursor: string | null;
  }>(load: (cursor?: string) => Promise<TPage>, initialCursor?: string): Promise<TPage> {
    const items = new Map<string, T>();
    const seenCursors = new Set<string>();
    let cursor = initialCursor;
    while (true) {
      const marker = cursor ?? "__FIRST__";
      if (seenCursors.has(marker)) {
        throw new Error("목록의 다음 위치가 반복되어 불러오기를 멈췄습니다.");
      }
      seenCursors.add(marker);
      const current = await load(cursor);
      for (const item of current.items) {
        if (!items.has(item.id)) items.set(item.id, item);
      }
      if (!current.next_cursor) {
        return { ...current, items: Array.from(items.values()), next_cursor: null };
      }
      cursor = current.next_cursor;
    }
  }

  return {
    getCurrentUser: async () =>
      (await request<User>("/api/v2/auth/me")).data,
    login: async (input) =>
      (
        await request<User>("/api/v2/auth/login", {
          method: "POST",
          body: input,
          mutation: true,
          logicalAction: "auth:login",
          logicalPayload: input,
          validateResponse: validUser,
        })
      ).data,
    logout: async () => {
      await request<void>("/api/v2/auth/logout", {
        method: "POST",
        mutation: true,
        csrf: true,
        logicalAction: "auth:logout",
        logicalPayload: {},
        validateResponse: (value) => value === undefined,
      });
    },
    listWorkspaces: async () =>
      (await request<Schemas["WorkspacePage"]>("/api/v2/workspaces?limit=100"))
        .data,
    getWorkspace: async (workspaceId) =>
      versioned(
        await request<Workspace>(`/api/v2/workspaces/${workspaceId}`),
      ),
    listSources: async (workspaceId) =>
      await collectCursorPages(async (cursor) => {
        const params = new URLSearchParams({ limit: "100" });
        if (cursor) params.set("cursor", cursor);
        return (
          await request<Schemas["SourcePage"]>(
            `/api/v2/workspaces/${workspaceId}/sources?${params.toString()}`,
          )
        ).data;
      }),
    listUploadRepairs: async (workspaceId) =>
      await collectCursorPages(async (cursor) => {
        const params = new URLSearchParams({ limit: "100" });
        if (cursor) params.set("cursor", cursor);
        return (
          await request<Schemas["UploadRepairPage"]>(
            `/api/v2/workspaces/${workspaceId}/upload-repairs?${params.toString()}`,
          )
        ).data;
      }),
    listWorkspaceJobs: async (workspaceId, filters = {}) => {
      const params = new URLSearchParams({
        limit: String(filters.limit ?? 100),
      });
      if (filters.type) params.set("type", filters.type);
      if (filters.status) params.set("status", filters.status);
      return await collectCursorPages(async (cursor) => {
        const pageParams = new URLSearchParams(params);
        if (cursor) pageParams.set("cursor", cursor);
        return (
          await request<Schemas["JobPage"]>(
            `/api/v2/workspaces/${workspaceId}/jobs?${pageParams.toString()}`,
          )
        ).data;
      }, filters.cursor);
    },
    uploadSources: async (workspaceId, input) => {
      const form = new FormData();
      for (const file of input.files) form.append("files", file);
      if (!input.replacementSourceId) form.set("role", input.role);
      if (
        !input.replacementSourceId &&
        (input.vendorScope !== undefined || !input.repairObligationId)
      ) {
        form.set("vendor_scope", input.vendorScope ?? "*");
      }
      if (input.requestedStartLocalDate) {
        form.set("requested_start_local_date", input.requestedStartLocalDate);
      }
      if (input.requestedThroughLocalDate) {
        form.set("requested_through_local_date", input.requestedThroughLocalDate);
      }
      if (input.repairObligationId) {
        form.set("repair_obligation_id", input.repairObligationId);
        form.set("repair_generation", String(input.repairGeneration ?? 1));
        if (input.confirmRepairConfiguration) {
          form.set("confirm_repair_configuration", "true");
        }
      }
      if (input.replacementSourceId) {
        form.set("replacement_source_document_id", input.replacementSourceId);
      }
      const files = await Promise.all(
        input.files.map(async (file) => ({
          filename: file.name,
          content_type: file.type,
          size_bytes: file.size,
          sha256: await fileDigest(file),
        })),
      );
      return (
        await request<Schemas["UploadResponse"]>(
          `/api/v2/workspaces/${workspaceId}/sources`,
          {
            method: "POST",
            form,
            mutation: true,
            csrf: true,
            logicalAction: `sources:upload:${workspaceId}`,
            logicalPayload: {
              files,
              ...(input.replacementSourceId
                ? {}
                : {
                    role: input.role,
                    vendor_scope: input.vendorScope ?? "*",
                    requested_start_local_date:
                      input.requestedStartLocalDate ?? null,
                    requested_through_local_date:
                      input.requestedThroughLocalDate ?? null,
                  }),
              repair_obligation_id: input.repairObligationId ?? null,
              repair_generation: input.repairGeneration ?? null,
              confirm_repair_configuration:
                input.confirmRepairConfiguration ?? false,
              replacement_source_document_id: input.replacementSourceId ?? null,
            },
            validateResponse: validUpload,
          },
        )
      ).data;
    },
    getJob: async (jobId) =>
      (await request<Job>(`/api/v2/jobs/${jobId}`)).data,
    retryJob: async (jobId) =>
      (
        await request<Schemas["JobCommandResponse"]>(
          `/api/v2/jobs/${jobId}/retry`,
          {
            method: "POST",
            mutation: true,
            csrf: true,
            logicalAction: `jobs:retry:${jobId}`,
            logicalPayload: { job_id: jobId },
            validateResponse: validJobCommand,
          },
        )
      ).data,
    cancelJob: async (jobId) =>
      (
        await request<Schemas["JobCommandResponse"]>(
          `/api/v2/jobs/${jobId}/cancel`,
          {
            method: "POST",
            mutation: true,
            csrf: true,
            logicalAction: `jobs:cancel:${jobId}`,
            logicalPayload: { job_id: jobId },
            validateResponse: validJobCommand,
          },
        )
      ).data,
    parseSource: async (sourceId) =>
      (
        await request<Schemas["QueuedJobResponse"]>(
          `/api/v2/sources/${sourceId}/parse`,
          {
            method: "POST",
            mutation: true,
            csrf: true,
            logicalAction: `sources:parse:${sourceId}`,
            logicalPayload: { source_id: sourceId },
            validateResponse: validQueuedJob,
          },
        )
      ).data,
    getSource: async (sourceId) =>
      versioned(await request<Source>(`/api/v2/sources/${sourceId}`)),
    updateSourceMapping: async (sourceId, input, version) =>
      versioned(
        await request<Source>(`/api/v2/sources/${sourceId}/mapping`, {
          method: "PATCH",
          body: input,
          version,
          mutation: true,
          csrf: true,
          logicalAction: `sources:mapping:${sourceId}`,
          logicalPayload: { ...input, row_version: version },
          validateResponse: validVersionedSource,
        }),
      ),
    createComparisonJob: async (workspaceId, _sourceDocumentIds, version) =>
      (
        await request<Schemas["ComparisonJobResponse"]>(
          `/api/v2/workspaces/${workspaceId}/comparison-jobs`,
          {
            method: "POST",
            body: {},
            version,
            mutation: true,
            csrf: true,
            logicalAction: `workspaces:compare:${workspaceId}`,
            logicalPayload: {
              row_version: version,
            },
            validateResponse: validComparison,
          },
        )
      ).data,
    listCandidates: async (workspaceId, filters) => {
      const query = new URLSearchParams({
        outcome: filters.outcome,
        limit: String(filters.limit ?? 100),
      });
      if (filters.search) query.set("search", filters.search);
      if (filters.cursor) query.set("cursor", filters.cursor);
      return (
        await request<Schemas["CandidatePage"]>(
          `/api/v2/workspaces/${workspaceId}/candidates?${query.toString()}`,
        )
      ).data;
    },
    getCandidate: async (candidateId, workspaceId) => {
      const query = new URLSearchParams({ workspace_id: workspaceId });
      return versioned(
        await request<Candidate>(
          `/api/v2/candidates/${candidateId}?${query.toString()}`,
        ),
      );
    },
    lockCandidate: async (candidateId, workspaceId) =>
      (
        await request<Schemas["CandidateLockResponse"]>(
          `/api/v2/candidates/${candidateId}/lock`,
          {
            method: "POST",
            body: { workspace_id: workspaceId },
            mutation: true,
            csrf: true,
            logicalAction: `candidates:lock:${candidateId}`,
            logicalPayload: { candidate_id: candidateId, workspace_id: workspaceId },
            validateResponse: validCandidateLock,
          },
        )
      ).data,
    updateCandidate: async (candidateId, input, version, command) =>
      versioned(
        await request<Schemas["CandidateMutationResponse"]>(
          `/api/v2/candidates/${candidateId}`,
          {
            method: "PATCH",
            body: input,
            version,
            mutation: true,
            csrf: true,
            command,
            logicalAction: `candidates:update:${candidateId}`,
            logicalPayload: { ...input, row_version: version },
            validateResponse: validCandidateMutation,
          },
        ),
      ),
    requestApproval: async (workspaceId, input, version) =>
      (
        await request<Schemas["ApprovalRequestResponse"]>(
          `/api/v2/workspaces/${workspaceId}/approvals/requests`,
          {
            method: "POST",
            body: input,
            version,
            mutation: true,
            csrf: true,
            logicalAction: `workspaces:approval:${workspaceId}`,
            logicalPayload: { ...input, row_version: version },
            validateResponse: validApproval,
          },
        )
      ).data,
  };
}
