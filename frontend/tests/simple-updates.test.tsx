import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { createLibraryApi } from "../src/simple/api";
import { SimpleLibraryApp } from "../src/simple/SimpleLibraryApp";
import type { UpdateInfo } from "../src/simple/types";

const idle: UpdateInfo = { available: false, phase: "idle", auto_enabled: true, auto_supported: true };
const ready: UpdateInfo = { ...idle, available: true, phase: "ready", latest_version: "2.0.3", url: "https://github.com/buildergarlic/suseoro/releases/tag/v2.0.3" };
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
const requestUrl = (input: RequestInfo | URL) => typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
function fixture(initial: UpdateInfo | null = idle) {
  const list = { id: "list-1", name: "2학기 도서 구입", year: 2026, budget: 15_000_000, discount_percent: 0 };
  const state = { update: initial, failPoll: false, failPreferences: false, checkResult: ready, installCount: 0 };
  const transport = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = requestUrl(input).replace("/api/library", "");
    if (path === "/bootstrap") return json({ version: "2.0.2", csrf_token: "update-test-token", settings: { school_name: "햇살학교", nl_api_key_configured: false }, lists: [list], update: state.update });
    if (path === "/lists/list-1") return json({ list, books: [], summary: { selected_count: 0, total_quantity: 0, order_total: 0, remaining: list.budget, missing_price_count: 0 } });
    if (path === "/updates/status") return state.failPoll ? json({ detail: "잠시 연결할 수 없습니다." }, 503) : json(state.update);
    if (path === "/updates/preferences") {
      expect(init?.method).toBe("PATCH");
      expect(new Headers(init?.headers).get("X-Suseoro-Token")).toBe("update-test-token");
      if (state.failPreferences) return json({ detail: "설정을 저장하지 못했습니다." }, 503);
      const body = JSON.parse(typeof init?.body === "string" ? init.body : "{}") as { auto_enabled: boolean };
      state.update = { ...(state.update ?? idle), auto_enabled: body.auto_enabled, phase: body.auto_enabled ? state.update?.phase : state.update?.available ? "manual" : "idle" };
      return json(state.update);
    }
    if (path === "/updates") { state.update = state.checkResult; return json(state.update); }
    if (path === "/updates/install") { state.installCount += 1; return json({ started: true }); }
    if (path === "/settings") {
      const body = JSON.parse(typeof init?.body === "string" ? init.body : "{}") as { school_name: string };
      return json({ school_name: body.school_name, nl_api_key_configured: false });
    }
    return json({ detail: "Unexpected route " + path }, 404);
  });
  return { state, transport, api: createLibraryApi(transport) };
}
afterEach(() => { vi.useRealTimers(); });

describe("자동 업데이트 화면", () => {
  it("shows a ready bootstrap update immediately and installs only after a deliberate click", async () => {
    const { api, state } = fixture(ready), user = userEvent.setup();
    render(<SimpleLibraryApp api={api} />);
    expect(await screen.findByText("수서로를 닫으면 새 버전이 자동으로 설치됩니다.")).toBeInTheDocument();
    expect(state.installCount).toBe(0);
    await user.click(screen.getByRole("button", { name: "학교 설정 · 백업" }));
    const dialog = screen.getByRole("dialog", { name: "학교 설정" });
    expect(within(dialog).getByRole("checkbox", { name: "자동 업데이트" })).toBeChecked();
    await user.click(within(dialog).getByRole("button", { name: "지금 업데이트" }));
    await waitFor(() => expect(state.installCount).toBe(1));
    expect(await screen.findByRole("heading", { name: "업데이트 설치를 시작했습니다." })).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("polls status without touching an open book form or starting installation, then cleans up", async () => {
    vi.useFakeTimers();
    const { api, state, transport } = fixture();
    const view = render(<SimpleLibraryApp api={api} />);
    await act(async () => {});
    fireEvent.click(screen.getByRole("button", { name: "＋ 직접 추가" }));
    fireEvent.change(screen.getByLabelText(/책 제목/), { target: { value: "아직 저장하지 않은 책" } });
    state.update = ready;
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    expect(screen.getByRole("dialog", { name: "책 직접 추가" })).toBeInTheDocument();
    expect(screen.getByLabelText(/책 제목/)).toHaveValue("아직 저장하지 않은 책");
    expect(screen.getByText("수서로를 닫으면 새 버전이 자동으로 설치됩니다.")).toBeInTheDocument();
    expect(state.installCount).toBe(0);
    const statusCalls = () => transport.mock.calls.filter(([path]) => requestUrl(path).endsWith("/updates/status")).length;
    expect(statusCalls()).toBe(1);
    view.unmount();
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
    expect(statusCalls()).toBe(1);
  });

  it("keeps polling failures quiet and retries on the next interval", async () => {
    vi.useFakeTimers();
    const { api, state } = fixture(null);
    render(<SimpleLibraryApp api={api} />);
    await act(async () => {});
    state.failPoll = true;
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "＋ 직접 추가" })).toBeEnabled();
    state.failPoll = false; state.update = { ...ready, phase: "downloading" };
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    expect(screen.getByText("새 버전을 내려받고 있습니다.")).toBeInTheDocument();
  });

  it("persists the automatic preference and no longer promises installation on exit when disabled", async () => {
    const { api, state } = fixture(ready), user = userEvent.setup();
    render(<SimpleLibraryApp api={api} />);
    await user.click(await screen.findByRole("button", { name: "학교 설정 · 백업" }));
    await user.click(screen.getByRole("checkbox", { name: "자동 업데이트" }));
    await waitFor(() => expect(state.update?.auto_enabled).toBe(false));
    expect(screen.getByRole("checkbox", { name: "자동 업데이트" })).not.toBeChecked();
    expect(screen.queryByText("수서로를 닫으면 새 버전이 자동으로 설치됩니다.")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "업데이트 설치" })).toBeEnabled();
    expect(state.installCount).toBe(0);
  });

  it("keeps the existing preference when saving it fails", async () => {
    const { api, state } = fixture(), user = userEvent.setup(); state.failPreferences = true;
    render(<SimpleLibraryApp api={api} />);
    await user.click(await screen.findByRole("button", { name: "학교 설정 · 백업" }));
    await user.click(screen.getByRole("checkbox", { name: "자동 업데이트" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("설정을 저장하지 못했습니다.");
    expect(screen.getByRole("checkbox", { name: "자동 업데이트" })).toBeChecked();
  });

  it("does not overlap slow polls or overwrite a preference with an older polling response", async () => {
    vi.useFakeTimers();
    const { transport } = fixture(ready);
    let resolveStatus: ((response: Response) => void) | undefined;
    const delayed = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (requestUrl(input).endsWith("/updates/status")) return new Promise<Response>(resolve => { resolveStatus = resolve; });
      return transport(input, init);
    });
    render(<SimpleLibraryApp api={createLibraryApi(delayed)} />);
    await act(async () => {});
    fireEvent.click(screen.getByRole("button", { name: "학교 설정 · 백업" }));
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
    expect(delayed.mock.calls.filter(([path]) => requestUrl(path).endsWith("/updates/status"))).toHaveLength(1);
    fireEvent.click(screen.getByRole("checkbox", { name: "자동 업데이트" }));
    await act(async () => {});
    expect(screen.getByRole("checkbox", { name: "자동 업데이트" })).not.toBeChecked();
    await act(async () => { resolveStatus?.(json(ready)); });
    expect(screen.getByRole("checkbox", { name: "자동 업데이트" })).not.toBeChecked();
    expect(screen.queryByText("수서로를 닫으면 새 버전이 자동으로 설치됩니다.")).not.toBeInTheDocument();
  });

  it("explains manual updates for unsupported installations and keeps the manual path available", async () => {
    const { api, state } = fixture({ ...ready, phase: "manual", auto_supported: false, auto_enabled: false }), user = userEvent.setup();
    render(<SimpleLibraryApp api={api} />);
    await user.click(await screen.findByRole("button", { name: "학교 설정 · 백업" }));
    expect(screen.getByRole("checkbox", { name: "자동 업데이트" })).toBeDisabled();
    expect(screen.getByText(/자동 업데이트는 Windows 설치형에서 사용할 수 있습니다/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /설치 파일 다운로드/ })).toHaveAttribute("href", ready.url);
    await user.click(screen.getByRole("button", { name: "업데이트 설치" }));
    await waitFor(() => expect(state.installCount).toBe(1));
  });

  it("retains unsaved school settings while status changes and requires saving before immediate installation", async () => {
    vi.useFakeTimers();
    const { api, state } = fixture();
    render(<SimpleLibraryApp api={api} />);
    await act(async () => {});
    fireEvent.click(screen.getByRole("button", { name: "학교 설정 · 백업" }));
    fireEvent.change(screen.getByLabelText("학교 이름"), { target: { value: "바꾸는 중인 학교명" } });
    state.update = ready;
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    expect(screen.getByLabelText("학교 이름")).toHaveValue("바꾸는 중인 학교명");
    expect(screen.getByRole("button", { name: "지금 업데이트" })).toBeDisabled();
    expect(state.installCount).toBe(0);
    fireEvent.click(screen.getByRole("button", { name: "설정 저장" }));
    await act(async () => {});
    expect(screen.getByRole("button", { name: "지금 업데이트" })).toBeEnabled();
  });

  it("shows an asynchronous manual check and then receives its result by status polling", async () => {
    vi.useFakeTimers();
    const { api, state } = fixture(); state.checkResult = { ...idle, phase: "checking" };
    render(<SimpleLibraryApp api={api} />);
    await act(async () => {});
    fireEvent.click(screen.getByRole("button", { name: "학교 설정 · 백업" }));
    fireEvent.click(screen.getByRole("button", { name: "업데이트 확인" }));
    await act(async () => {});
    expect(screen.getByText("새 버전을 확인하고 있습니다.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "지금 업데이트" })).not.toBeInTheDocument();
    state.update = ready;
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    expect(screen.getByRole("button", { name: "지금 업데이트" })).toBeEnabled();
    expect(state.installCount).toBe(0);
  });
});
