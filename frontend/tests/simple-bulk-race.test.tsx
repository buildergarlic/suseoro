import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { createLibraryApi } from "../src/simple/api";
import { SimpleLibraryApp } from "../src/simple/SimpleLibraryApp";
import { emptyBook, type AcquisitionList, type BulkResult } from "../src/simple/types";

it("keeps the active list when an old bulk request finishes after creating another list", async () => {
  const old: AcquisitionList = { id: "old", name: "이전 목록", year: 2026, budget: 10000, discount_percent: 0, created_at: "" };
  const fresh = { ...old, id: "fresh", name: "새로운 목록" };
  let release!: (value: BulkResult) => void;
  const pending = new Promise<BulkResult>(resolve => { release = resolve; });
  const api = Object.assign(createLibraryApi(), {
    bootstrap: vi.fn(async () => ({ version: "2.2.0", csrf_token: "token", settings: { school_name: "학교", nl_api_key_configured: false }, lists: [old], update: null })),
    list: vi.fn(async (id: string) => ({ list: id === "old" ? old : fresh, books: id === "old" ? [{ ...emptyBook(), id: "book", title: "확인할 책", price: 100, held: false, duplicate: false }] : [], summary: { selected_count: 1, total_quantity: 1, list_total: 100, order_total: 100, remaining: 9900, missing_price_count: 0, review_count: 0, held_count: 0, duplicate_count: 0 } })),
    createList: vi.fn(async () => fresh),
    bulkBooks: vi.fn(() => pending),
  });
  const user = userEvent.setup();
  render(<SimpleLibraryApp api={api} />);
  await screen.findByRole("checkbox", { name: "확인할 책 일괄 작업 선택" });
  await user.click(screen.getByRole("checkbox", { name: "확인할 책 일괄 작업 선택" }));
  await user.click(screen.getByRole("button", { name: "선택 도서 보류" }));
  await user.click(screen.getByRole("button", { name: "새 구입 목록" }));
  await user.click(screen.getByRole("button", { name: "저장" }));
  await screen.findByRole("heading", { name: "새로운 목록" });
  await act(async () => { release({ updated: 1, skipped: [], operation_id: "old-operation" }); await pending; });
  await waitFor(() => expect(screen.getByRole("heading", { name: "새로운 목록" })).toBeInTheDocument());
  expect(screen.queryByRole("heading", { name: "이전 목록" })).not.toBeInTheDocument();
});
