import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { createLibraryApi } from "../src/simple/api";
import { SimpleLibraryApp } from "../src/simple/SimpleLibraryApp";
import { emptyBook, type AcquisitionList } from "../src/simple/types";

const first: AcquisitionList = { id: "first", name: "1학기 구입", year: 2026, budget: 100000, discount_percent: 0, created_at: "2026-01-01" };
const second: AcquisitionList = { ...first, id: "second", name: "2학기 구입" };
function fixture(withDeleted = false) {
  const state = { lists: withDeleted ? [first] : [first, second], deleted: withDeleted ? [{ ...second, deleted_at: "2026-10-12T00:00:00Z" }] : [] as AcquisitionList[], failDelete: false };
  const api = Object.assign(createLibraryApi(), {
    bootstrap: vi.fn(async () => ({ version: "2.3.0", csrf_token: "token", settings: { school_name: "우리 학교", nl_api_key_configured: false }, lists: state.lists, deleted_lists: state.deleted, update: null })),
    list: vi.fn(async (id: string) => {
      const list = state.lists.find(item => item.id === id)!;
      return { list, books: [{ ...emptyBook(), id: `${id}-book`, title: `${id} 보관 도서`, held: false, duplicate: false }], summary: { selected_count: 1, total_quantity: 1, list_total: 0, order_total: 0, remaining: list.budget, missing_price_count: 1, review_count: 0, held_count: 0, duplicate_count: 0 } };
    }),
    updateList: vi.fn(async (id: string, values: Partial<AcquisitionList>) => {
      const result = { ...state.lists.find(item => item.id === id)!, ...values };
      state.lists = state.lists.map(item => item.id === id ? result : item);
      return result;
    }),
    deleteList: vi.fn(async (id: string) => {
      if (state.failDelete) throw new Error("저장하지 못했습니다.");
      const result = { ...state.lists.find(item => item.id === id)!, deleted_at: "2026-10-12T00:00:00Z" };
      state.lists = state.lists.filter(item => item.id !== id); state.deleted.push(result);
      return result;
    }),
    restoreList: vi.fn(async (id: string) => {
      const result = { ...state.deleted.find(item => item.id === id)! };
      delete result.deleted_at;
      state.deleted = state.deleted.filter(item => item.id !== id); state.lists.push(result);
      return result;
    }),
  });
  return { api, state };
}

it("edits an inactive list from the menu while keeping the open purchase list", async () => {
  const user = userEvent.setup(), { api } = fixture();
  render(<SimpleLibraryApp api={api} />);
  await screen.findByRole("button", { name: "first 보관 도서" });
  await user.click(screen.getByRole("button", { name: "2학기 구입 목록 수정" }));
  const dialog = screen.getByRole("dialog", { name: "목록과 예산 설정" });
  await user.clear(within(dialog).getByRole("textbox", { name: "목록 이름" }));
  await user.type(within(dialog).getByRole("textbox", { name: "목록 이름" }), "독서 사업");
  await user.clear(within(dialog).getByRole("spinbutton", { name: "예산 (원)" }));
  await user.type(within(dialog).getByRole("spinbutton", { name: "예산 (원)" }), "250000");
  await user.click(within(dialog).getByRole("button", { name: "저장" }));
  expect(await screen.findByRole("button", { name: "독서 사업 목록 수정" })).toBeInTheDocument();
  expect(screen.getByRole("heading", { level: 1, name: "1학기 구입" })).toBeInTheDocument();
  expect(api.updateList).toHaveBeenCalledWith("second", { name: "독서 사업", year: 2026, budget: 250000, discount_percent: 0 });
});

it("confirms list deletion, keeps failed requests reviewable, and restores the selected list with undo", async () => {
  const user = userEvent.setup(), { api, state } = fixture();
  render(<SimpleLibraryApp api={api} />);
  await screen.findByRole("button", { name: "first 보관 도서" });
  await user.click(screen.getByRole("button", { name: "1학기 구입 목록 삭제" }));
  await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "취소" }));
  expect(api.deleteList).not.toHaveBeenCalled();
  state.failDelete = true;
  await user.click(screen.getByRole("button", { name: "1학기 구입 목록 삭제" }));
  await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "목록 삭제" }));
  expect(await within(screen.getByRole("dialog")).findByRole("alert")).toHaveTextContent("저장하지 못했습니다.");
  state.failDelete = false;
  await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "목록 삭제" }));
  expect(await screen.findByRole("button", { name: "second 보관 도서" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "2학기 구입 목록 삭제" })).toBeDisabled();
  await user.click(screen.getByRole("button", { name: "목록 삭제 취소" }));
  expect(await screen.findByRole("button", { name: "first 보관 도서" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "1학기 구입 목록 삭제" })).toBeEnabled();
  expect(api.restoreList).toHaveBeenCalledWith("first");
});

it("restores a previously deleted list after reopening the application", async () => {
  const user = userEvent.setup(), { api } = fixture(true);
  render(<SimpleLibraryApp api={api} />);
  await screen.findByRole("button", { name: "first 보관 도서" });
  await user.click(screen.getByText("삭제한 목록 (1)"));
  await user.click(screen.getByRole("button", { name: "2학기 구입 목록 복원" }));
  expect(await screen.findByRole("button", { name: "second 보관 도서" })).toBeInTheDocument();
  expect(screen.queryByText("삭제한 목록 (1)")).not.toBeInTheDocument();
});
