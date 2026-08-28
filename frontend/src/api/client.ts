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
}

export interface SuseoroApi {
  getCurrentUser(): Promise<User>;
  login(input: Schemas["LoginRequest"]): Promise<User>;
  logout(): Promise<void>;
  listWorkspaces(): Promise<Schemas["WorkspacePage"]>;
  getWorkspace(workspaceId: string): Promise<Versioned<Workspace>>;
  listSources(workspaceId: string): Promise<Schemas["SourcePage"]>;
  uploadSources(
    workspaceId: string,
    input: UploadInput,
  ): Promise<Schemas["UploadResponse"]>;
  getJob(jobId: string): Promise<Job>;
  retryJob(jobId: string): Promise<Schemas["JobCommandResponse"]>;
  cancelJob(jobId: string): Promise<Schemas["JobCommandResponse"]>;
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
    filters: { outcome: string; search?: string },
  ): Promise<Schemas["CandidatePage"]>;
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
}

function commandId(): string {
  return crypto.randomUUID();
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

async function responseBody(response: Response): Promise<unknown> {
  if (response.status === 204) return undefined;
  const text = await response.text();
  if (!text) return undefined;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return undefined;
  }
}

export function createApiClient(baseUrl = ""): SuseoroApi {
  async function request<T>(
    path: string,
    options: RequestOptions = {},
  ): Promise<{ data: T; etag: string | null }> {
    const method = options.method ?? "GET";
    if (options.mutation && navigator.onLine === false) {
      throw new OfflineMutationError();
    }
    const headers = new Headers({ Accept: "application/json" });
    if (options.form === undefined && options.body !== undefined) {
      headers.set("Content-Type", "application/json");
    }
    if (options.mutation) {
      headers.set("Idempotency-Key", options.command?.commandKey ?? commandId());
      headers.set("X-Request-ID", options.command?.requestId ?? commandId());
    }
    if (options.csrf) {
      const token = csrfToken();
      if (token) headers.set("X-CSRF-Token", token);
    }
    if (options.version !== undefined) {
      headers.set("If-Match", quoteVersion(options.version));
    }
    const response = await fetch(`${baseUrl}${path}`, {
      method,
      credentials: "include",
      headers,
      body:
        options.form ??
        (options.body === undefined ? undefined : JSON.stringify(options.body)),
    });
    const body = await responseBody(response);
    if (!response.ok) {
      throw new ApiClientError(response.status, errorDetail(response.status, body));
    }
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

  return {
    getCurrentUser: async () =>
      (await request<User>("/api/v2/auth/me")).data,
    login: async (input) =>
      (
        await request<User>("/api/v2/auth/login", {
          method: "POST",
          body: input,
          mutation: true,
        })
      ).data,
    logout: async () => {
      await request<void>("/api/v2/auth/logout", {
        method: "POST",
        mutation: true,
        csrf: true,
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
      (
        await request<Schemas["SourcePage"]>(
          `/api/v2/workspaces/${workspaceId}/sources?limit=100`,
        )
      ).data,
    uploadSources: async (workspaceId, input) => {
      const form = new FormData();
      for (const file of input.files) form.append("files", file);
      form.set("role", input.role);
      form.set("vendor_scope", input.vendorScope ?? "*");
      if (input.requestedStartLocalDate) {
        form.set("requested_start_local_date", input.requestedStartLocalDate);
      }
      if (input.requestedThroughLocalDate) {
        form.set("requested_through_local_date", input.requestedThroughLocalDate);
      }
      return (
        await request<Schemas["UploadResponse"]>(
          `/api/v2/workspaces/${workspaceId}/sources`,
          { method: "POST", form, mutation: true, csrf: true },
        )
      ).data;
    },
    getJob: async (jobId) =>
      (await request<Job>(`/api/v2/jobs/${jobId}`)).data,
    retryJob: async (jobId) =>
      (
        await request<Schemas["JobCommandResponse"]>(
          `/api/v2/jobs/${jobId}/retry`,
          { method: "POST", mutation: true, csrf: true },
        )
      ).data,
    cancelJob: async (jobId) =>
      (
        await request<Schemas["JobCommandResponse"]>(
          `/api/v2/jobs/${jobId}/cancel`,
          { method: "POST", mutation: true, csrf: true },
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
        }),
      ),
    createComparisonJob: async (workspaceId, sourceDocumentIds, version) =>
      (
        await request<Schemas["ComparisonJobResponse"]>(
          `/api/v2/workspaces/${workspaceId}/comparison-jobs`,
          {
            method: "POST",
            body: { source_document_ids: sourceDocumentIds },
            version,
            mutation: true,
            csrf: true,
          },
        )
      ).data,
    listCandidates: async (workspaceId, filters) => {
      const query = new URLSearchParams({ outcome: filters.outcome, limit: "100" });
      if (filters.search) query.set("search", filters.search);
      return (
        await request<Schemas["CandidatePage"]>(
          `/api/v2/workspaces/${workspaceId}/candidates?${query.toString()}`,
        )
      ).data;
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
          },
        )
      ).data,
  };
}
