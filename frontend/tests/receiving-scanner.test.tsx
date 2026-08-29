import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, test, vi } from "vitest";

import type { SuseoroApi } from "../src/api/client";
import { BarcodeScanner } from "../src/features/receiving/BarcodeScanner";
import { ReceivingPanel } from "../src/features/receiving/ReceivingPanel";
import { SCAN_AUDIO_PATTERNS } from "../src/features/receiving/scanFeedback";
import { createFixtureApi, operator, workspace } from "../src/test/fixtures";

function apiWith(overrides: Partial<SuseoroApi>): SuseoroApi {
  return { ...createFixtureApi(), ...overrides };
}

describe("바코드 집중 모드", () => {
  test("네 결과는 색상과 별개로 서로 다른 음향 pattern을 가진다", () => {
    expect(Object.keys(SCAN_AUDIO_PATTERNS)).toEqual([
      "NORMAL", "OVER", "NOT_ORDERED", "EDITION_MISMATCH",
    ]);
    expect(new Set(Object.values(SCAN_AUDIO_PATTERNS).map((item) => item.join(","))).size).toBe(4);
  });

  test("mount와 모든 Enter 스캔 뒤 초점을 유지하고 스캔 사이 클릭이 없다", async () => {
    const user = userEvent.setup();
    const results = ["NORMAL", "OVER", "UNORDERED", "EDITION_MISMATCH"] as const;
    let scanCount = 0;
    const scan = vi.fn<SuseoroApi["recordScan"]>(async () => {
      scanCount += 1;
      return {
      event_id: `event-${scanCount}`,
      code: results[scanCount - 1],
      isbn13: "9788937464010",
      scanned_quantity: scanCount,
      order_row_id: "order-row-1",
      };
    });
    const clicks = vi.fn();
    document.addEventListener("click", clicks);
    render(
      <BarcodeScanner
        api={apiWith({ recordScan: scan })}
        onProgress={vi.fn()}
        orderRows={[{ order_row_id: "order-row-1", isbn13: "9788937464010", title: "책", edition: "개정판", ordered_quantity: 1, delivered_quantity: 0, scanned_quantity: 0, unit_price: 10_000 }]}
        sessionId="scan-session"
        workspaceId="workspace-receiving"
      />,
    );
    const input = screen.getByLabelText("ISBN 바코드");
    await waitFor(() => expect(input).toHaveFocus());
    const clicksBefore = clicks.mock.calls.length;
    for (const isbn of ["978-89-3746-401-0", "9788937464010", "9788937464999", "9788937464883"]) {
      await user.keyboard(`${isbn}{Enter}`);
      await waitFor(() => expect(input).toHaveFocus());
    }
    expect(clicks.mock.calls.length).toBe(clicksBefore);
    expect(scan).toHaveBeenCalledTimes(4);
    expect(scan.mock.calls[0]?.[1].isbn).toBe("9788937464010");
    expect(scan.mock.calls[1]?.[1].isbn).toBe("9788937464010");
    expect(scan.mock.calls[0]?.[2]?.commandKey).not.toBe(
      scan.mock.calls[1]?.[2]?.commandKey,
    );
    expect(screen.getByRole("status")).toHaveTextContent("판본 차이");
    document.removeEventListener("click", clicks);
  });

  test("불확실한 실패 재시도는 같은 command key를 보존하고 초점을 되돌린다", async () => {
    const user = userEvent.setup();
    const scan = vi.fn<SuseoroApi["recordScan"]>()
      .mockRejectedValueOnce(new Error("연결을 다시 확인해 주세요."))
      .mockResolvedValueOnce({
        event_id: "event-retry", code: "NORMAL", isbn13: "9788937464010",
        scanned_quantity: 1, order_row_id: "order-row-1",
      });
    render(
      <BarcodeScanner
        api={apiWith({ recordScan: scan })}
        onProgress={vi.fn()}
        orderRows={[]}
        sessionId="scan-session"
        workspaceId="workspace-receiving"
      />,
    );
    const input = screen.getByLabelText("ISBN 바코드");
    await waitFor(() => expect(input).toHaveFocus());
    await user.keyboard("978-89-3746-401-0{Enter}");
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("다시 확인"));
    expect(input).toHaveValue("9788937464010");
    expect(input).toHaveFocus();
    await user.keyboard("{Enter}");
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("확인됨"));
    expect(scan.mock.calls[0]?.[2]?.commandKey).toBe(
      scan.mock.calls[1]?.[2]?.commandKey,
    );
    expect(input).toHaveFocus();
  });

  test("이전 응답이 다음 바코드 입력 중 도착해도 입력 앞자리를 덮어쓰지 않는다", async () => {
    const user = userEvent.setup();
    let resolveFirst!: (value: Awaited<ReturnType<SuseoroApi["recordScan"]>>) => void;
    const first = new Promise<Awaited<ReturnType<SuseoroApi["recordScan"]>>>(
      (resolve) => { resolveFirst = resolve; },
    );
    const scan = vi.fn<SuseoroApi["recordScan"]>()
      .mockImplementationOnce(async () => await first)
      .mockResolvedValueOnce({
        event_id: "event-second", code: "NORMAL", isbn13: "9788936434267",
        scanned_quantity: 1, order_row_id: "order-row-2",
      });
    render(
      <BarcodeScanner
        api={apiWith({ recordScan: scan })}
        onProgress={vi.fn()}
        orderRows={[]}
        sessionId="scan-session"
        workspaceId="workspace-receiving"
      />,
    );
    const input = screen.getByLabelText("ISBN 바코드");
    await waitFor(() => expect(input).toHaveFocus());
    await user.keyboard("9788937464010{Enter}");
    await user.keyboard("97889364");
    resolveFirst({
      event_id: "event-first", code: "NORMAL", isbn13: "9788937464010",
      scanned_quantity: 1, order_row_id: "order-row-1",
    });
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("확인됨"));
    await user.keyboard("34267{Enter}");

    await waitFor(() => expect(scan).toHaveBeenCalledTimes(2));
    expect(scan.mock.calls[1]?.[1].isbn).toBe("9788936434267");
  });

  test("이전 실패가 이미 대기 중인 다음 스캔을 덮거나 다시 제출하지 않는다", async () => {
    const user = userEvent.setup();
    let rejectFirst!: (reason: Error) => void;
    const first = new Promise<Awaited<ReturnType<SuseoroApi["recordScan"]>>>(
      (_resolve, reject) => { rejectFirst = reject; },
    );
    const scan = vi.fn<SuseoroApi["recordScan"]>()
      .mockImplementationOnce(async () => await first)
      .mockResolvedValueOnce({
        event_id: "event-second", code: "NORMAL", isbn13: "9788936434267",
        scanned_quantity: 1, order_row_id: "order-row-2",
      });
    render(
      <BarcodeScanner
        api={apiWith({ recordScan: scan })}
        onProgress={vi.fn()}
        orderRows={[]}
        sessionId="scan-session"
        workspaceId="workspace-receiving"
      />,
    );
    const input = screen.getByLabelText("ISBN 바코드");
    await waitFor(() => expect(input).toHaveFocus());
    await user.keyboard("9788937464010{Enter}");
    await user.keyboard("9788936434267{Enter}");
    rejectFirst(new Error("첫 스캔 응답을 확인하지 못했습니다."));

    await waitFor(() => expect(scan).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("확인됨"));
    expect(input).toHaveValue("");
    await user.keyboard("{Enter}");
    await new Promise((resolve) => window.setTimeout(resolve, 0));
    expect(scan).toHaveBeenCalledTimes(2);
    expect(scan.mock.calls[1]?.[1].isbn).toBe("9788936434267");
  });
});

describe("부분 납품과 차이 처리", () => {
  test("납품 파일도 공통 서버 파서에 맡기고 준비된 결과만 누적 조합한다", async () => {
    const user = userEvent.setup();
    const receiving = workspace("workspace-import-delivery", "납품 파일", "ORDER_SENT");
    const createDelivery = vi.fn();
    const uploadSources = vi.fn(async () => ({
      job_id: "job-delivery",
      items: [{
        filename: "delivery.xlsx", status: "ACCEPTED", source_id: "source-delivery",
        procurement_import_id: "import-delivery", error: null,
        repair_obligation_id: null, repair_generation: null,
      }],
    }));
    const composeProcurementImport = vi.fn(async () => ({
      import_id: "import-delivery", kind: "DELIVERY", status: "IMPORTED",
      result_id: "delivery-1", state: "RECEIVING", row_version: 2,
    }));
    const api = apiWith({
      getCurrentOrder: async () => ({ order: {
        revision_id: "order-1", revision_number: 1, approval_revision_id: "approval-1",
        quote_id: "quote-1", vendor_name: "푸른서점", budget_won: 50_000,
        total_won: 42_000, difference_won: 8_000, state: "ORDER_SENT", row_version: 1,
        sent: true, artifacts: [], rows: [],
      } }),
      getReceivingStatus: async () => ({
        order_revision_id: "order-1", active_session_id: null, delivery_count: 0,
        ordered_quantity: 2, delivered_quantity: 0, scanned_quantity: 0,
        unresolved_difference_count: 0, can_complete: false,
        blocking_reasons: ["아직 확인하지 않은 주문 수량이 있습니다."], rows: [],
      }),
      listReceivingDifferences: async () => ({ items: [], next_cursor: null }),
      listDeliveries: async () => ({ items: [], next_cursor: null, differences: [], differences_truncated: false }),
      listProcurementImports: async () => ({ items: [{
        import_id: "import-delivery", source_id: "source-delivery", kind: "DELIVERY",
        target_revision_id: "order-1", vendor_name: "납품명세서:푸른서점",
        status: "READY", filename: "delivery.xlsx", detected_format: "XLSX",
        parser_version: "xlsx-v1", template_version: null, total_rows: 2,
        processed_rows: 2, row_error_count: 0, count_confidence: "EXACT",
        mapping_required: null, result_id: null, completed_at: null,
        created_at: "2026-08-29T08:00:00Z", rows: [],
      }], next_cursor: null }),
      uploadSources,
      composeProcurementImport,
      createDelivery,
    });
    render(<ReceivingPanel api={api} onWorkspaceChange={vi.fn()} user={operator} workspace={receiving} />);

    await user.upload(
      await screen.findByLabelText("납품명세서 파일"),
      new File(["not client parsed"], "delivery.xlsx", {
        type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      }),
    );
    await user.click(screen.getByRole("button", { name: "납품명세서 비교하기" }));

    await waitFor(() => expect(composeProcurementImport).toHaveBeenCalledWith("import-delivery", 1));
    expect(uploadSources).toHaveBeenCalledWith("workspace-import-delivery", expect.objectContaining({
      procurementKind: "DELIVERY", targetRevisionId: "order-1",
    }));
    expect(createDelivery).not.toHaveBeenCalled();
  });

  test("네 가지 처리 방침과 완료 불가 이유를 텍스트로 보인다", async () => {
    const receiving = workspace("workspace-receiving", "도착한 책", "RECEIVING");
    const api = apiWith({
      getCurrentOrder: async () => ({ order: {
        revision_id: "order-1", revision_number: 1, approval_revision_id: "approval-1",
        quote_id: "quote-1", vendor_name: "푸른서점", budget_won: 50_000,
        total_won: 42_000, difference_won: 8_000, state: "RECEIVING", row_version: 4,
        sent: true, artifacts: [], rows: [],
      } }),
      getReceivingStatus: async () => ({
        order_revision_id: "order-1", active_session_id: "session-1", delivery_count: 2,
        ordered_quantity: 3, delivered_quantity: 2, scanned_quantity: 2,
        unresolved_difference_count: 1, can_complete: false,
        blocking_reasons: ["처리 방침이 필요한 차이가 1건 있습니다."],
        rows: [{ order_row_id: "row-1", isbn13: "9788937464010", title: "책", edition: "개정판", ordered_quantity: 3, delivered_quantity: 2, scanned_quantity: 2, unit_price: 10_000 }],
      }),
      listReceivingDifferences: async () => ({ items: [{
        id: "difference-1", kind: "QUANTITY", reference_key: "delivery:quantity:1",
        disposition: null, active: true, row_version: 1,
        details: { expected: 3, received: 2, isbn13: "9788937464010", title: "책", edition: "개정판", scanned: null, scanned_quantity: null },
        created_at: "2026-08-29T08:00:00Z", updated_at: "2026-08-29T08:00:00Z",
      }], next_cursor: null }),
      listDeliveries: async () => ({ items: [], next_cursor: null, differences: [], differences_truncated: false }),
    });
    render(<ReceivingPanel api={api} onWorkspaceChange={vi.fn()} user={operator} workspace={receiving} />);
    const summary = await screen.findByLabelText("수령 진행 요약");
    const manifest = within(summary).getByText("납품명세서 반영").parentElement;
    const barcode = within(summary).getByText("바코드 확인").parentElement;
    const unresolved = within(summary).getByText("처리 대기 차이").parentElement;
    expect(manifest).toHaveTextContent("2 / 3권");
    expect(barcode).toHaveTextContent("2 / 3권");
    expect(unresolved).toHaveTextContent("1건");
    expect(screen.getByText("수량 차이")).toBeVisible();
    expect(screen.getByText("책 · ISBN 9788937464010 · 개정판")).toBeVisible();
    const disposition = screen.getByLabelText("처리 방침");
    expect(disposition).toHaveTextContent("업체 확인");
    expect(disposition).toHaveTextContent("반품 예정");
    expect(disposition).toHaveTextContent("추가 납품");
    expect(disposition).toHaveTextContent("그대로 수용");
    expect(screen.getByRole("button", { name: "검수 완료하고 작업 끝내기" })).toBeDisabled();
    expect(screen.getByText("처리 방침이 필요한 차이가 1건 있습니다.")).toBeVisible();
  });
});
