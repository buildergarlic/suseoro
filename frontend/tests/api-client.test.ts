import { afterEach, describe, expect, test, vi } from "vitest";

import {
  ApiClientError,
  createApiClient,
  OfflineMutationError,
  type SuseoroApi,
} from "../src/api/client";
import type { components } from "../src/api/types";

type Schemas = components["schemas"];

const validUserResponse = {
  id: "user-1",
  school_id: "school-1",
  username: "operator",
  display_name: "담당자",
  roles: ["OPERATOR"],
} satisfies Schemas["UserResponse"];

const validUploadResponse = {
  job_id: "job-upload",
  items: [
    {
      filename: "books.csv",
      status: "ACCEPTED",
      source_id: "source-upload",
      procurement_import_id: null,
      error: null,
      repair_obligation_id: null,
      repair_generation: null,
    },
  ],
} satisfies Schemas["UploadResponse"];

const validUploadErrorResponse = {
  job_id: null,
  items: [
    {
      filename: "broken.exe",
      status: "FAILED",
      source_id: null,
      procurement_import_id: null,
      error: { code: "UNSUPPORTED_FILE_TYPE", message: "지원하지 않는 형식입니다." },
      repair_obligation_id: "repair-1",
      repair_generation: 1,
    },
  ],
} satisfies Schemas["UploadResponse"];

const validJobCommandResponse = {
  id: "job-command",
  workspace_id: "workspace-1",
  type: "INGEST",
  status: "QUEUED",
  stage: "QUEUED",
  progress_current: 0,
  progress_total: 1,
  error: null,
  retry_count: 1,
} satisfies Schemas["JobCommandResponse"];

const validErroredJobCommandResponse = {
  ...validJobCommandResponse,
  status: "FAILED",
  stage: "FAILED",
  error: { type: "JobFailure", code: "JOB_FAILED", message: "다시 시도해 주세요." },
} satisfies Schemas["JobCommandResponse"];

const validProcurementImport = {
  import_id: "import-1",
  source_id: "source-1",
  kind: "QUOTE",
  target_revision_id: "approval-1",
  vendor_name: "푸른서점",
  status: "READY",
  filename: "quote.xlsx",
  detected_format: "XLSX",
  parser_version: "xlsx-v1",
  template_version: null,
  total_rows: 1,
  processed_rows: 1,
  row_error_count: 0,
  count_confidence: "EXACT",
  mapping_required: null,
  result_id: null,
  rows: [{
    source_row_id: "source-row-1",
    status: "SUCCESS",
    provenance: { sheet: "견적", source_row: 2 },
    error: null,
    result_row_id: null,
  }],
  created_at: "2026-08-29T00:00:00Z",
  completed_at: null,
} satisfies Schemas["ProcurementImportItem"];

const validProcurementCompose = {
  import_id: "import-1",
  kind: "QUOTE",
  status: "IMPORTED",
  result_id: "quote-1",
  state: "QUOTE_REVIEW",
  row_version: 2,
} satisfies Schemas["ProcurementImportComposeResponse"];

const validQueuedJobResponse = {
  job_id: "job-parse",
  status: "QUEUED",
} satisfies Schemas["QueuedJobResponse"];

const validSourceResponse = {
  id: "source-1",
  filename: "books.csv",
  sha256: "a".repeat(64),
  size_bytes: 12,
  role: "VENDOR_QUOTE",
  status: "PENDING",
  detected_format: "CSV",
  mapping: { 제목: "title" },
  vendor_scope: "vendor-a",
  remember_template: false,
  requested_start_local_date: null,
  requested_through_local_date: null,
  parsed_config_version: null,
  row_version: 2,
  created_at: "2026-08-29T00:00:00Z",
  completed_at: null,
  latest_job_id: null,
  latest_result: null,
} satisfies Schemas["SourceResponse"];

const validSourceWithResultResponse = {
  ...validSourceResponse,
  latest_job_id: "job-parse",
  latest_result: {
    source_document_id: "source-1",
    filename: "books.csv",
    status: "PARTIAL",
    total_rows: 1,
    processed_rows: 0,
    count_confidence: "EXACT",
    row_error_count: 0,
    error: { type: null, code: "MAPPING_REQUIRED", message: "연결이 필요합니다." },
    mapping_required: {
      headers: ["제목"],
      preview_rows: [["책"]],
      suggested_mapping: { 제목: null },
      required_fields: ["title"],
      confidence: 0.25,
      questions: ["제목 열을 골라 주세요."],
    },
  },
} satisfies Schemas["SourceResponse"];

const validComparisonResponse = {
  job_id: "job-compare",
  status: "QUEUED",
  workspace_status: "ANALYZING",
  row_version: 2,
} satisfies Schemas["ComparisonJobResponse"];

const validLockResponse = {
  candidate_id: "candidate-1",
  actor_id: "user-1",
  expires_at: "2026-08-29T00:01:00Z",
} satisfies Schemas["CandidateLockResponse"];

const validCandidateMutationResponse = {
  id: "candidate-1",
  outcome: "CANDIDATE",
  quantity: 2,
  unit_price: 12_000,
  row_version: 2,
} satisfies Schemas["CandidateMutationResponse"];

const validApprovalResponse = {
  revision_id: "revision-1",
  revision_number: 1,
  sha256: "b".repeat(64),
  expected_total_won: 24_000,
  budget_won: 30_000,
  candidate_collection_revision: 5,
  state: "AWAITING_APPROVAL",
  row_version: 3,
} satisfies Schemas["ApprovalRequestResponse"];

type MutationContractCase = {
  name: string;
  malformed: unknown;
  valid: unknown;
  invoke: (client: SuseoroApi) => Promise<unknown>;
};

const mutationContractCases: MutationContractCase[] = [
  {
    name: "login roles 원소",
    malformed: { ...validUserResponse, roles: [null] },
    valid: validUserResponse,
    invoke: (client) =>
      client.login({ school_id: "school-1", username: "operator", password: "pw" }),
  },
  {
    name: "upload item error",
    malformed: {
      ...validUploadResponse,
      items: validUploadResponse.items.map(({ error: _error, ...item }) => item),
    },
    valid: validUploadResponse,
    invoke: (client) =>
      client.uploadSources("workspace-1", {
        files: [new File(["제목\n책\n"], "books.csv", { type: "text/csv" })],
        role: "PURCHASE_REQUEST",
      }),
  },
  {
    name: "upload item repair obligation",
    malformed: {
      ...validUploadResponse,
      items: validUploadResponse.items.map(
        ({ repair_obligation_id: _repairObligationId, ...item }) => item,
      ),
    },
    valid: validUploadResponse,
    invoke: (client) =>
      client.uploadSources("workspace-1", {
        files: [new File(["제목\n책\n"], "books.csv", { type: "text/csv" })],
        role: "PURCHASE_REQUEST",
      }),
  },
  {
    name: "upload procurement import link",
    malformed: {
      ...validUploadResponse,
      items: validUploadResponse.items.map(
        ({ procurement_import_id: _procurementImportId, ...item }) => item,
      ),
    },
    valid: validUploadResponse,
    invoke: (client) =>
      client.uploadSources("workspace-1", {
        files: [new File(["제목\n책\n"], "books.csv", { type: "text/csv" })],
        role: "PURCHASE_REQUEST",
      }),
  },
  {
    name: "upload item repair generation",
    malformed: {
      ...validUploadResponse,
      items: validUploadResponse.items.map(
        ({ repair_generation: _repairGeneration, ...item }) => item,
      ),
    },
    valid: validUploadResponse,
    invoke: (client) =>
      client.uploadSources("workspace-1", {
        files: [new File(["제목\n책\n"], "books.csv", { type: "text/csv" })],
        role: "PURCHASE_REQUEST",
      }),
  },
  {
    name: "upload nested error message",
    malformed: {
      ...validUploadErrorResponse,
      items: validUploadErrorResponse.items.map((item) => ({
        ...item,
        error: { code: item.error.code },
      })),
    },
    valid: validUploadErrorResponse,
    invoke: (client) =>
      client.uploadSources("workspace-1", {
        files: [new File(["broken"], "broken.exe")],
        role: "UNKNOWN",
      }),
  },
  {
    name: "retry job error",
    malformed: (({ error: _error, ...value }) => value)(validJobCommandResponse),
    valid: validJobCommandResponse,
    invoke: (client) => client.retryJob("job-command"),
  },
  {
    name: "cancel job workspace",
    malformed: (({ workspace_id: _workspaceId, ...value }) => value)(
      validJobCommandResponse,
    ),
    valid: validJobCommandResponse,
    invoke: (client) => client.cancelJob("job-command"),
  },
  {
    name: "job nested nullable error type",
    malformed: {
      ...validErroredJobCommandResponse,
      error: { code: "JOB_FAILED", message: "다시 시도해 주세요." },
    },
    valid: validErroredJobCommandResponse,
    invoke: (client) => client.retryJob("job-command-error"),
  },
  {
    name: "parse unexpected property",
    malformed: { ...validQueuedJobResponse, unexpected: true },
    valid: validQueuedJobResponse,
    invoke: (client) => client.parseSource("source-1"),
  },
  {
    name: "mapping truncated source",
    malformed: {
      id: validSourceResponse.id,
      role: validSourceResponse.role,
      mapping: validSourceResponse.mapping,
      row_version: validSourceResponse.row_version,
    },
    valid: validSourceResponse,
    invoke: (client) =>
      client.updateSourceMapping(
        "source-1",
        {
          role: "VENDOR_QUOTE",
          vendor_scope: "vendor-a",
          mapping: { 제목: "title" },
          remember_template: false,
        },
        1,
      ),
  },
  {
    name: "mapping nested latest result",
    malformed: {
      ...validSourceWithResultResponse,
      latest_result: (({ mapping_required: _mappingRequired, ...result }) => result)(
        validSourceWithResultResponse.latest_result,
      ),
    },
    valid: validSourceWithResultResponse,
    invoke: (client) =>
      client.updateSourceMapping(
        "source-1",
        {
          role: "VENDOR_QUOTE",
          vendor_scope: "vendor-a",
          mapping: { 제목: "title" },
          remember_template: false,
        },
        1,
      ),
  },
  {
    name: "comparison fractional version",
    malformed: { ...validComparisonResponse, row_version: 2.5 },
    valid: validComparisonResponse,
    invoke: (client) => client.createComparisonJob("workspace-1", [], 1),
  },
  {
    name: "candidate lock unexpected property",
    malformed: { ...validLockResponse, unexpected: true },
    valid: validLockResponse,
    invoke: (client) => client.lockCandidate("candidate-1", "workspace-1"),
  },
  {
    name: "candidate fractional quantity",
    malformed: { ...validCandidateMutationResponse, quantity: 1.5 },
    valid: validCandidateMutationResponse,
    invoke: (client) =>
      client.updateCandidate(
        "candidate-1",
        { workspace_id: "workspace-1", changes: { quantity: 2 }, reason: "둘" },
        1,
      ),
  },
  {
    name: "approval fractional budget",
    malformed: { ...validApprovalResponse, budget_won: 30_000.5 },
    valid: validApprovalResponse,
    invoke: (client) =>
      client.requestApproval(
        "workspace-1",
        { budget_won: 30_000, candidate_collection_revision: 5, reason: "승인 요청" },
        2,
      ),
  },
  {
    name: "procurement compose unexpected property",
    malformed: { ...validProcurementCompose, unexpected: true },
    valid: validProcurementCompose,
    invoke: (client) => client.composeProcurementImport("import-1", 1),
  },
];

const originalOnline = Object.getOwnPropertyDescriptor(
  window.navigator,
  "onLine",
);

afterEach(() => {
  vi.unstubAllGlobals();
  document.cookie = "suseoro_csrf=; Max-Age=0; path=/";
  if (originalOnline) {
    Object.defineProperty(window.navigator, "onLine", originalOnline);
  } else {
    Object.defineProperty(window.navigator, "onLine", {
      configurable: true,
      value: true,
    });
  }
});

describe("생성 계약을 쓰는 API client", () => {
  test("cookie session·CSRF·idempotency·If-Match와 ETag를 한 요청 경계에서 처리한다", async () => {
    document.cookie = "suseoro_csrf=csrf%20token; path=/";
    let outgoing: Request | undefined;
    vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
      const resolved =
        typeof input === "string"
          ? new URL(input, window.location.href)
          : input;
      outgoing = new Request(resolved, init);
      return new Response(
        JSON.stringify({
          id: "candidate-1",
          outcome: "CANDIDATE",
          quantity: 2,
          unit_price: 12000,
          row_version: 4,
        }),
        {
          status: 200,
          headers: { "Content-Type": "application/json", ETag: '"4"' },
        },
      );
    });
    const client = createApiClient();

    const result = await client.updateCandidate(
      "candidate-1",
      {
        workspace_id: "workspace-1",
        changes: { quantity: 2 },
        reason: "수량 변경",
      },
      3,
      {
        commandKey: "550e8400-e29b-41d4-a716-446655440201",
        requestId: "550e8400-e29b-41d4-a716-446655440202",
      },
    );

    expect(outgoing).toBeDefined();
    expect(outgoing?.credentials).toBe("include");
    expect(outgoing?.headers.get("X-CSRF-Token")).toBe("csrf token");
    expect(outgoing?.headers.get("Idempotency-Key")).toBe(
      "550e8400-e29b-41d4-a716-446655440201",
    );
    expect(outgoing?.headers.get("X-Request-ID")).toBe(
      "550e8400-e29b-41d4-a716-446655440202",
    );
    expect(outgoing?.headers.get("If-Match")).toBe('"3"');
    expect(result.etag).toBe('"4"');
    expect(result.data.quantity).toBe(2);
  });

  test("412 구조화 오류를 현재/제출 버전과 함께 보존한다", async () => {
    document.cookie = "suseoro_csrf=token; path=/";
    vi.stubGlobal("fetch", async () =>
      new Response(
        JSON.stringify({
          detail: {
            code: "ROW_VERSION_CONFLICT",
            message: "다른 사용자가 먼저 수정했습니다. 현재 내용과 변경 내용을 확인해 주세요.",
            request_id: "550e8400-e29b-41d4-a716-446655440203",
            fields: [
              { field: "current", value: 4 },
              { field: "submitted", value: 3 },
            ],
          },
        }),
        { status: 412, headers: { "Content-Type": "application/json" } },
      ),
    );
    const client = createApiClient();

    const failure = await client
      .updateCandidate(
        "candidate-1",
        {
          workspace_id: "workspace-1",
          changes: { quantity: 2 },
          reason: "수량 변경",
        },
        3,
      )
      .catch((error: unknown) => error);

    expect(failure).toBeInstanceOf(ApiClientError);
    expect((failure as ApiClientError).status).toBe(412);
    expect((failure as ApiClientError).detail.fields).toEqual([
      { field: "current", value: 4 },
      { field: "submitted", value: 3 },
    ]);
  });

  test("오프라인 상태 변경을 막고 호출자가 가진 입력 객체를 바꾸지 않는다", async () => {
    Object.defineProperty(window.navigator, "onLine", {
      configurable: true,
      value: false,
    });
    const input = {
      workspace_id: "workspace-1",
      changes: { quantity: 7 },
      reason: "수량 변경",
    };
    const snapshot = structuredClone(input);
    const client = createApiClient();

    const failure = await client
      .updateCandidate("candidate-1", input, 3)
      .catch((error: unknown) => error);

    expect(failure).toBeInstanceOf(OfflineMutationError);
    expect(input).toEqual(snapshot);
  });

  test("후보 cursor·limit·검색을 서버 요청에 보존하고 단건 ETag를 읽는다", async () => {
    const outgoing: URL[] = [];
    vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = new Request(
        typeof input === "string" ? new URL(input, window.location.href) : input,
        init,
      );
      outgoing.push(new URL(request.url));
      if (request.url.includes("/candidates/candidate-1")) {
        return new Response(
          JSON.stringify({
            id: "candidate-1",
            workspace_id: "workspace-1",
            title: "현재 책",
            authors: ["저자"],
            isbn13: null,
            edition: null,
            outcome: "CANDIDATE",
            reason: "현재 내용",
            quantity: 5,
            unit_price: 10000,
            row_version: 7,
            updated_at: "2026-08-29T08:00:00Z",
          }),
          { status: 200, headers: { "Content-Type": "application/json", ETag: '"7"' } },
        );
      }
      return new Response(
        JSON.stringify({
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
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    });
    const client = createApiClient();

    await client.listCandidates("workspace-1", {
      outcome: "CANDIDATE",
      search: "마법 학교 & ISBN",
      cursor: "next+/=",
      limit: 37,
    });
    const current = await client.getCandidate("candidate-1", "workspace-1");

    expect(outgoing[0]?.searchParams.get("outcome")).toBe("CANDIDATE");
    expect(outgoing[0]?.searchParams.get("search")).toBe("마법 학교 & ISBN");
    expect(outgoing[0]?.searchParams.get("cursor")).toBe("next+/=");
    expect(outgoing[0]?.searchParams.get("limit")).toBe("37");
    expect(outgoing[1]?.searchParams.get("workspace_id")).toBe("workspace-1");
    expect(current.etag).toBe('"7"');
    expect(current.data.quantity).toBe(5);
  });

  test("수정 파일 업로드는 source identity만 보내고 원래 설정은 서버가 이어받는다", async () => {
    let outgoing: FormData | undefined;
    vi.stubGlobal("fetch", async (_input: RequestInfo | URL, init?: RequestInit) => {
      outgoing = init?.body as FormData;
      return new Response(
        JSON.stringify({
          job_id: "job-replacement",
          items: [
            {
              filename: "corrected.csv",
              status: "ACCEPTED",
              source_id: "source-corrected",
              procurement_import_id: null,
              error: null,
              repair_obligation_id: null,
              repair_generation: null,
            },
          ],
        }),
        { status: 202, headers: { "Content-Type": "application/json" } },
      );
    });
    const client = createApiClient();

    await client.uploadSources("workspace-1", {
      files: [new File(["제목\n수정한 책\n"], "corrected.csv", { type: "text/csv" })],
      role: "PURCHASE_REQUEST",
      replacementSourceId: "source-original",
    });

    expect(outgoing?.get("role")).toBeNull();
    expect(outgoing?.get("vendor_scope")).toBeNull();
    expect(outgoing?.get("requested_start_local_date")).toBeNull();
    expect(outgoing?.get("requested_through_local_date")).toBeNull();
    expect(outgoing?.get("replacement_source_document_id")).toBe("source-original");
    expect((outgoing?.get("files") as File).name).toBe("corrected.csv");
  });

  test("견적 파일 intent와 durable 결과 조회·조합 계약을 그대로 보낸다", async () => {
    const requests: Request[] = [];
    let uploadForm: FormData | undefined;
    vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = new Request(
        typeof input === "string" ? new URL(input, window.location.href) : input,
        init,
      );
      requests.push(request);
      if (request.method === "POST" && request.url.includes("/sources")) {
        uploadForm = init?.body as FormData;
        return new Response(JSON.stringify({
          job_id: "job-1",
          items: [{
            ...validUploadResponse.items[0],
            procurement_import_id: "import-1",
          }],
        }), { status: 202, headers: { "Content-Type": "application/json" } });
      }
      if (request.method === "POST") {
        return new Response(JSON.stringify(validProcurementCompose), {
          status: 201,
          headers: { "Content-Type": "application/json" },
        });
      }
      return new Response(JSON.stringify({
        items: [validProcurementImport], next_cursor: null,
      }), { status: 200, headers: { "Content-Type": "application/json" } });
    });
    const client = createApiClient();

    await client.uploadSources("workspace-1", {
      files: [new File(["xlsx"], "quote.xlsx")],
      role: "VENDOR_QUOTE",
      vendorScope: "푸른서점",
      procurementKind: "QUOTE",
      targetRevisionId: "approval-1",
      reason: "견적 비교",
    });
    const imports = await client.listProcurementImports("workspace-1");
    const composed = await client.composeProcurementImport("import-1", 1);

    expect(uploadForm?.get("procurement_kind")).toBe("QUOTE");
    expect(uploadForm?.get("target_revision_id")).toBe("approval-1");
    expect(uploadForm?.get("reason")).toBe("견적 비교");
    expect(imports.items[0]?.rows[0]?.provenance).toEqual({ sheet: "견적", source_row: 2 });
    expect(requests[2]?.headers.get("If-Match")).toBe('"1"');
    expect(requests[2]?.headers.get("Idempotency-Key")).toBeTruthy();
    expect(composed.result_id).toBe("quote-1");
  });

  const retryableActions: Array<{
    name: string;
    run: (client: SuseoroApi) => Promise<unknown>;
    success: unknown;
  }> = [
    {
      name: "login",
      run: (client) => client.login({ school_id: "school", username: "user", password: "secret" }),
      success: {
        id: "user-1",
        school_id: "school-1",
        username: "user",
        display_name: "사서",
        roles: ["OPERATOR"],
      },
    },
    {
      name: "upload",
      run: (client) =>
        client.uploadSources("workspace-1", {
          files: [new File(["title\nbook\n"], "books.csv", { type: "text/csv" })],
          role: "PURCHASE_REQUEST",
        }),
      success: { job_id: "job-1", items: [] },
    },
    {
      name: "autosave",
      run: (client) =>
        client.updateCandidate(
          "candidate-1",
          {
            workspace_id: "workspace-1",
            changes: { quantity: 2 },
            reason: "수량 변경",
          },
          1,
        ),
      success: {
        id: "candidate-1",
        outcome: "CANDIDATE",
        quantity: 2,
        unit_price: 12000,
        row_version: 2,
      },
    },
    {
      name: "approve",
      run: (client) =>
        client.requestApproval(
          "workspace-1",
          { budget_won: 50000, candidate_collection_revision: 5, reason: "검토 완료" },
          1,
        ),
      success: {
        revision_id: "approval-1",
        revision_number: 1,
        sha256: "a".repeat(64),
        expected_total_won: 12000,
        budget_won: 50000,
        candidate_collection_revision: 5,
        state: "APPROVAL_PENDING",
        row_version: 2,
      },
    },
    {
      name: "retry",
      run: (client) => client.retryJob("job-1"),
      success: {
        id: "job-1",
        workspace_id: "workspace-1",
        type: "INGEST",
        status: "QUEUED",
        stage: "QUEUED",
        progress_current: 0,
        progress_total: 1,
        error: null,
        retry_count: 1,
      },
    },
    {
      name: "cancel",
      run: (client) => client.cancelJob("job-1"),
      success: {
        id: "job-1",
        workspace_id: "workspace-1",
        type: "INGEST",
        status: "CANCEL_REQUESTED",
        stage: "PARSING",
        progress_current: 0,
        progress_total: 1,
        error: null,
        retry_count: 0,
      },
    },
  ];

  test.each(retryableActions)(
    "$name 논리 행동은 retryable 응답 동안 같은 idempotency key를 유지한다",
    async ({ run, success }) => {
      const keys: string[] = [];
      let call = 0;
      vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
        call += 1;
        const request = new Request(
          typeof input === "string" ? new URL(input, window.location.href) : input,
          init,
        );
        keys.push(request.headers.get("Idempotency-Key") ?? "");
        if (call === 1) {
          return new Response(
            JSON.stringify({
              detail: {
                code: "UPLOAD_RESERVATION_BUSY",
                message: "다른 저장 작업이 진행 중입니다. 잠시 후 다시 시도해 주세요.",
                request_id: "550e8400-e29b-41d4-a716-446655440300",
                fields: [],
              },
            }),
            {
              status: 503,
              headers: { "Content-Type": "application/json", "Retry-After": "0" },
            },
          );
        }
        return new Response(JSON.stringify(success), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      });
      const client = createApiClient();

      await run(client).catch(() => undefined);
      await run(client);
      await run(client);

      expect(keys[0]).toBeTruthy();
      expect(keys[1]).toBe(keys[0]);
      expect(keys[2]).not.toBe(keys[1]);
    },
  );

  test("retryable autosave 중 payload가 바뀌면 새 논리 행동 key를 쓴다", async () => {
    const keys: string[] = [];
    let call = 0;
    vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
      call += 1;
      const request = new Request(
        typeof input === "string" ? new URL(input, window.location.href) : input,
        init,
      );
      keys.push(request.headers.get("Idempotency-Key") ?? "");
      if (call === 1) {
        throw new TypeError("network disconnected after send");
      }
      return new Response(
        JSON.stringify({
          id: "candidate-1",
          outcome: "CANDIDATE",
          quantity: 3,
          unit_price: 12000,
          row_version: 2,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    });
    const client = createApiClient();
    await client
      .updateCandidate(
        "candidate-1",
        { workspace_id: "workspace-1", changes: { quantity: 2 }, reason: "둘" },
        1,
      )
      .catch(() => undefined);
    await client.updateCandidate(
      "candidate-1",
      { workspace_id: "workspace-1", changes: { quantity: 3 }, reason: "셋" },
      1,
    );

    expect(keys[0]).toBeTruthy();
    expect(keys[1]).not.toBe(keys[0]);
  });

  test("읽을 수 없는 2xx 응답은 성공으로 확정하지 않고 같은 논리 행동 key를 보존한다", async () => {
    const keys: string[] = [];
    let call = 0;
    vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
      call += 1;
      const request = new Request(
        typeof input === "string" ? new URL(input, window.location.href) : input,
        init,
      );
      keys.push(request.headers.get("Idempotency-Key") ?? "");
      if (call === 1) {
        return new Response("<html>upstream response was truncated", {
          status: 200,
          headers: { "Content-Type": "text/html" },
        });
      }
      return new Response(
        JSON.stringify({
          id: "candidate-1",
          outcome: "CANDIDATE",
          quantity: 2,
          unit_price: 12_000,
          row_version: 2,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    });
    const client = createApiClient();
    const action = () =>
      client.updateCandidate(
        "candidate-1",
        { workspace_id: "workspace-1", changes: { quantity: 2 }, reason: "둘" },
        1,
      );

    await expect(action()).rejects.toThrow("서버 응답");
    await action();

    expect(keys[0]).toBeTruthy();
    expect(keys[1]).toBe(keys[0]);
  });

  test.each(mutationContractCases)(
    "$name 계약 위반 2xx는 같은 논리 행동 key로만 재확인한다",
    async ({ malformed, valid, invoke }) => {
      const keys: string[] = [];
      let call = 0;
      vi.stubGlobal(
        "fetch",
        async (input: RequestInfo | URL, init?: RequestInit) => {
          const request = new Request(
            typeof input === "string"
              ? new URL(input, window.location.href)
              : input,
            init,
          );
          keys.push(request.headers.get("Idempotency-Key") ?? "");
          call += 1;
          return new Response(JSON.stringify(call === 1 ? malformed : valid), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          });
        },
      );
      const client = createApiClient();

      await expect(invoke(client)).rejects.toThrow("서버 응답");
      await invoke(client);
      await invoke(client);

      expect(keys[0]).toBeTruthy();
      expect(keys[1]).toBe(keys[0]);
      expect(keys[2]).not.toBe(keys[1]);
    },
  );

  test("필수 필드가 빠진 JSON 2xx도 모호한 응답으로 보고 같은 업로드 key를 보존한다", async () => {
    const keys: string[] = [];
    let call = 0;
    vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = new Request(
        typeof input === "string" ? new URL(input, window.location.href) : input,
        init,
      );
      keys.push(request.headers.get("Idempotency-Key") ?? "");
      call += 1;
      return new Response(
        JSON.stringify(
          call === 1
            ? {}
            : {
                job_id: "job-valid",
                items: [
                  {
                    filename: "books.csv",
                    status: "ACCEPTED",
                    source_id: "source-valid",
                    procurement_import_id: null,
                    error: null,
                    repair_obligation_id: null,
                    repair_generation: null,
                  },
                ],
              },
        ),
        { status: 202, headers: { "Content-Type": "application/json" } },
      );
    });
    const client = createApiClient();
    const file = new File(["제목\n책\n"], "books.csv", { type: "text/csv" });
    const action = () =>
      client.uploadSources("workspace-1", {
        files: [file],
        role: "PURCHASE_REQUEST",
      });

    await expect(action()).rejects.toThrow("서버 응답");
    await action();

    expect(keys[0]).toBeTruthy();
    expect(keys[1]).toBe(keys[0]);
  });

  test("자료·교체 의무·작업 목록은 next_cursor가 끝날 때까지 모두 모은다", async () => {
    const requested = { sources: [] as string[], repairs: [] as string[], jobs: [] as string[] };
    vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = new Request(
        typeof input === "string" ? new URL(input, window.location.href) : input,
        init,
      );
      const url = new URL(request.url);
      const cursor = url.searchParams.get("cursor");
      const kind = url.pathname.endsWith("/sources")
        ? "sources"
        : url.pathname.endsWith("/upload-repairs")
          ? "repairs"
          : "jobs";
      requested[kind].push(cursor ?? "FIRST");
      const id = `${kind}-${cursor ?? "first"}`;
      const item =
        kind === "sources"
          ? {
              id,
              filename: `${id}.csv`,
              sha256: "a".repeat(64),
              size_bytes: 1,
              role: "PURCHASE_REQUEST",
              status: "SUCCESS",
              detected_format: "CSV",
              mapping: {},
              row_version: 1,
              created_at: "2026-08-29T00:00:00Z",
              completed_at: "2026-08-29T00:00:01Z",
              latest_job_id: null,
              latest_result: null,
            }
          : kind === "repairs"
            ? {
                id,
                filename: `${id}.csv`,
                error: { code: "FILE_TOO_LARGE", message: "파일을 확인해 주세요." },
                status: "UNRESOLVED",
                generation: 0,
                role: "PURCHASE_REQUEST",
                resolved_source_id: null,
                created_at: "2026-08-29T00:00:00Z",
                updated_at: "2026-08-29T00:00:00Z",
              }
            : {
                id,
                workspace_id: "workspace-1",
                type: "INGEST",
                status: "SUCCEEDED",
                stage: "COMPLETED",
                progress_current: 1,
                progress_total: 1,
                error: null,
                retry_count: 0,
                items: [],
              };
      return new Response(
        JSON.stringify({
          items: [item],
          next_cursor: cursor ? null : `${kind}-next`,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    });
    const client = createApiClient();

    const [sources, repairs, jobs] = await Promise.all([
      client.listSources("workspace-1"),
      client.listUploadRepairs("workspace-1"),
      client.listWorkspaceJobs("workspace-1"),
    ]);

    expect(sources.items).toHaveLength(2);
    expect(repairs.items).toHaveLength(2);
    expect(jobs.items).toHaveLength(2);
    expect(requested).toEqual({
      sources: ["FIRST", "sources-next"],
      repairs: ["FIRST", "repairs-next"],
      jobs: ["FIRST", "jobs-next"],
    });
    expect(sources.next_cursor).toBeNull();
    expect(repairs.next_cursor).toBeNull();
    expect(jobs.next_cursor).toBeNull();
  });

  test("비교 시작은 브라우저가 센 source 목록 대신 서버 권위 집합을 요청한다", async () => {
    let body: unknown;
    vi.stubGlobal("fetch", async (_input: RequestInfo | URL, init?: RequestInit) => {
      if (typeof init?.body !== "string") throw new TypeError("JSON body required");
      body = JSON.parse(init.body);
      return new Response(
        JSON.stringify({
          job_id: "compare-1",
          status: "QUEUED",
          workspace_status: "ANALYZING",
          row_version: 2,
        }),
        { status: 202, headers: { "Content-Type": "application/json" } },
      );
    });
    const client = createApiClient();

    await client.createComparisonJob(
      "workspace-1",
      Array.from({ length: 101 }, (_, index) => `source-${index}`),
      1,
    );

    expect(body).toEqual({});
  });

  test("동시 요청의 읽을 수 없는 응답은 뒤늦은 성공이 와도 해당 caller 재시도 key를 보존한다", async () => {
    const keys: string[] = [];
    let malformed!: (response: Response) => void;
    let success!: (response: Response) => void;
    let call = 0;
    vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = new Request(
        typeof input === "string" ? new URL(input, window.location.href) : input,
        init,
      );
      keys.push(request.headers.get("Idempotency-Key") ?? "");
      call += 1;
      if (call === 1) return await new Promise<Response>((resolve) => (malformed = resolve));
      if (call === 2) return await new Promise<Response>((resolve) => (success = resolve));
      return new Response(
        JSON.stringify({
          id: "candidate-1",
          outcome: "CANDIDATE",
          quantity: call === 4 ? 3 : 2,
          unit_price: 12_000,
          row_version: call === 4 ? 3 : 2,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    });
    const client = createApiClient();
    const input = {
      workspace_id: "workspace-1",
      changes: { quantity: 2 },
      reason: "동시 저장",
    };
    const first = client.updateCandidate("candidate-1", input, 1);
    const second = client.updateCandidate("candidate-1", input, 1);
    await vi.waitFor(() => expect(keys).toHaveLength(2));
    malformed(new Response("truncated", { status: 200 }));
    await expect(first).rejects.toThrow("서버 응답");
    success(
      new Response(
        JSON.stringify({
          id: "candidate-1",
          outcome: "CANDIDATE",
          quantity: 2,
          unit_price: 12_000,
          row_version: 2,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    await second;
    await client.updateCandidate("candidate-1", input, 1);
    await client.updateCandidate(
      "candidate-1",
      { ...input, changes: { quantity: 3 } },
      2,
    );

    expect(keys[0]).toBeTruthy();
    expect(keys[1]).toBe(keys[0]);
    expect(keys[2]).toBe(keys[0]);
    expect(keys[3]).not.toBe(keys[2]);
  });
});
