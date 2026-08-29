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

export interface OrderDownload {
  blob: Blob;
  filename: string;
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
  procurementKind?: "QUOTE" | "DELIVERY";
  targetRevisionId?: string;
  reason?: string;
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
  listProcurementImports(
    workspaceId: string,
  ): Promise<Schemas["ProcurementImportPage"]>;
  composeProcurementImport(
    importId: string,
    version: number,
  ): Promise<Schemas["ProcurementImportComposeResponse"]>;
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
  listApprovals(workspaceId: string): Promise<Schemas["ApprovalPage"]>;
  getApproval(revisionId: string): Promise<Schemas["ApprovalDetailResponse"]>;
  approveApproval(
    revisionId: string,
    workspaceId: string,
    reason: string,
    version: number,
  ): Promise<Schemas["ApprovalDecisionResponse"]>;
  requestApprovalChanges(
    revisionId: string,
    workspaceId: string,
    reason: string,
    version: number,
  ): Promise<Schemas["ApprovalDecisionResponse"]>;
  listQuotes(workspaceId: string): Promise<Schemas["QuotePage"]>;
  getQuote(quoteId: string): Promise<Schemas["QuoteDetailResponse"]>;
  createQuote(
    workspaceId: string,
    input: Schemas["QuoteCreate"],
    version: number,
  ): Promise<Schemas["QuoteResponse"]>;
  getCurrentOrder(workspaceId: string): Promise<Schemas["CurrentOrderResponse"]>;
  createOrder(
    workspaceId: string,
    input: Schemas["OrderCreate"],
    version: number,
  ): Promise<Schemas["OrderResponse"]>;
  downloadOrder(orderRevisionId: string): Promise<OrderDownload>;
  markOrderSent(
    orderRevisionId: string,
    workspaceId: string,
    reason: string,
    version: number,
  ): Promise<Schemas["OrderSentResponse"]>;
  listDeliveries(workspaceId: string): Promise<Schemas["DeliveryPage"]>;
  listReceivingDifferences(
    workspaceId: string,
  ): Promise<Schemas["ReceivingDifferencePage"]>;
  getReceivingStatus(workspaceId: string): Promise<Schemas["ReceivingStatusResponse"]>;
  createDelivery(
    workspaceId: string,
    input: Schemas["DeliveryCreate"],
    version: number,
  ): Promise<Schemas["DeliveryResponse"]>;
  startScanSession(
    workspaceId: string,
    input: Schemas["ScanStart"],
    version: number,
  ): Promise<Schemas["ScanSessionResponse"]>;
  recordScan(
    sessionId: string,
    input: Schemas["ScanRecord"],
    options?: MutationOptions,
  ): Promise<Schemas["ScanResponse"]>;
  setReceivingDisposition(
    differenceId: string,
    input: Schemas["DifferenceDisposition"],
    version: number,
  ): Promise<Schemas["DifferenceDispositionResponse"]>;
  completeReceiving(
    workspaceId: string,
    reason: string,
    version: number,
  ): Promise<Schemas["WorkspaceStateResponse"]>;
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

type Guard<T> = (value: unknown) => value is T;
type ExactShape<T extends object> = {
  [Key in keyof T]-?: Guard<T[Key]>;
};

const stringValue: Guard<string> = (value): value is string =>
  typeof value === "string";
const booleanValue: Guard<boolean> = (value): value is boolean =>
  typeof value === "boolean";
const finiteNumber: Guard<number> = (value): value is number =>
  typeof value === "number" && Number.isFinite(value);
const integerValue: Guard<number> = (value): value is number =>
  finiteNumber(value) && Number.isInteger(value);

function nullable<T>(guard: Guard<T>): Guard<T | null> {
  return (value): value is T | null => value === null || guard(value);
}

function arrayOf<T>(guard: Guard<T>): Guard<T[]> {
  return (value): value is T[] => Array.isArray(value) && value.every(guard);
}

function recordOf<T>(guard: Guard<T>): Guard<Record<string, T>> {
  return (value): value is Record<string, T> =>
    objectValue(value) && Object.values(value).every(guard);
}

function exactObject<T extends object>(shape: ExactShape<T>): Guard<T> {
  const keys = Object.keys(shape) as (keyof T & string)[];
  return (value): value is T =>
    objectValue(value) &&
    Object.keys(value).length === keys.length &&
    keys.every(
      (key) =>
        Object.prototype.hasOwnProperty.call(value, key) &&
        shape[key](value[key]),
    );
}

const validJobError = exactObject<Schemas["JobError"]>({
  type: nullable(stringValue),
  code: nullable(stringValue),
  message: nullable(stringValue),
});

const scalarValue: Guard<string | number | boolean | null> = (
  value,
): value is string | number | boolean | null =>
  value === null ||
  stringValue(value) ||
  finiteNumber(value) ||
  booleanValue(value);

const validMappingRequired = exactObject<Schemas["MappingRequired"]>({
  headers: arrayOf(stringValue),
  preview_rows: arrayOf(arrayOf(scalarValue)),
  suggested_mapping: recordOf(nullable(stringValue)),
  required_fields: arrayOf(stringValue),
  confidence: finiteNumber,
  questions: arrayOf(stringValue),
});

const validJobFileResult = exactObject<Schemas["JobFileResult"]>({
  source_document_id: stringValue,
  filename: stringValue,
  status: stringValue,
  total_rows: integerValue,
  processed_rows: integerValue,
  row_error_count: integerValue,
  count_confidence: stringValue,
  error: nullable(validJobError),
  mapping_required: nullable(validMappingRequired),
});

const validUser = exactObject<Schemas["UserResponse"]>({
  id: stringValue,
  school_id: stringValue,
  username: stringValue,
  display_name: stringValue,
  roles: arrayOf(stringValue),
});

const validUploadItemError = exactObject<Schemas["UploadItemError"]>({
  code: stringValue,
  message: stringValue,
});

const validUploadItem = exactObject<Schemas["UploadItem"]>({
  filename: stringValue,
  status: stringValue,
  source_id: nullable(stringValue),
  error: nullable(validUploadItemError),
  repair_obligation_id: nullable(stringValue),
  repair_generation: nullable(integerValue),
  procurement_import_id: nullable(stringValue),
});

const validUpload = exactObject<Schemas["UploadResponse"]>({
  job_id: nullable(stringValue),
  items: arrayOf(validUploadItem),
});

const validProcurementImportProvenance = exactObject<
  Schemas["ProcurementImportProvenance"]
>({
  sheet: nullable(stringValue),
  source_row: integerValue,
});

const validProcurementImportRow = exactObject<
  Schemas["ProcurementImportRow"]
>({
  source_row_id: stringValue,
  status: stringValue,
  provenance: validProcurementImportProvenance,
  error: nullable(validUploadItemError),
  result_row_id: nullable(stringValue),
});

const validProcurementImport = exactObject<
  Schemas["ProcurementImportItem"]
>({
  import_id: stringValue,
  source_id: stringValue,
  kind: stringValue,
  target_revision_id: stringValue,
  vendor_name: stringValue,
  status: stringValue,
  filename: stringValue,
  detected_format: stringValue,
  parser_version: stringValue,
  template_version: nullable(stringValue),
  total_rows: integerValue,
  processed_rows: integerValue,
  row_error_count: integerValue,
  count_confidence: stringValue,
  mapping_required: nullable(validMappingRequired),
  result_id: nullable(stringValue),
  rows: arrayOf(validProcurementImportRow),
  created_at: stringValue,
  completed_at: nullable(stringValue),
});

const validProcurementImportPage = exactObject<
  Schemas["ProcurementImportPage"]
>({
  items: arrayOf(validProcurementImport),
  next_cursor: nullable(stringValue),
});

const validProcurementImportCompose = exactObject<
  Schemas["ProcurementImportComposeResponse"]
>({
  import_id: stringValue,
  kind: stringValue,
  status: stringValue,
  result_id: stringValue,
  state: stringValue,
  row_version: integerValue,
});

const validJobCommand = exactObject<Schemas["JobCommandResponse"]>({
  id: stringValue,
  workspace_id: nullable(stringValue),
  type: stringValue,
  status: stringValue,
  stage: stringValue,
  progress_current: integerValue,
  progress_total: integerValue,
  error: nullable(validJobError),
  retry_count: integerValue,
});

const validQueuedJob = exactObject<Schemas["QueuedJobResponse"]>({
  job_id: stringValue,
  status: stringValue,
});

const validVersionedSource = exactObject<Schemas["SourceResponse"]>({
  id: stringValue,
  filename: stringValue,
  sha256: stringValue,
  size_bytes: integerValue,
  role: stringValue,
  status: stringValue,
  detected_format: nullable(stringValue),
  mapping: recordOf(stringValue),
  vendor_scope: stringValue,
  remember_template: booleanValue,
  requested_start_local_date: nullable(stringValue),
  requested_through_local_date: nullable(stringValue),
  parsed_config_version: nullable(integerValue),
  row_version: integerValue,
  created_at: stringValue,
  completed_at: nullable(stringValue),
  latest_job_id: nullable(stringValue),
  latest_result: nullable(validJobFileResult),
});

const validComparison = exactObject<Schemas["ComparisonJobResponse"]>({
  job_id: stringValue,
  status: stringValue,
  workspace_status: stringValue,
  row_version: integerValue,
});

const validCandidateLock = exactObject<Schemas["CandidateLockResponse"]>({
  candidate_id: stringValue,
  actor_id: stringValue,
  expires_at: stringValue,
});

const validCandidateMutation = exactObject<
  Schemas["CandidateMutationResponse"]
>({
  id: stringValue,
  outcome: stringValue,
  quantity: integerValue,
  unit_price: nullable(integerValue),
  row_version: integerValue,
});

const validApproval = exactObject<Schemas["ApprovalRequestResponse"]>({
  revision_id: stringValue,
  revision_number: integerValue,
  sha256: stringValue,
  expected_total_won: integerValue,
  budget_won: integerValue,
  candidate_collection_revision: integerValue,
  state: stringValue,
  row_version: integerValue,
});

const validApprovalDecision = exactObject<Schemas["ApprovalDecisionResponse"]>({
  revision_id: stringValue,
  decision: stringValue,
  state: stringValue,
  row_version: integerValue,
});

const validQuoteReconciliation = exactObject<Schemas["QuoteReconciliation"]>({
  duplicate_isbns: arrayOf(stringValue),
  price_conflicts: arrayOf(stringValue),
});

const validQuoteRow = exactObject<Schemas["QuoteRowResponse"]>({
  quote_row_id: stringValue,
  approval_row_id: nullable(stringValue),
  match_status: stringValue,
  isbn13: nullable(stringValue),
  title: stringValue,
  author: stringValue,
  publisher: nullable(stringValue),
  edition: nullable(stringValue),
  quantity: integerValue,
  unit_price: nullable(integerValue),
  list_price: nullable(integerValue),
  out_of_stock: booleanValue,
  line_total_won: integerValue,
});

const validQuote = exactObject<Schemas["QuoteResponse"]>({
  quote_id: stringValue,
  revision_number: integerValue,
  state: stringValue,
  row_version: integerValue,
  total_won: integerValue,
  list_total_won: integerValue,
  discount_won: integerValue,
  budget_overrun_won: integerValue,
  out_of_stock_count: integerValue,
  missing_price_count: integerValue,
  list_mismatch_count: integerValue,
  needs_review_count: integerValue,
  unmatched_count: integerValue,
  requires_reapproval: booleanValue,
  reconciliation: validQuoteReconciliation,
  rows: arrayOf(validQuoteRow),
});

const validOrderAllocation = exactObject<Schemas["OrderAllocation"]>({
  candidate_id: stringValue,
  quote_id: stringValue,
  quote_row_id: stringValue,
  vendor_name: stringValue,
  quantity: integerValue,
  unit_price: integerValue,
});

const validOrderArtifact = exactObject<Schemas["OrderArtifact"]>({
  vendor_name: stringValue,
  quote_id: stringValue,
  artifact_id: stringValue,
  sha256: stringValue,
  size_bytes: integerValue,
});

const validOverAllocation = exactObject<Schemas["OrderOverAllocation"]>({
  candidate_id: stringValue,
  expected: integerValue,
  allocated: integerValue,
});

const validOrderDiagnostics = exactObject<Schemas["OrderDiagnostics"]>({
  missing_candidate_ids: arrayOf(stringValue),
  duplicate_candidate_ids: arrayOf(stringValue),
  over_allocations: arrayOf(validOverAllocation),
  price_conflict_candidate_ids: arrayOf(stringValue),
  unmapped_quote_row_ids: arrayOf(stringValue),
});

const validOrder = exactObject<Schemas["OrderResponse"]>({
  status: stringValue,
  state: stringValue,
  row_version: integerValue,
  revision_id: nullable(stringValue),
  revision_number: nullable(integerValue),
  artifact_id: nullable(stringValue),
  sha256: nullable(stringValue),
  size_bytes: nullable(integerValue),
  artifacts: nullable(arrayOf(validOrderArtifact)),
  allocations: nullable(arrayOf(validOrderAllocation)),
  validation_id: nullable(stringValue),
  diagnostics: nullable(validOrderDiagnostics),
  external_send_performed: nullable(booleanValue),
});

const validOrderSent = exactObject<Schemas["OrderSentResponse"]>({
  transmission_id: stringValue,
  state: stringValue,
  row_version: integerValue,
  external_send_performed: booleanValue,
});

const validDifferenceDetails = exactObject<Schemas["ReceivingDifferenceDetails"]>({
  expected: nullable((value): value is string | number => stringValue(value) || finiteNumber(value)),
  received: nullable((value): value is string | number => stringValue(value) || finiteNumber(value)),
  isbn13: nullable(stringValue),
  title: nullable(stringValue),
  edition: nullable(stringValue),
  scanned: nullable(integerValue),
  scanned_quantity: nullable(integerValue),
});

const validDeliveryDifference = exactObject<Schemas["DeliveryDifferenceResult"]>({
  id: stringValue,
  kind: stringValue,
  details: validDifferenceDetails,
});

const validDelivery = exactObject<Schemas["DeliveryResponse"]>({
  delivery_batch_id: stringValue,
  delivery_number: integerValue,
  received_quantity: integerValue,
  differences: arrayOf(validDeliveryDifference),
  state: stringValue,
  row_version: integerValue,
});

const validScanSession = exactObject<Schemas["ScanSessionResponse"]>({
  session_id: stringValue,
  state: stringValue,
  row_version: integerValue,
});

const validScan = exactObject<Schemas["ScanResponse"]>({
  event_id: stringValue,
  code: stringValue,
  isbn13: nullable(stringValue),
  scanned_quantity: integerValue,
  order_row_id: nullable(stringValue),
});

const validDisposition = exactObject<Schemas["DifferenceDispositionResponse"]>({
  difference_id: stringValue,
  disposition: stringValue,
  row_version: integerValue,
});

const validWorkspaceState = exactObject<Schemas["WorkspaceStateResponse"]>({
  state: stringValue,
  row_version: integerValue,
});

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
      if (input.procurementKind) {
        form.set("procurement_kind", input.procurementKind);
      }
      if (input.targetRevisionId) {
        form.set("target_revision_id", input.targetRevisionId);
      }
      if (input.reason) {
        form.set("reason", input.reason);
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
              procurement_kind: input.procurementKind ?? null,
              target_revision_id: input.targetRevisionId ?? null,
              reason: input.reason ?? null,
            },
            validateResponse: validUpload,
          },
        )
      ).data;
    },
    getJob: async (jobId) =>
      (await request<Job>(`/api/v2/jobs/${jobId}`)).data,
    listProcurementImports: async (workspaceId) =>
      (
        await request<Schemas["ProcurementImportPage"]>(
          `/api/v2/workspaces/${workspaceId}/procurement-imports`,
          { validateResponse: validProcurementImportPage },
        )
      ).data,
    composeProcurementImport: async (importId, version) =>
      (
        await request<Schemas["ProcurementImportComposeResponse"]>(
          `/api/v2/procurement-imports/${importId}/compose`,
          {
            method: "POST",
            version,
            mutation: true,
            csrf: true,
            logicalAction: `procurement-imports:compose:${importId}`,
            logicalPayload: { import_id: importId, row_version: version },
            validateResponse: validProcurementImportCompose,
          },
        )
      ).data,
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
    listApprovals: async (workspaceId) =>
      await collectCursorPages(async (cursor) => {
        const query = new URLSearchParams({ limit: "100" });
        if (cursor) query.set("cursor", cursor);
        return (
          await request<Schemas["ApprovalPage"]>(
            `/api/v2/workspaces/${workspaceId}/approvals?${query.toString()}`,
          )
        ).data;
      }),
    getApproval: async (revisionId) =>
      (
        await request<Schemas["ApprovalDetailResponse"]>(
          `/api/v2/approvals/${revisionId}`,
        )
      ).data,
    approveApproval: async (revisionId, workspaceId, reason, version) =>
      (
        await request<Schemas["ApprovalDecisionResponse"]>(
          `/api/v2/approvals/${revisionId}/approve`,
          {
            method: "POST",
            body: { workspace_id: workspaceId, reason },
            version,
            mutation: true,
            csrf: true,
            logicalAction: `approvals:approve:${revisionId}`,
            logicalPayload: { workspace_id: workspaceId, reason, row_version: version },
            validateResponse: validApprovalDecision,
          },
        )
      ).data,
    requestApprovalChanges: async (revisionId, workspaceId, reason, version) =>
      (
        await request<Schemas["ApprovalDecisionResponse"]>(
          `/api/v2/approvals/${revisionId}/changes-request`,
          {
            method: "POST",
            body: { workspace_id: workspaceId, reason },
            version,
            mutation: true,
            csrf: true,
            logicalAction: `approvals:changes:${revisionId}`,
            logicalPayload: { workspace_id: workspaceId, reason, row_version: version },
            validateResponse: validApprovalDecision,
          },
        )
      ).data,
    listQuotes: async (workspaceId) =>
      await collectCursorPages(async (cursor) => {
        const query = new URLSearchParams({ limit: "100" });
        if (cursor) query.set("cursor", cursor);
        return (
          await request<Schemas["QuotePage"]>(
            `/api/v2/workspaces/${workspaceId}/quotes?${query.toString()}`,
          )
        ).data;
      }),
    getQuote: async (quoteId) =>
      (
        await request<Schemas["QuoteDetailResponse"]>(
          `/api/v2/quotes/${quoteId}`,
        )
      ).data,
    createQuote: async (workspaceId, input, version) =>
      (
        await request<Schemas["QuoteResponse"]>(
          `/api/v2/workspaces/${workspaceId}/quotes`,
          {
            method: "POST",
            body: input,
            version,
            mutation: true,
            csrf: true,
            logicalAction: `quotes:create:${workspaceId}`,
            logicalPayload: { ...input, row_version: version },
            validateResponse: validQuote,
          },
        )
      ).data,
    getCurrentOrder: async (workspaceId) =>
      (
        await request<Schemas["CurrentOrderResponse"]>(
          `/api/v2/workspaces/${workspaceId}/orders/current`,
        )
      ).data,
    createOrder: async (workspaceId, input, version) =>
      (
        await request<Schemas["OrderResponse"]>(
          `/api/v2/workspaces/${workspaceId}/orders`,
          {
            method: "POST",
            body: input,
            version,
            mutation: true,
            csrf: true,
            logicalAction: `orders:create:${workspaceId}`,
            logicalPayload: { ...input, row_version: version },
            validateResponse: validOrder,
          },
        )
      ).data,
    downloadOrder: async (orderRevisionId) => {
      const response = await fetch(
        `${baseUrl}/api/v2/orders/${orderRevisionId}/download`,
        { credentials: "include", headers: { Accept: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" } },
      );
      if (!response.ok) {
        let body: unknown;
        try {
          body = await response.json();
        } catch {
          body = undefined;
        }
        throw new ApiClientError(response.status, errorDetail(response.status, body));
      }
      const disposition = response.headers.get("Content-Disposition") ?? "";
      const filename = /filename="?([^";]+)"?/i.exec(disposition)?.[1] ?? `order-${orderRevisionId}.xlsx`;
      return { blob: await response.blob(), filename };
    },
    markOrderSent: async (orderRevisionId, workspaceId, reason, version) =>
      (
        await request<Schemas["OrderSentResponse"]>(
          `/api/v2/orders/${orderRevisionId}/sent`,
          {
            method: "POST",
            body: { workspace_id: workspaceId, reason },
            version,
            mutation: true,
            csrf: true,
            logicalAction: `orders:sent:${orderRevisionId}`,
            logicalPayload: { workspace_id: workspaceId, reason, row_version: version },
            validateResponse: validOrderSent,
          },
        )
      ).data,
    listDeliveries: async (workspaceId) =>
      (
        await request<Schemas["DeliveryPage"]>(
          `/api/v2/workspaces/${workspaceId}/deliveries?limit=100`,
        )
      ).data,
    listReceivingDifferences: async (workspaceId) =>
      await collectCursorPages(async (cursor) => {
        const query = new URLSearchParams({ limit: "100", active: "true" });
        if (cursor) query.set("cursor", cursor);
        return (
          await request<Schemas["ReceivingDifferencePage"]>(
            `/api/v2/workspaces/${workspaceId}/receiving/differences?${query.toString()}`,
          )
        ).data;
      }),
    getReceivingStatus: async (workspaceId) =>
      (
        await request<Schemas["ReceivingStatusResponse"]>(
          `/api/v2/workspaces/${workspaceId}/receiving/status`,
        )
      ).data,
    createDelivery: async (workspaceId, input, version) =>
      (
        await request<Schemas["DeliveryResponse"]>(
          `/api/v2/workspaces/${workspaceId}/deliveries`,
          {
            method: "POST",
            body: input,
            version,
            mutation: true,
            csrf: true,
            logicalAction: `deliveries:create:${workspaceId}`,
            logicalPayload: { ...input, row_version: version },
            validateResponse: validDelivery,
          },
        )
      ).data,
    startScanSession: async (workspaceId, input, version) =>
      (
        await request<Schemas["ScanSessionResponse"]>(
          `/api/v2/workspaces/${workspaceId}/scans/sessions`,
          {
            method: "POST",
            body: input,
            version,
            mutation: true,
            csrf: true,
            logicalAction: `scans:start:${workspaceId}`,
            logicalPayload: { ...input, row_version: version },
            validateResponse: validScanSession,
          },
        )
      ).data,
    recordScan: async (sessionId, input, command) =>
      (
        await request<Schemas["ScanResponse"]>(
          `/api/v2/scans/sessions/${sessionId}/events`,
          {
            method: "POST",
            body: input,
            mutation: true,
            csrf: true,
            command,
            logicalAction: `scans:event:${sessionId}`,
            logicalPayload: input,
            validateResponse: validScan,
          },
        )
      ).data,
    setReceivingDisposition: async (differenceId, input, version) =>
      (
        await request<Schemas["DifferenceDispositionResponse"]>(
          `/api/v2/receiving/differences/${differenceId}`,
          {
            method: "PATCH",
            body: input,
            version,
            mutation: true,
            csrf: true,
            logicalAction: `receiving:disposition:${differenceId}`,
            logicalPayload: { ...input, row_version: version },
            validateResponse: validDisposition,
          },
        )
      ).data,
    completeReceiving: async (workspaceId, reason, version) =>
      (
        await request<Schemas["WorkspaceStateResponse"]>(
          `/api/v2/workspaces/${workspaceId}/receiving/complete`,
          {
            method: "POST",
            body: { reason },
            version,
            mutation: true,
            csrf: true,
            logicalAction: `receiving:complete:${workspaceId}`,
            logicalPayload: { reason, row_version: version },
            validateResponse: validWorkspaceState,
          },
        )
      ).data,
  };
}
