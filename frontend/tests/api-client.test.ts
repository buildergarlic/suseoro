import { afterEach, describe, expect, test, vi } from "vitest";

import {
  ApiClientError,
  createApiClient,
  OfflineMutationError,
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
});
