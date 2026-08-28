import { afterEach, describe, expect, test, vi } from "vitest";

import {
  ApiClientError,
  createApiClient,
  OfflineMutationError,
  type SuseoroApi,
} from "../src/api/client";

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

  test("수정 파일 업로드는 교체할 source identity와 원래 역할을 multipart 계약에 보존한다", async () => {
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

    expect(outgoing?.get("role")).toBe("PURCHASE_REQUEST");
    expect(outgoing?.get("replacement_source_document_id")).toBe("source-original");
    expect((outgoing?.get("files") as File).name).toBe("corrected.csv");
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
          { budget_won: 50000, reason: "검토 완료" },
          1,
        ),
      success: {
        revision_id: "approval-1",
        revision_number: 1,
        sha256: "a".repeat(64),
        expected_total_won: 12000,
        budget_won: 50000,
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
