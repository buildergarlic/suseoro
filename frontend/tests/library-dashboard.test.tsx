import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { createLibraryApi } from "../src/simple/api";
import { LibraryDashboard } from "../src/simple/LibraryDashboard";
import type { HoldingsEnrichment, HoldingsStatistics } from "../src/simple/types";

const statistics: HoldingsStatistics = {
  total_records: 100, total_copies: 123, unique_isbns: 90, valid_isbn_records: 95,
  classified_records: 50, metadata_records: 70,
  category_counts: [{ key: "400", label: "자연과학", count: 10 }, { key: "800", label: "문학", count: 40 }, { key: "unknown", label: "미분류", count: 50 }],
  topics: [{ label: "환경", count: 20 }, { label: "우정", count: 10 }],
  publication_years: [{ label: "2020년대", count: 80 }],
  top_publishers: [{ label: "책숲출판사", count: 35 }],
  coverage: { missing_isbn: 5, missing_category: 50, missing_year: 20 },
  topic_method: "제목과 주제어의 키워드로 추정합니다.",
};
const idle: HoldingsEnrichment = { phase: "idle", total: 0, processed: 0, found: 0, message: "" };
const running: HoldingsEnrichment = { phase: "running", total: 1, processed: 0, found: 0, message: "도서정보를 확인하고 있어요." };
function testApi() {
  return {
    ...createLibraryApi(),
    holdingsStatistics: vi.fn<() => Promise<HoldingsStatistics>>().mockResolvedValue(statistics),
    holdingsEnrichment: vi.fn<() => Promise<HoldingsEnrichment>>().mockResolvedValue(idle),
    enrichHoldings: vi.fn<() => Promise<HoldingsEnrichment>>().mockResolvedValue(running),
    stopHoldingsEnrichment: vi.fn<() => Promise<HoldingsEnrichment>>().mockResolvedValue({ ...idle, message: "보완을 중지했습니다." }),
  };
}
afterEach(() => { vi.useRealTimers(); });

describe("우리 학교 책숲", () => {
  it("shows real counts, includes unclassified records, and identifies the largest classified group", async () => {
    const { container } = render(<LibraryDashboard api={testApi()} schoolName="햇살초등학교" refreshKey={0} onImport={vi.fn()} />);
    expect(await screen.findByRole("img", { name: /자연과학 10건, 문학 40건, 미분류 50건/ })).toBeInTheDocument();
    expect(screen.getByText("123")).toHaveTextContent("123권");
    expect(container.querySelector(".library-insight")).toHaveTextContent("문학 분야가 40건으로 가장 큰 책숲");
    expect(screen.getByText("제목과 도서 주제어에서 찾은 소재 · 추정치")).toBeInTheDocument();
    expect(screen.getByText(/책 본문을 분석한 결과는 아니에요/)).toBeInTheDocument();
    expect(screen.getByText(/학교명이나 소장목록 파일 전체는 보내지 않아요/)).toBeInTheDocument();
  });

  it("invites an import for an empty collection without showing invented statistics", async () => {
    const api = testApi(), onImport = vi.fn();
    api.holdingsStatistics.mockResolvedValue({ ...statistics, total_records: 0, total_copies: 0, category_counts: [], topics: [] });
    render(<LibraryDashboard api={api} schoolName="" refreshKey={0} onImport={onImport} />);
    await screen.findByRole("heading", { name: "첫 책부터 책숲을 채워 볼까요?" });
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
    expect(screen.queryByText("123")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "학교 소장목록 가져오기" }));
    expect(onImport).toHaveBeenCalledOnce();
  });

  it("keeps statistics available if enrichment status fails, and retries a statistics failure", async () => {
    const api = testApi();
    api.holdingsStatistics.mockRejectedValueOnce(new Error("통계 조회 실패"));
    api.holdingsEnrichment.mockRejectedValue(new Error("보완 상태 조회 실패"));
    render(<LibraryDashboard api={api} schoolName="햇살학교" refreshKey={0} onImport={vi.fn()} />);
    await screen.findByText("통계 조회 실패");
    fireEvent.click(screen.getByRole("button", { name: "통계 다시 불러오기" }));
    await screen.findByRole("img", { name: /분야별 등록 자료 분포/ });
    expect(screen.getByText("보완 상태 조회 실패")).toBeInTheDocument();
    expect(api.holdingsStatistics).toHaveBeenCalledTimes(2);
  });

  it("polls an active lookup, reloads statistics on completion, and stops polling afterward", async () => {
    const api = testApi();
    api.holdingsEnrichment.mockResolvedValueOnce(idle).mockResolvedValue({ phase: "completed", total: 1, processed: 1, found: 1, message: "정보 보완 완료" });
    render(<LibraryDashboard api={api} schoolName="햇살학교" refreshKey={0} onImport={vi.fn()} />);
    await screen.findByRole("img", { name: /분야별 등록 자료 분포/ });
    vi.useFakeTimers();
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "ISBN 도서정보 보완" })); });
    expect(screen.getByRole("progressbar", { name: "ISBN 정보 보완 진행률" })).toHaveValue(0);
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000); });
    expect(screen.getByText("정보 보완 완료")).toBeInTheDocument();
    expect(api.holdingsStatistics).toHaveBeenCalledTimes(2);
    const calls = api.holdingsEnrichment.mock.calls.length;
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    expect(api.holdingsEnrichment).toHaveBeenCalledTimes(calls);
  });

  it("lets a librarian stop an active lookup and refreshes the saved progress", async () => {
    const api = testApi();
    api.holdingsEnrichment.mockResolvedValueOnce(running).mockResolvedValue({ ...idle, message: "보완을 중지했습니다." });
    render(<LibraryDashboard api={api} schoolName="햇살학교" refreshKey={0} onImport={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "보완 중지" }));
    await screen.findByText("보완을 중지했습니다.");
    expect(api.stopHoldingsEnrichment).toHaveBeenCalledOnce();
    expect(screen.queryByRole("button", { name: "보완 중지" })).not.toBeInTheDocument();
  });

  it("does not let an older polling response override a stop request", async () => {
    const api = testApi();
    let finishPoll!: (value: HoldingsEnrichment) => void;
    let finishStop!: (value: HoldingsEnrichment) => void;
    const poll = new Promise<HoldingsEnrichment>(resolve => { finishPoll = resolve; });
    const stop = new Promise<HoldingsEnrichment>(resolve => { finishStop = resolve; });
    api.holdingsEnrichment.mockResolvedValueOnce(running).mockReturnValueOnce(poll).mockResolvedValue(idle);
    api.stopHoldingsEnrichment.mockReturnValue(stop);
    render(<LibraryDashboard api={api} schoolName="햇살학교" refreshKey={0} onImport={vi.fn()} />);
    await screen.findByRole("button", { name: "보완 중지" });
    vi.useFakeTimers();
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000); });
    fireEvent.click(screen.getByRole("button", { name: "보완 중지" }));
    await act(async () => { finishPoll({ ...running, phase: "completed", message: "이전 조회 응답" }); });
    expect(screen.queryByText("이전 조회 응답")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "중지하는 중…" })).toBeDisabled();
    await act(async () => { finishStop({ ...idle, message: "중지 완료" }); });
    expect(api.stopHoldingsEnrichment).toHaveBeenCalledOnce();
  });
});
