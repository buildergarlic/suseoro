import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, test, vi } from "vitest";

import type { SuseoroApi } from "../src/api/client";
import { ApprovalPanel, ChangesRequestedNotice } from "../src/features/approvals/ApprovalPanel";
import { OrderPanel } from "../src/features/orders/OrderPanel";
import { waitForProcurementJob } from "../src/features/procurement/procurementImport";
import { QuotePanel } from "../src/features/quotes/QuotePanel";
import {
  createFixtureApi,
  FixtureApiError,
  idleJob,
  operator,
  reviewer,
  sourceFixture,
  workspace,
} from "../src/test/fixtures";

const approvalDetail = {
  revision_id: "approval-1",
  revision_number: 2,
  sha256: "a".repeat(64),
  payload: {
    budget_won: 50_000,
    candidate_collection_revision: 7,
    expected_total_won: 42_000,
    candidates: [
      {
        approval_row_id: "approval-row-1",
        author: "글쓴이",
        candidate_id: "candidate-1",
        edition: "개정판",
        isbn13: "9788937464010",
        quantity: 3,
        title: "도서관의 책",
        unit_price: 14_000,
      },
    ],
  },
  metadata: {
    candidate_count: 3,
    catalog_as_of_local_date: "2026-08-28",
    unresolved_complete: true,
    source_counts_verified: true,
    auto_excluded_count: 2,
    auto_exclusions: [{ reason: "이미 소장", count: 2 }],
    previous_revision: {
      revision_number: 1,
      added_count: 1,
      removed_count: 0,
      quantity_changed_count: 1,
      price_changed_count: 0,
    },
    request_reason: "수정 내용을 반영했습니다.",
    requested_by_display_name: "김사서",
    created_at: "2026-08-29T08:00:00Z",
    decision: null,
    decision_reason: null,
  },
};

function apiWith(overrides: Partial<SuseoroApi>): SuseoroApi {
  return { ...createFixtureApi(), ...overrides };
}

describe("공통 파서 작업 상태", () => {
  test("취소된 작업을 즉시 종료 상태로 처리한다", async () => {
    vi.useFakeTimers();
    const getJob = vi.fn(async () => ({ ...idleJob, status: "CANCELLED" }));
    try {
      const waiting = waitForProcurementJob(apiWith({ getJob }), "job-cancelled");
      await vi.runAllTimersAsync();
      await expect(waiting).resolves.toBeUndefined();
      expect(getJob).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("승인 검토", () => {
  test("검토자는 핵심 근거만 보고 한 행동으로 승인한다", async () => {
    const user = userEvent.setup();
    const approve = vi.fn(async () => ({
      revision_id: "approval-1",
      decision: "APPROVED",
      state: "APPROVED",
      row_version: 4,
    }));
    const pending = workspace("workspace-approval", "승인할 목록", "APPROVAL_PENDING");
    const api = apiWith({
      listApprovals: async () => ({
        items: [
          {
            id: "approval-1",
            revision_number: 2,
            budget_won: 50_000,
            expected_total_won: 42_000,
            sha256: "a".repeat(64),
            candidate_collection_revision: 7,
            candidate_count: 3,
            decision: null,
            request_reason: "수정 내용을 반영했습니다.",
            created_at: "2026-08-29T08:00:00Z",
          },
        ],
        next_cursor: null,
      }),
      getApproval: async () => approvalDetail,
      approveApproval: approve,
    });
    render(
      <ApprovalPanel
        api={api}
        onWorkspaceChange={vi.fn()}
        user={reviewer}
        workspace={pending}
      />,
    );

    expect(await screen.findByText("후보 3권")).toBeVisible();
    expect(screen.getByText("장서 기준일 2026. 8. 28.")).toBeVisible();
    expect(screen.getByText(/확인 필요 0건/)).toBeVisible();
    expect(screen.getByText(/이전 버전보다 1권 추가/)).toBeVisible();
    expect(screen.getByText(/자동 제외 2권/)).toBeVisible();
    await user.click(screen.getByRole("button", { name: "이 목록 승인" }));
    await waitFor(() => expect(approve).toHaveBeenCalledTimes(1));
  });

  test("수정 요청은 사유 없이는 전송되지 않고 닫은 뒤 초점이 돌아온다", async () => {
    const user = userEvent.setup();
    const requestChanges = vi.fn();
    const pending = workspace("workspace-changes", "수정할 목록", "APPROVAL_PENDING");
    const api = apiWith({
      listApprovals: async () => ({ items: [{
        id: "approval-1", revision_number: 2, budget_won: 50_000,
        expected_total_won: 42_000, sha256: "a".repeat(64),
        candidate_collection_revision: 7, candidate_count: 3, decision: null,
        request_reason: "요청", created_at: "2026-08-29T08:00:00Z",
      }], next_cursor: null }),
      getApproval: async () => approvalDetail,
      requestApprovalChanges: requestChanges,
    });
    render(<ApprovalPanel api={api} onWorkspaceChange={vi.fn()} user={reviewer} workspace={pending} />);
    const opener = await screen.findByRole("button", { name: "수정 요청 보내기" });
    await user.click(opener);
    const dialog = screen.getByRole("dialog", { name: "수정 요청 보내기" });
    await user.click(within(dialog).getByRole("button", { name: "수정 요청 보내기" }));
    expect(requestChanges).not.toHaveBeenCalled();
    expect(within(dialog).getByRole("alert")).toHaveTextContent("사유");
    await user.keyboard("{Escape}");
    expect(opener).toHaveFocus();
  });

  test("담당자는 검토자의 수정 사유를 실제 승인 상세에서 다시 본다", async () => {
    const requested = workspace("workspace-changes", "수정할 목록", "CHANGES_REQUESTED");
    const api = apiWith({
      listApprovals: async () => ({
        items: [{
          id: "approval-1", revision_number: 2, budget_won: 50_000,
          expected_total_won: 42_000, sha256: "a".repeat(64),
          candidate_collection_revision: 7, candidate_count: 3,
          decision: "REJECTED", request_reason: "요청",
          created_at: "2026-08-29T08:00:00Z",
        }],
        next_cursor: null,
      }),
      getApproval: async () => ({
        ...approvalDetail,
        metadata: {
          ...approvalDetail.metadata,
          decision: "REJECTED",
          decision_reason: "판본과 수량을 다시 확인해 주세요.",
        },
      }),
    });
    render(<ChangesRequestedNotice api={api} workspace={requested} />);
    expect(await screen.findByText("판본과 수량을 다시 확인해 주세요.")).toBeVisible();
    expect(screen.getByText(/새 버전으로 다시 승인 요청/)).toBeVisible();
  });

  test("stale 승인 충돌은 최신 요청을 다시 읽는 복구 행동을 제공한다", async () => {
    const user = userEvent.setup();
    const pending = workspace("workspace-stale-approval", "바뀐 승인", "APPROVAL_PENDING");
    const getWorkspace = vi.fn(async () => ({
      data: { ...pending, row_version: 2 }, etag: '"2"',
    }));
    const api = apiWith({
      listApprovals: async () => ({ items: [{
        id: "approval-1", revision_number: 2, budget_won: 50_000,
        expected_total_won: 42_000, sha256: "a".repeat(64),
        candidate_collection_revision: 7, candidate_count: 3, decision: null,
        request_reason: "요청", created_at: "2026-08-29T08:00:00Z",
      }], next_cursor: null }),
      getApproval: async () => approvalDetail,
      approveApproval: async () => { throw new FixtureApiError(409, "ROW_VERSION_CONFLICT", "다른 사용자가 먼저 수정했습니다."); },
      getWorkspace,
    });
    render(<ApprovalPanel api={api} onWorkspaceChange={vi.fn()} user={reviewer} workspace={pending} />);
    await user.click(await screen.findByRole("button", { name: "이 목록 승인" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("최신 승인 요청을 다시 확인");
    await user.click(screen.getByRole("button", { name: "최신 승인 요청 다시 불러오기" }));
    await waitFor(() => expect(getWorkspace).toHaveBeenCalledWith(pending.id));
  });
});

describe("견적 비교와 발주", () => {
  test("견적 workroom 안에서 MAPPING_REQUIRED 열 연결을 저장하고 다시 파싱한다", async () => {
    const user = userEvent.setup();
    const quoteReview = workspace("workspace-mapping-quote", "열 연결", "QUOTE_REVIEW");
    const mappingImport = {
      import_id: "import-mapping", source_id: "source-mapping", kind: "QUOTE" as const,
      target_revision_id: "approval-1", vendor_name: "처음 보는 업체",
      status: "MAPPING_REQUIRED", filename: "unknown.csv", detected_format: "CSV",
      parser_version: "tabular-v1", template_version: null, total_rows: 1,
      processed_rows: 0, row_error_count: 0, count_confidence: "EXACT",
      mapping_required: {
        headers: ["A", "B", "C"], preview_rows: [["9788937464010", "도서관의 책", 12000]],
        suggested_mapping: { A: "isbn", B: "title", C: "unit_price" },
        required_fields: ["title"], confidence: 0.4, questions: ["제목 열을 확인해 주세요."],
      },
      result_id: null, completed_at: null, created_at: "2026-08-29T08:00:00Z", rows: [],
    };
    const updateSourceMapping = vi.fn<SuseoroApi["updateSourceMapping"]>().mockResolvedValue({
      data: { ...sourceFixture, id: "source-mapping", role: "VENDOR_QUOTE" as const, vendor_scope: "처음 보는 업체", row_version: 2 },
      etag: '"2"',
    });
    const parseSource = vi.fn(async () => ({ job_id: "job-mapping", status: "QUEUED" }));
    const api = apiWith({
      listApprovals: async () => ({ items: [{
        id: "approval-1", revision_number: 2, budget_won: 50_000,
        expected_total_won: 42_000, sha256: "a".repeat(64),
        candidate_collection_revision: 7, candidate_count: 3, decision: "APPROVED",
        request_reason: "요청", created_at: "2026-08-29T08:00:00Z",
      }], next_cursor: null }),
      getApproval: async () => approvalDetail,
      listQuotes: async () => ({ items: [], next_cursor: null }),
      listProcurementImports: async () => ({ items: [mappingImport], next_cursor: null }),
      getSource: async () => ({ data: { ...sourceFixture, id: "source-mapping", role: "VENDOR_QUOTE", vendor_scope: "처음 보는 업체" }, etag: '"1"' }),
      updateSourceMapping,
      parseSource,
      getJob: async () => ({ ...idleJob, id: "job-mapping" }),
    });
    render(<QuotePanel api={api} onWorkspaceChange={vi.fn()} user={operator} workspace={quoteReview} />);
    await user.click(await screen.findByRole("button", { name: "unknown.csv 열 연결하기" }));
    expect(screen.getByRole("dialog", { name: "열 연결 확인" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "열 연결 적용" }));
    await waitFor(() => expect(updateSourceMapping).toHaveBeenCalledOnce());
    const [sourceId, payload, sourceVersion] = updateSourceMapping.mock.calls[0];
    expect(sourceId).toBe("source-mapping");
    expect(payload).toMatchObject({ role: "VENDOR_QUOTE", mapping: { B: "title" } });
    expect(sourceVersion).toBe(1);
    await waitFor(() => expect(parseSource).toHaveBeenCalledWith("source-mapping"));
  });

  test("견적 read model 오류는 영구 loading 대신 복구 행동을 보인다", async () => {
    const user = userEvent.setup();
    const listApprovals = vi.fn<SuseoroApi["listApprovals"]>()
      .mockRejectedValueOnce(new Error("견적 기준을 불러오지 못했습니다."))
      .mockResolvedValueOnce({ items: [], next_cursor: null });
    render(<QuotePanel api={apiWith({ listApprovals })} onWorkspaceChange={vi.fn()} user={operator} workspace={workspace("workspace-quote-error", "견적 오류", "QUOTE_REVIEW")} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("견적 기준을 불러오지 못했습니다.");
    await user.click(screen.getByRole("button", { name: "다시 불러오기" }));
    await waitFor(() => expect(listApprovals).toHaveBeenCalledTimes(2));
  });

  test("품절·가격 누락·목록 불일치·미연결은 각각 정확한 이유로 선택을 막는다", async () => {
    const quoteReview = workspace("workspace-blocked-quotes", "선택 차단", "QUOTE_REVIEW");
    const base = {
      approval_revision_id: "approval-1", total_won: 10_000, list_total_won: 10_000,
      discount_won: 0, budget_overrun_won: 0, out_of_stock_count: 0,
      missing_price_count: 0, list_mismatch_count: 0, needs_review_count: 0,
      unmatched_count: 0, requires_reapproval: false,
      reconciliation: { duplicate_isbns: [], price_conflicts: [] },
      created_at: "2026-08-29T08:00:00Z",
    };
    const api = apiWith({
      listApprovals: async () => ({ items: [{
        id: "approval-1", revision_number: 2, budget_won: 50_000,
        expected_total_won: 42_000, sha256: "a".repeat(64), candidate_collection_revision: 7,
        candidate_count: 3, decision: "APPROVED", request_reason: "요청",
        created_at: "2026-08-29T08:00:00Z",
      }], next_cursor: null }),
      getApproval: async () => approvalDetail,
      listQuotes: async () => ({ items: [
        { ...base, id: "out", vendor_name: "품절서점", out_of_stock_count: 1 },
        { ...base, id: "price", vendor_name: "가격누락서점", missing_price_count: 1 },
        { ...base, id: "mismatch", vendor_name: "불일치서점", list_mismatch_count: 1 },
        { ...base, id: "unmatched", vendor_name: "미연결서점", unmatched_count: 1 },
      ], next_cursor: null }),
    });
    render(<QuotePanel api={api} onWorkspaceChange={vi.fn()} user={operator} workspace={quoteReview} />);
    await screen.findByText("품절서점");
    const blockedButtons = screen.getAllByRole("button", { name: "이 견적 사용" });
    expect(blockedButtons).toHaveLength(4);
    blockedButtons.forEach((button) => expect(button).toBeDisabled());
    expect(screen.getByText("품절 도서 1건을 먼저 조정해 주세요.")).toBeVisible();
    expect(screen.getByText("가격이 없는 도서 1건을 먼저 확인해 주세요.")).toBeVisible();
    expect(screen.getByText("승인 목록과 가격·수량이 다른 도서 1건을 먼저 확인해 주세요.")).toBeVisible();
    expect(screen.getByText("승인 목록에 연결되지 않은 도서 1건을 먼저 연결해 주세요.")).toBeVisible();
  });

  test("견적 파일은 브라우저에서 해석하지 않고 공통 서버 파서 결과를 조합한다", async () => {
    const user = userEvent.setup();
    const quoteReview = workspace("workspace-import-quote", "견적 파일", "QUOTE_REVIEW");
    const createQuote = vi.fn();
    const uploadSources = vi.fn(async () => ({
      job_id: "job-quote",
      items: [{
        filename: "quote.xlsx", status: "ACCEPTED", source_id: "source-quote",
        procurement_import_id: "import-quote", error: null,
        repair_obligation_id: null, repair_generation: null,
      }],
    }));
    const composeProcurementImport = vi.fn(async () => ({
      import_id: "import-quote", kind: "QUOTE", status: "IMPORTED",
      result_id: "quote-1", state: "QUOTE_REVIEW", row_version: 2,
    }));
    const readyImport = {
      import_id: "import-quote", source_id: "source-quote", kind: "QUOTE",
      target_revision_id: "approval-1", vendor_name: "푸른서점",
      status: "READY", filename: "quote.xlsx", detected_format: "XLSX",
      parser_version: "xlsx-v1", template_version: null, total_rows: 2,
      processed_rows: 2, row_error_count: 0, count_confidence: "EXACT",
      mapping_required: null, result_id: null, completed_at: null,
      created_at: "2026-08-29T08:00:00Z", rows: [],
    };
    const api = apiWith({
      listApprovals: async () => ({ items: [{
        id: "approval-1", revision_number: 2, budget_won: 50_000,
        expected_total_won: 42_000, sha256: "a".repeat(64),
        candidate_collection_revision: 7, candidate_count: 3, decision: "APPROVED",
        request_reason: "요청", created_at: "2026-08-29T08:00:00Z",
      }], next_cursor: null }),
      getApproval: async () => approvalDetail,
      listQuotes: async () => ({ items: [], next_cursor: null }),
      listProcurementImports: async () => ({ items: [readyImport], next_cursor: null }),
      uploadSources,
      getJob: async () => ({
        id: "job-quote", workspace_id: "workspace-import-quote", type: "INGEST",
        status: "SUCCEEDED", stage: "COMPLETE", progress_current: 1,
        progress_total: 1, error: null, retry_count: 0, items: [],
      }),
      composeProcurementImport,
      createQuote,
    });
    render(<QuotePanel api={api} onWorkspaceChange={vi.fn()} user={operator} workspace={quoteReview} />);

    await user.type(await screen.findByLabelText("업체 이름"), "푸른서점");
    await user.upload(
      screen.getByLabelText("견적 파일"),
      new File(["not client parsed"], "quote.xlsx", {
        type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      }),
    );
    await user.click(screen.getByRole("button", { name: "견적서 비교하기" }));

    await waitFor(() => expect(composeProcurementImport).toHaveBeenCalledWith("import-quote", 1));
    expect(uploadSources).toHaveBeenCalledWith("workspace-import-quote", expect.objectContaining({
      procurementKind: "QUOTE", targetRevisionId: "approval-1",
    }));
    expect(createQuote).not.toHaveBeenCalled();
  });

  test("여러 업체 지표와 예산 차이를 항상 보이고 초과 견적을 임의 삭제하지 않는다", async () => {
    const quoteReview = workspace("workspace-quotes", "견적 비교", "QUOTE_REVIEW");
    const api = apiWith({
      listApprovals: async () => ({ items: [{
        id: "approval-1", revision_number: 2, budget_won: 50_000,
        expected_total_won: 42_000, sha256: "a".repeat(64),
        candidate_collection_revision: 7, candidate_count: 3, decision: "APPROVED",
        request_reason: "요청", created_at: "2026-08-29T08:00:00Z",
      }], next_cursor: null }),
      getApproval: async () => approvalDetail,
      listQuotes: async () => ({
        items: [
          {
            id: "quote-a", approval_revision_id: "approval-1", vendor_name: "푸른서점",
            total_won: 47_000, list_total_won: 52_000, discount_won: 5_000,
            budget_overrun_won: 0, out_of_stock_count: 0, missing_price_count: 0,
            list_mismatch_count: 0, needs_review_count: 0, unmatched_count: 0,
            requires_reapproval: false, reconciliation: { duplicate_isbns: [], price_conflicts: [] },
            created_at: "2026-08-29T08:00:00Z",
          },
          {
            id: "quote-b", approval_revision_id: "approval-1", vendor_name: "초과서점",
            total_won: 53_000, list_total_won: 55_000, discount_won: 2_000,
            budget_overrun_won: 3_000, out_of_stock_count: 0, missing_price_count: 1,
            list_mismatch_count: 0, needs_review_count: 1, unmatched_count: 1,
            requires_reapproval: true, reconciliation: { duplicate_isbns: [], price_conflicts: [] },
            created_at: "2026-08-29T08:01:00Z",
          },
        ],
        next_cursor: null,
      }),
      getCurrentOrder: async () => ({ order: null }),
    });
    render(<QuotePanel api={api} onWorkspaceChange={vi.fn()} user={operator} workspace={quoteReview} />);
    expect(await screen.findByText("푸른서점")).toBeVisible();
    expect(screen.getByText("초과서점")).toBeVisible();
    expect(screen.getByText("승인 예산")).toBeVisible();
    expect(screen.getByText("선택 견적")).toBeVisible();
    expect(screen.getByText("차이")).toBeVisible();
    expect(screen.getByText(/3,000원 초과/)).toBeVisible();
    expect(screen.getByText(/목록을 자동으로 줄이지 않습니다/)).toBeVisible();
    const quoteActions = screen.getAllByRole("button", { name: "이 견적 사용" });
    expect(quoteActions).toHaveLength(2);
    expect(quoteActions.filter((button) => button.hasAttribute("disabled"))).toHaveLength(1);
  });

  test("발주파일 받기는 실제 blob을 한 번 내려받고 외부 전달 경계를 알린다", async () => {
    const user = userEvent.setup();
    const ready = workspace("workspace-order", "발주 준비", "ORDER_READY");
    const downloadOrder = vi.fn(async () => ({
      blob: new Blob(["xlsx"], { type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" }),
      filename: "order.xlsx",
    }));
    vi.stubGlobal("URL", {
      createObjectURL: vi.fn(() => "blob:order"),
      revokeObjectURL: vi.fn(),
    });
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
    const api = apiWith({
      downloadOrder,
      getCurrentOrder: async () => ({ order: {
        revision_id: "order-1", revision_number: 1,
        approval_revision_id: "approval-1", quote_id: "quote-1",
        vendor_name: "푸른서점", budget_won: 50_000, total_won: 42_000,
        difference_won: 8_000, state: "ORDER_READY", row_version: 4,
        sent: false, artifacts: [], rows: [],
      } }),
    });
    render(<OrderPanel api={api} onWorkspaceChange={vi.fn()} user={operator} workspace={ready} />);
    await user.click(await screen.findByRole("button", { name: "발주파일 받기" }));
    expect(downloadOrder).toHaveBeenCalledOnce();
    expect(screen.getByRole("status")).toHaveTextContent(
      "발주파일이 완성되었습니다. 실제 주문 전 업체에 직접 전달해 주세요.",
    );
  });
});
