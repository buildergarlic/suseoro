import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { createLibraryApi } from "./api";
import { HelpDialog } from "./HelpDialog";
import { SimpleLibraryApp } from "./SimpleLibraryApp";
import { emptyBook, type AcquisitionList, type Book } from "./types";

describe("프로그램 안 사용설명서", () => {
  it("opens and closes without saving or refreshing the current list, and keeps its search and filter", async () => {
    const user = userEvent.setup();
    const list: AcquisitionList = { id: "list-1", name: "2학기 수서", year: 2026, budget: 15_000_000, discount_percent: 0, created_at: "2026-09-15" };
    const books: Book[] = [
      { ...emptyBook(), id: "book-1", title: "교실 속 우주", price: 18000, selected: false, held: false, duplicate: false },
      { ...emptyBook(), id: "book-2", title: "나무 이야기", price: 12000, selected: true, held: false, duplicate: false },
    ];
    const transport = vi.fn((input: RequestInfo | URL) => {
      const path = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      if (path.endsWith("/updates/status")) return Promise.resolve(new Response(JSON.stringify({ available: false, phase: "idle", auto_enabled: false, auto_supported: false }), { headers: { "Content-Type": "application/json" } }));
      const payload = path.endsWith("/bootstrap") ? { version: "2.0.1", csrf_token: "test-token", lists: [list], settings: { school_name: "햇살학교", nl_api_key_configured: false }, update: null } : { list, books, summary: { selected_count: 1, total_quantity: 1, list_total: 12000, order_total: 12000, remaining: 14988000, missing_price_count: 0, review_count: 0, held_count: 0, duplicate_count: 0 } };
      return Promise.resolve(new Response(JSON.stringify(payload), { headers: { "Content-Type": "application/json" } }));
    });
    render(<SimpleLibraryApp api={createLibraryApi(transport)} />);
    await screen.findByRole("heading", { name: "2학기 수서" });
    await user.type(screen.getByRole("textbox", { name: "도서 검색" }), "우주");
    await user.click(screen.getByRole("button", { name: /보류\s*1/ }));
    const callsBeforeHelp = transport.mock.calls.length;
    const open = screen.getByRole("button", { name: "사용설명서" });
    await user.click(open);
    const dialog = screen.getByRole("dialog", { name: "사용설명서" });
    expect(within(dialog).getByTitle("수서로 사용설명서 · 안내 처음")).toHaveAttribute("src", "/help/index.html");
    await user.click(within(dialog).getByRole("button", { name: "목록으로 돌아가기" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(open).toHaveFocus();
    expect(screen.getByRole("textbox", { name: "도서 검색" })).toHaveValue("우주");
    expect(screen.getByRole("button", { name: /보류\s*1/ })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "교실 속 우주" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "나무 이야기" })).not.toBeInTheDocument();
    expect(transport.mock.calls).toHaveLength(callsBeforeHelp);
  });

  it("switches bundled guides inside a restricted frame and provides a local fallback", async () => {
    const user = userEvent.setup();
    render(<HelpDialog onClose={vi.fn()} />);
    for (const [title, file] of [["그림 안내", "visual-guide.html"], ["상세 설명서", "user-guide.html"], ["간단한 사용법", "quick-start.html"]]) {
      await user.click(screen.getByRole("button", { name: title }));
      const frame = screen.getByTitle(`수서로 사용설명서 · ${title}`);
      expect(frame).toHaveAttribute("src", `/help/${file}`);
      expect(frame).toHaveAttribute("sandbox", "allow-scripts allow-modals allow-popups allow-popups-to-escape-sandbox");
      expect(frame).toHaveAttribute("tabindex", "0");
      fireEvent.load(frame);
      expect(screen.queryByRole("status")).not.toBeInTheDocument();
      expect(screen.getByRole("link", { name: /설명서가 보이지 않으면/ })).toHaveAttribute("href", `/help/${file}`);
      expect(screen.getByRole("button", { name: title })).toHaveAttribute("aria-pressed", "true");
    }
  });

  it("accepts Escape messages only from the guide frame and still supports the parent close button", async () => {
    const user = userEvent.setup(), onClose = vi.fn();
    render(<HelpDialog onClose={onClose} />);
    const frame = screen.getByTitle<HTMLIFrameElement>("수서로 사용설명서 · 안내 처음");
    fireEvent(window, new MessageEvent("message", { source: window, data: { type: "suseoro-help-close" }, origin: "null" }));
    fireEvent(window, new MessageEvent("message", { source: frame.contentWindow, data: { type: "unrelated" }, origin: "null" }));
    expect(onClose).not.toHaveBeenCalled();
    fireEvent(window, new MessageEvent("message", { source: frame.contentWindow, data: { type: "suseoro-help-close" }, origin: "null" }));
    expect(onClose).toHaveBeenCalledOnce();
    await user.click(screen.getByRole("button", { name: "닫기" }));
    expect(onClose).toHaveBeenCalledTimes(2);
  });

  it("keeps the manual available when the library cannot load", async () => {
    const user = userEvent.setup();
    const transport = vi.fn(() => Promise.reject<Response>(new Error("Unavailable")));
    render(<SimpleLibraryApp api={createLibraryApi(transport)} />);
    await screen.findByRole("alert");
    await user.click(screen.getByRole("button", { name: "사용설명서" }));
    expect(screen.getByRole("dialog", { name: "사용설명서" })).toBeInTheDocument();
    expect(transport).toHaveBeenCalledOnce();
  });
});
