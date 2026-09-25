import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createLibraryApi } from "../src/simple/api";
import { SimpleLibraryApp } from "../src/simple/SimpleLibraryApp";
import { emptyBook, orderAmount, type AcquisitionList, type Book, type BookFields, type PreviewRow } from "../src/simple/types";

const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } });
const requestUrl = (input: RequestInfo | URL) => typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
const anchorClick = vi.fn();
it("confirms 200 ISBN-less books across pages without changing purchase selections", async () => {
  const user = userEvent.setup();
  const books = Array.from({ length: 200 }, (_, i) => book(`bulk-${i}`, { title: `검토 도서 ${i}`, isbn: "", needs_review: true, selected: i % 2 === 0 }));
  const { api, transport, state } = fixture(books);
  render(<SimpleLibraryApp api={api} />);
  await screen.findByRole("button", { name: "검토 도서 0" });
  expect(screen.queryByRole("button", { name: "검토 도서 199" })).not.toBeInTheDocument();
  expect(screen.getByText("일괄 작업").closest("details")).not.toHaveAttribute("open");
  await user.click(screen.getByText("일괄 작업"));
  await user.keyboard("{Escape}");
  expect(screen.getByText("일괄 작업").closest("details")).not.toHaveAttribute("open");
  await user.click(screen.getByText("일괄 작업"));
  await user.click(screen.getByRole("button", { name: "검색·필터 결과 200건 전체 선택" }));
  await user.click(screen.getByRole("button", { name: "선택 도서 서지 확인 완료" }));
  await waitFor(() => expect(state.books.every(item => !item.needs_review)).toBe(true));
  const request = transport.mock.calls.find(([input]) => requestUrl(input).endsWith("/books/bulk"));
  expect(JSON.parse(typeof request?.[1]?.body === "string" ? request[1].body : "{}")).toMatchObject({ action: "confirm_metadata", book_ids: books.map(item => item.id) });
  expect(state.books.filter(item => item.selected)).toHaveLength(100);
  expect(await screen.findByRole("button", { name: "일괄 작업 되돌리기" })).toBeInTheDocument();
}, 15_000);

it("clears bulk targets when search changes and applies only the new filtered result", async () => {
  const user = userEvent.setup();
  const { api, transport } = fixture([book("a", { title: "사과", isbn: "" }), book("b", { title: "배" })]);
  render(<SimpleLibraryApp api={api} />);
  await screen.findByRole("button", { name: "사과" });
  await user.click(screen.getByText("일괄 작업"));
  await user.click(screen.getByRole("checkbox", { name: "사과 일괄 작업 선택" }));
  await user.type(screen.getByRole("textbox", { name: "도서 검색" }), "배");
  expect(screen.getByRole("button", { name: "선택 도서 보류" })).toBeDisabled();
  await user.click(screen.getByRole("button", { name: "검색·필터 결과 1건 전체 선택" }));
  await user.click(screen.getByRole("button", { name: "선택 도서 보류" }));
  await waitFor(() => expect(transport.mock.calls.some(([input]) => requestUrl(input).endsWith("/books/bulk"))).toBe(true));
  const request = transport.mock.calls.find(([input]) => requestUrl(input).endsWith("/books/bulk"));
  expect(JSON.parse(typeof request?.[1]?.body === "string" ? request[1].body : "{}")).toEqual({ action: "hold", book_ids: ["b"] });
});

it("drops a bulk target when an ordinary purchase change moves it out of the current filter", async () => {
  const user = userEvent.setup();
  const { api } = fixture([book("a", { title: "사과" }), book("b", { title: "배" })]);
  render(<SimpleLibraryApp api={api} />);
  await screen.findByRole("button", { name: "사과" });
  await user.click(screen.getByText("일괄 작업"));
  await user.click(screen.getByRole("button", { name: /^구입 선택2$/ }));
  await user.click(screen.getByRole("checkbox", { name: "사과 일괄 작업 선택" }));
  await user.click(screen.getByRole("checkbox", { name: "사과 구입 선택" }));
  await waitFor(() => expect(screen.queryByRole("checkbox", { name: "사과 일괄 작업 선택" })).not.toBeInTheDocument());
  expect(screen.getByRole("button", { name: "선택 도서 보류" })).toBeDisabled();
});

it("asks before deleting only work-checked books and restores them with bulk undo", async () => {
  const user = userEvent.setup();
  const { api, state, transport } = fixture([book("a", { title: "사과", selected: true }), book("b", { title: "배", selected: true })]);
  render(<SimpleLibraryApp api={api} />);
  await screen.findByRole("button", { name: "사과" });
  await user.click(screen.getByText("일괄 작업"));
  await user.click(screen.getByRole("checkbox", { name: "사과 일괄 작업 선택" }));
  await user.click(screen.getByRole("button", { name: "선택한 책 삭제" }));
  const confirm = screen.getByRole("dialog", { name: "선택한 책을 삭제할까요?" });
  expect(within(confirm).getByText(/작업 체크한 1종/)).toBeInTheDocument();
  await user.click(within(confirm).getByRole("button", { name: "취소" }));
  expect(state.books.map(item => item.id)).toEqual(["a", "b"]);
  expect(transport.mock.calls.some(([input]) => requestUrl(input).endsWith("/books/bulk"))).toBe(false);
  await user.click(screen.getByRole("button", { name: "선택한 책 삭제" }));
  await user.click(within(screen.getByRole("dialog", { name: "선택한 책을 삭제할까요?" })).getByRole("button", { name: "1종 삭제" }));
  await waitFor(() => expect(state.books.map(item => item.id)).toEqual(["b"]));
  expect(screen.queryByRole("button", { name: "사과" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "배" })).toBeInTheDocument();
  const request = transport.mock.calls.find(([input]) => requestUrl(input).endsWith("/books/bulk"));
  expect(JSON.parse(typeof request?.[1]?.body === "string" ? request[1].body : "{}")).toEqual({ action: "delete", book_ids: ["a"] });
  await waitFor(() => expect(screen.getByRole("button", { name: "일괄 작업 되돌리기" })).toHaveFocus());
  await user.click(screen.getByRole("button", { name: "일괄 작업 되돌리기" }));
  await screen.findByRole("button", { name: "사과" });
  expect(state.books.map(item => item.id)).toEqual(["a", "b"]);
});

it("keeps bulk undo available after deleting every book in a list", async () => {
  const user = userEvent.setup();
  const { api } = fixture([book("only", { title: "마지막 책" })]);
  render(<SimpleLibraryApp api={api} />);
  await screen.findByRole("button", { name: "마지막 책" });
  await user.click(screen.getByText("일괄 작업"));
  await user.click(screen.getByRole("checkbox", { name: "마지막 책 일괄 작업 선택" }));
  await user.click(screen.getByRole("button", { name: "선택한 책 삭제" }));
  await user.click(screen.getByRole("button", { name: "1종 삭제" }));
  await screen.findByRole("heading", { name: "도서를 추가해 목록을 시작하세요" });
  await waitFor(() => expect(screen.getByRole("button", { name: "일괄 작업 되돌리기" })).toHaveFocus());
  await user.click(screen.getByRole("button", { name: "일괄 작업 되돌리기" }));
  await screen.findByRole("button", { name: "마지막 책" });
});
it("keeps the bulk deletion confirmation and selection when saving fails", async () => {
  const user = userEvent.setup();
  const { api, state } = fixture([book("a", { title: "사과" })]);
  render(<SimpleLibraryApp api={api} />);
  await screen.findByRole("button", { name: "사과" });
  await user.click(screen.getByText("일괄 작업"));
  await user.click(screen.getByRole("checkbox", { name: "사과 일괄 작업 선택" }));
  await user.click(screen.getByRole("button", { name: "선택한 책 삭제" }));
  state.failBulkDelete = true;
  await user.click(within(screen.getByRole("dialog", { name: "선택한 책을 삭제할까요?" })).getByRole("button", { name: "1종 삭제" }));
  expect(await within(screen.getByRole("dialog", { name: "선택한 책을 삭제할까요?" })).findByRole("alert")).toHaveTextContent("저장 공간에 접근할 수 없습니다.");
  expect(screen.getByRole("checkbox", { name: "사과 일괄 작업 선택" })).toBeChecked();
  expect(state.books.map(item => item.id)).toEqual(["a"]);
  state.failBulkDelete = false;
  await user.click(within(screen.getByRole("dialog", { name: "선택한 책을 삭제할까요?" })).getByRole("button", { name: "1종 삭제" }));
  await waitFor(() => expect(screen.queryByRole("dialog", { name: "선택한 책을 삭제할까요?" })).not.toBeInTheDocument());
  expect(state.books).toHaveLength(0);
});
it("rounds fractional discounts at the same half-won boundary as the order file", () => {
  expect(orderAmount(500, 33.9, 2)).toBe(662);
  expect(orderAmount(15001, 9.7, 3)).toBe(40638);
});
const book = (id: string, fields: Partial<BookFields> = {}): Book => ({ ...emptyBook(), id, title: "어린 왕자", author: "생텍쥐페리", publisher: "문학출판", isbn: "9788936434267", price: 10_000, held: false, duplicate: false, ...fields });
function fixture(initial: Book[] = []) {
  const state = { display: { text_size: 16, row_density: "comfortable" }, books: initial, deleted: null as Book | null, bulkDeleted: [] as { book: Book; index: number }[], failSave: false, failBulkDelete: false, failExport: false, imports: null as null | { kind: string; rows: PreviewRow[] }, exports: null as null | Record<string, unknown>, lookupCount: 0, restoreCount: 0 };
  const list: AcquisitionList = { id: "list-1", name: "2026 2학기 도서 구입", year: 2026, budget: 15_000_000, discount_percent: 10, created_at: "2026-09-15" };
  const preview: PreviewRow = { ...emptyBook(), title: "가져온 제목", isbn: "9788936434267", price: null, needs_review: true, source: "학교 추천.xlsx", raw_values: { 도서명: "가져온 제목" }, provenance: { filename: "학교 추천.xlsx", row: 2 } };
  const transport = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = requestUrl(input).replace("/api/library", ""), method = init?.method ?? "GET";
    const payload = typeof init?.body === "string" ? JSON.parse(init.body) as Record<string, unknown> : {};
    if (path !== "/bootstrap" && method !== "GET") expect(new Headers(init?.headers).get("X-Suseoro-Token")).toBe("test-token");
    if (path === "/bootstrap") return json({ version: "2.0.0", csrf_token: "test-token", settings: { school_name: "햇살초등학교", nl_api_key_configured: false, ...state.display }, lists: [list], update: null });
    if (path === "/updates/status") return json({ available: false, phase: "idle", auto_enabled: false, auto_supported: false });
    if (path === "/shutdown") return json({ ok: true });
    if (path === "/lists/list-1" && method === "GET") {
      const selected = state.books.filter(item => item.selected), amount = selected.reduce((total, item) => total + Math.round((item.price ?? 0) * .9) * item.quantity, 0);
      return json({ list, books: state.books, summary: { selected_count: selected.length, total_quantity: selected.reduce((total, item) => total + item.quantity, 0), list_total: selected.reduce((total, item) => total + (item.price ?? 0) * item.quantity, 0), order_total: amount, remaining: list.budget - amount, missing_price_count: selected.filter(item => item.price === null).length, review_count: state.books.filter(item => item.needs_review).length, held_count: 0, duplicate_count: 0 } });
    }
    if (path === "/lists/list-1/books" && method === "POST") {
      if (state.failSave) return json({ detail: "저장 공간에 접근할 수 없습니다." }, 503);
      const added = book(`book-${state.books.length + 1}`, payload); state.books.push(added); return json(added);
    }
    if (/\/books\/[^/]+\/restore$/.test(path)) { if (state.deleted) state.books.push(state.deleted); return json(state.deleted); }
    if (path === "/lists/list-1/books/bulk") {
      const ids = payload.book_ids as string[];
      if (payload.action === "delete") { if (state.failBulkDelete) return json({ detail: "저장 공간에 접근할 수 없습니다." }, 503); state.bulkDeleted = state.books.flatMap((item, index) => ids.includes(item.id) ? [{ book: item, index }] : []); state.books = state.books.filter(item => !ids.includes(item.id)); return json({ updated: ids.length, skipped: [], operation_id: "bulk-1" }); }
      for (const item of state.books.filter(item => ids.includes(item.id))) {
        if (payload.action === "confirm_metadata") { item.needs_review = false; item.warnings = []; }
        else item.selected = payload.action === "select";
      }
      return json({ updated: ids.length, skipped: [], operation_id: "bulk-1" });
    }
    if (/\/books\/[^/]+$/.test(path)) {
      const id = path.split("/").at(-1), current = state.books.find(item => item.id === id);
      if (method === "DELETE") { state.deleted = current ?? null; state.books = state.books.filter(item => item.id !== id); return json({ ok: true }); }
      if (method === "PATCH" && current) { if (state.failSave) return json({ detail: "저장 공간에 접근할 수 없습니다." }, 503); Object.assign(current, payload); return json(current); }
    }
    if (path === "/lookup") { state.lookupCount += 1; if (payload.isbn === "invalid") return json({ detail: "ISBN 체크숫자가 올바르지 않습니다." }, 400); return json({ found: true, book: { ...emptyBook(), title: "조회한 다른 제목", author: "조회 저자", publisher: "조회 출판사", isbn: String(payload.isbn), price: 15_000, source: "국립중앙도서관", needs_review: true }, warnings: ["예정가격을 확인해 주세요."] }); }
    if (path === "/imports/preview") return json({ import_id: "import-1", filename: "학교 추천.xlsx", rows: [preview], warnings: [] });
    if (path === "/lists/list-1/imports/batch") {
      const rows = (payload.imports as { rows: PreviewRow[] }[]).flatMap(item => item.rows);
      state.imports = { kind: "recommendations", rows };
      state.books.push(...rows.map((row, index) => book(`imported-${index}`, row)));
      return json({ added: rows.length, merged: 0, input_count: rows.length, warnings: [], operation_id: "import-op" });
    }
    if (path === "/lists/list-1/operations/import-op/undo") { state.books = state.books.filter(item => !item.id.startsWith("imported-")); return json({ restored: 1 }); }
    if (path === "/lists/list-1/operations/bulk-1/undo") { for (const item of state.bulkDeleted) state.books.splice(item.index, 0, item.book); state.bulkDeleted = []; return json({ restored: 1 }); }
    if (path === "/lists/list-1/imports") { state.imports = payload as typeof state.imports; const rows = payload.rows as PreviewRow[]; if (payload.kind === "recommendations") state.books.push(...rows.map((row, index) => book(`imported-${index}`, row))); return json({ added: rows.length, warnings: [] }); }
    if (path === "/templates") return json([]);
    if (path === "/lists/list-1/export") { state.exports = payload; return state.failExport ? json({ detail: "선택한 책의 ISBN을 확인해 주세요." }, 400) : new Response("export-content", { headers: { "Content-Disposition": "attachment; filename=order.csv" } }); }
    if (path === "/settings") { if (typeof payload.text_size === "number") state.display.text_size = payload.text_size; if (typeof payload.row_density === "string") state.display.row_density = payload.row_density; return json({ school_name: payload.school_name ?? "햇살초등학교", nl_api_key_configured: !!payload.nl_api_key, ...state.display }); }
    if (path === "/restore") { state.restoreCount += 1; return json({ ok: true }); }
    return json({ detail: `Unexpected route: ${method} ${path}` }, 404);
  });
  return { state, transport, api: createLibraryApi(transport) };
}

beforeEach(() => {
  localStorage.clear();
  Object.defineProperty(URL, "createObjectURL", { configurable: true, value: vi.fn(() => "blob:export") });
  Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: vi.fn() });
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(anchorClick);
});

it("uses the left rail for budget and actions while the book table owns the main workspace", async () => {
  render(<SimpleLibraryApp api={fixture([book("one")]).api} />);
  const budget = await screen.findByRole("region", { name: "예산 현황" });
  const sidebar = budget.closest("aside");
  expect(sidebar).not.toBeNull();
  expect(within(sidebar!).getByRole("button", { name: "ISBN 추가" })).toBeInTheDocument();
  expect(within(sidebar!).getByRole("button", { name: "파일 가져오기" })).toBeInTheDocument();
  expect(within(sidebar!).getByRole("button", { name: "발주서 저장" })).toBeInTheDocument();
  expect(within(screen.getByRole("main")).getByRole("table", { name: "도서 목록" })).toBeInTheDocument();
});

it("opens registered collections from the side menu and returns to the purchase table", async () => {
  const user = userEvent.setup();
  render(<SimpleLibraryApp api={fixture([book("one", { source: "교육청 추천.xlsx" })]).api} />);
  await screen.findByRole("button", { name: "어린 왕자" });
  await user.click(screen.getByRole("button", { name: "추천도서 목록 열람" }));
  expect(screen.getByRole("heading", { level: 1, name: "추천도서 목록" })).toBeInTheDocument();
  expect(screen.queryByRole("table", { name: "도서 목록" })).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "구입 목록으로 돌아가기" }));
  expect(screen.getByRole("table", { name: "도서 목록" })).toBeInTheDocument();
});

it("keeps the recommendations view when the current purchase list is selected", async () => {
  const user = userEvent.setup();
  render(<SimpleLibraryApp api={fixture([book("one", { source: "교육청 추천.xlsx" })]).api} />);
  await screen.findByRole("button", { name: "어린 왕자" });
  await user.click(screen.getByRole("button", { name: "추천도서 목록 열람" }));
  await user.click(screen.getByRole("button", { name: "2026 2학기 도서 구입" }));
  expect(screen.getByRole("heading", { level: 1, name: "추천도서 목록" })).toBeInTheDocument();
});

it("shows loading and the next list's saved books when switching lists in recommendations", async () => {
  const user = userEvent.setup();
  const original = fixture([book("one", { title: "첫 목록 도서" })]).api;
  const bootstrap = await original.bootstrap();
  const firstDetail = await original.list("list-1");
  const secondList = { ...firstDetail.list, id: "list-2", name: "2026 겨울 도서" };
  let resolveSecond!: (value: typeof firstDetail) => void;
  const delayed = new Promise<typeof firstDetail>(resolve => { resolveSecond = resolve; });
  const api = {
    ...original,
    bootstrap: async () => ({ ...bootstrap, lists: [...bootstrap.lists, secondList] }),
    list: (id: string) => id === secondList.id ? delayed : original.list(id),
  };
  render(<SimpleLibraryApp api={api} />);
  await screen.findByRole("button", { name: "첫 목록 도서" });
  await user.click(screen.getByRole("button", { name: "추천도서 목록 열람" }));
  await user.click(screen.getByRole("button", { name: "2026 겨울 도서" }));
  expect(screen.getByRole("status")).toHaveTextContent("추천도서 목록을 불러오고 있습니다.");
  resolveSecond({ ...firstDetail, list: secondList, books: [book("two", { title: "둘째 목록 도서" })] });
  expect(await screen.findByText("둘째 목록 도서")).toBeInTheDocument();
  expect(screen.queryByText("첫 목록 도서")).not.toBeInTheDocument();
});

it("returns focus after closing the mobile menu and closes it when a task opens", async () => {
  const user = userEvent.setup();
  render(<SimpleLibraryApp api={fixture([book("one")]).api} />);
  await screen.findByRole("button", { name: "어린 왕자" });
  const open = screen.getByRole("button", { name: "메뉴 열기" });
  await user.click(open);
  expect(screen.getByRole("button", { name: "메뉴 닫기" })).toHaveFocus();
  await user.keyboard("{Escape}");
  expect(open).toHaveFocus();
  await user.click(open);
  await user.click(screen.getByRole("button", { name: "ISBN 추가" }));
  expect(document.querySelector(".simple-app")).not.toHaveClass("menu-open");
  await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "닫기" }));
  expect(open).toHaveFocus();
});

it("exposes the full imported source while keeping its table cell compact", async () => {
  const source = "교육청 · 가을 추천.xlsx · 추천도서 · 129행";
  render(<SimpleLibraryApp api={fixture([book("one", { source })]).api} />);
  const label = await screen.findByText(source);
  expect(label).toHaveAttribute("title", source);
});

it("opens canonical ISBN search links from an existing book and rejects executable references", async () => {
  const { api } = fixture([book("ten", { isbn: "0-306-40615-2", link: "javascript:alert(1)" })]);
  render(<SimpleLibraryApp api={api} />);
  expect(await screen.findByRole("link", { name: "어린 왕자 알라딘 ISBN 검색" })).toHaveAttribute("href", "https://www.aladin.co.kr/search/wsearchresult.aspx?SearchTarget=Book&SearchWord=9780306406157");
  expect(screen.getByRole("link", { name: "어린 왕자 국립중앙도서관 ISBN 검색" })).toHaveAttribute("href", "https://www.nl.go.kr/NL/contents/search.do?kwd=9780306406157");
  expect(screen.queryByRole("link", { name: "어린 왕자 참고 링크" })).not.toBeInTheDocument();
});

it("loads canonical ISBN thumbnails and keeps an accessible placeholder when no cover exists", async () => {
  render(<SimpleLibraryApp api={fixture([book("ten", { isbn: "0-306-40615-2" }), book("none", { title: "ISBN 없는 책", isbn: "" })]).api} />);
  const cover = await screen.findByRole("img", { name: "어린 왕자 표지" });
  expect(cover).toHaveAttribute("src", "/api/library/covers/9780306406157");
  expect(cover).toHaveAttribute("loading", "lazy");
  expect(screen.getByRole("img", { name: "ISBN 없는 책 표지 없음" })).toBeInTheDocument();
  fireEvent.error(cover);
  expect(screen.getByRole("status")).toHaveTextContent("재시도 중");
  expect(screen.queryByRole("img", { name: "어린 왕자 표지" })).not.toBeInTheDocument();
});

it("shows holdings beside titles and filters library duplicates independently of purchase selection", async () => {
  const user = userEvent.setup();
  const held = { ...book("held", { title: "소장된 책", selected: true }), held: true, holdings_status: "held" as const, held_match: "isbn" as const };
  const absent = { ...book("absent", { title: "새 책" }), holdings_status: "not_held" as const };
  render(<SimpleLibraryApp api={fixture([held, absent]).api} />);
  const title = await screen.findByRole("button", { name: "소장된 책" });
  expect(within(title.closest("td")!).getByText("소장 중 · 중복")).toBeInTheDocument();
  expect(screen.getByText("ISBN 일치")).toBeInTheDocument();
  expect(screen.getByText("소장목록에 없음")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: /소장 중복/ }));
  expect(screen.queryByRole("button", { name: "새 책" })).not.toBeInTheDocument();
  expect(screen.getByRole("checkbox", { name: "소장된 책 구입 선택" })).toBeChecked();
});

it("does not claim a book is absent before holdings are uploaded", async () => {
  render(<SimpleLibraryApp api={fixture([book("unchecked")]).api} />);
  expect(await screen.findByText("소장 확인 전")).toBeInTheDocument();
  expect(screen.queryByText("소장목록에 없음")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "소장목록 가져오기" })).toBeInTheDocument();
});

it("lets users enlarge text to 24px with visible controls and persists the exact choice", async () => {
  const user = userEvent.setup(), { api, state } = fixture();
  render(<SimpleLibraryApp api={api} />);
  await screen.findByRole("region", { name: "예산 현황" });
  await user.selectOptions(screen.getByRole("combobox", { name: "글자 크기" }), "20");
  await user.click(screen.getByRole("button", { name: "글자 크게" }));
  expect(document.documentElement.style.fontSize).toBe("22px");
  await user.click(screen.getByRole("button", { name: "글자 크게" }));
  expect(document.documentElement.style.fontSize).toBe("24px");
  expect(screen.getByRole("button", { name: "글자 크게" })).toBeDisabled();
  await waitFor(() => expect(state.display.text_size).toBe(24));
  await user.click(screen.getByRole("button", { name: "글자 작게" }));
  expect(document.documentElement.style.fontSize).toBe("22px");
});

it("restores text and density from app data even when a new launch has no localStorage", async () => {
  const user = userEvent.setup(), { api, state } = fixture();
  const first = render(<SimpleLibraryApp api={api} />);
  await screen.findByRole("region", { name: "예산 현황" });
  const size = await screen.findByRole("combobox", { name: "글자 크기" });
  await user.selectOptions(size, "22");
  expect(document.documentElement.style.fontSize).toBe("22px");
  await user.click(screen.getByRole("button", { name: "간결한 행 간격" }));
  expect(document.querySelector(".simple-app")).toHaveClass("density-compact");
  expect(document.documentElement.style.fontSize).toBe("22px");
  await waitFor(() => expect(state.display).toEqual({ text_size: 22, row_density: "compact" }));
  first.unmount();
  localStorage.clear();
  render(<SimpleLibraryApp api={api} />);
  await screen.findByRole("region", { name: "예산 현황" });
  expect(screen.getByRole("combobox", { name: "글자 크기" })).toHaveValue("22");
  expect(document.querySelector(".simple-app")).toHaveClass("density-compact");
});

it("focuses book search with Ctrl+F without interrupting an open editor", async () => {
  const user = userEvent.setup();
  render(<SimpleLibraryApp api={fixture([book("one")]).api} />);
  await screen.findByRole("button", { name: "어린 왕자" });
  await user.keyboard("{Control>}f{/Control}");
  expect(screen.getByRole("textbox", { name: "도서 검색" })).toHaveFocus();
  await user.click(screen.getByRole("button", { name: "어린 왕자 수정" }));
  const title = screen.getByLabelText(/책 제목/);
  await user.click(title);
  await user.keyboard("{Control>}f{/Control}");
  expect(title).toHaveFocus();
});

it("registers the ISBN provider without losing pending ISBN input", async () => {
  const user = userEvent.setup(), { api, transport } = fixture();
  render(<SimpleLibraryApp api={api} />);
  await user.click(await screen.findByRole("button", { name: "ISBN 추가" }));
  expect(screen.getByLabelText("ISBN")).toHaveFocus();
  await user.type(screen.getByLabelText("ISBN"), "9788936434267");
  await user.click(screen.getByText("국립중앙도서관 인증키 등록"));
  await user.type(screen.getByLabelText("국립중앙도서관 인증키"), "test-key-only");
  await user.click(screen.getByRole("button", { name: "인증키 저장" }));
  await screen.findByText("국립중앙도서관 인증키 등록됨");
  expect(screen.getByLabelText("ISBN")).toHaveValue("9788936434267");
  expect(screen.getByLabelText("국립중앙도서관 인증키")).toHaveValue("");
  const saved = transport.mock.calls.find(call => requestUrl(call[0]).endsWith("/settings"));
  const body = saved?.[1]?.body;
  expect(JSON.parse(typeof body === "string" ? body : "{}")).toEqual({ nl_api_key: "test-key-only" });
});

it("keeps original row indexes when filtering and correcting imports and preserves diagnostics", async () => {
  const user = userEvent.setup(), { api, state } = fixture();
  vi.spyOn(api, "preview").mockResolvedValue({ import_id: "import-1", filename: "다른 양식.xlsx", warnings: [], rows: [
    { ...emptyBook(), title: "정상 도서", price: 10000, source_sheet: "추천목록", source_row: 5 },
    { ...emptyBook(), title: "확인 도서", price: null, needs_review: true, source_sheet: "추천목록", source_row: 6, warnings: ["정가 확인"] },
  ], diagnostics: [{ kind: "preamble", sheet: "추천목록", row: 1, message: "안내문", raw_text: "2026 사서 추천도서" }] });
  render(<SimpleLibraryApp api={api} />);
  await user.click(await screen.findByRole("button", { name: "파일 가져오기" }));
  await user.upload(screen.getByLabelText("가져올 파일"), new File(["fixture"], "다른 양식.xlsx"));
  await screen.findByLabelText("2행 책 제목");
  expect(screen.getByText("추천목록 · 원본 6행")).toBeInTheDocument();
  await user.click(screen.getByLabelText("확인 필요한 행만 보기 (1)"));
  expect(screen.queryByLabelText("1행 책 제목")).not.toBeInTheDocument();
  await user.clear(screen.getByLabelText("2행 책 제목"));
  await user.type(screen.getByLabelText("2행 책 제목"), "확인 완료 제목");
  await user.type(screen.getByLabelText("2행 정가"), "17000");
  await user.click(screen.getByText("안내문·반복 머리글·합계 1행 별도 보관"));
  expect(screen.getByText("2026 사서 추천도서")).toBeVisible();
  await user.click(screen.getByRole("button", { name: "2건 가져오기" }));
  await waitFor(() => expect(state.imports?.rows).toHaveLength(2));
  expect(state.imports?.rows[0].title).toBe("정상 도서");
  expect(state.imports?.rows[1]).toMatchObject({ title: "확인 완료 제목", price: 17000, source_row: 6 });
});

it("keeps a filtered unknown-price row mounted until the entire price has been entered", async () => {
  const user = userEvent.setup(), { api, state } = fixture();
  vi.spyOn(api, "preview").mockResolvedValue({ import_id: "import-1", filename: "가격.xlsx", warnings: [], rows: [{ ...emptyBook(), title: "정가 미확인", price: null, needs_review: false }] });
  render(<SimpleLibraryApp api={api} />);
  await user.click(await screen.findByRole("button", { name: "파일 가져오기" }));
  await user.upload(screen.getByLabelText("가져올 파일"), new File(["fixture"], "가격.xlsx"));
  await user.click(await screen.findByLabelText("확인 필요한 행만 보기 (1)"));
  await user.type(screen.getByLabelText("1행 정가"), "12000");
  expect(screen.getByLabelText("1행 정가")).toHaveValue(12000);
  expect(screen.getByLabelText("1행 정가")).toHaveFocus();
  await user.click(screen.getByRole("button", { name: "1건 가져오기" }));
  await waitFor(() => expect(state.imports?.rows[0].price).toBe(12000));
});

it("locks book fields while ISBN lookup is pending so metadata cannot move to another ISBN", async () => {
  const user = userEvent.setup(), { api } = fixture([book("one", { author: "" })]);
  let finish!: (result: Awaited<ReturnType<typeof api.lookup>>) => void;
  vi.spyOn(api, "lookup").mockReturnValue(new Promise(resolve => { finish = resolve; }));
  render(<SimpleLibraryApp api={api} />);
  await user.click(await screen.findByRole("button", { name: "어린 왕자 수정" }));
  await user.click(screen.getByRole("button", { name: "ISBN으로 빈 정보 채우기" }));
  expect(screen.getByLabelText("ISBN")).toBeDisabled();
  expect(screen.getByLabelText(/책 제목/)).toBeDisabled();
  finish({ found: true, book: { ...emptyBook(), author: "조회 저자", isbn: "9788936434267" }, warnings: [] });
  await waitFor(() => expect(screen.getByLabelText("ISBN")).toBeEnabled());
  expect(screen.getByLabelText("ISBN")).toHaveValue("9788936434267");
  expect(screen.getByLabelText("저자")).toHaveValue("조회 저자");
});

describe("개인 수서 목록", () => {
  it("shows discounted budget and distinguishes unknown prices, then recalculates after purchase selection", async () => {
    const user = userEvent.setup(), { api } = fixture([book("known", { quantity: 2 }), book("unknown", { title: "가격을 확인할 책", price: null })]);
    render(<SimpleLibraryApp api={api} />);
    const budget = await screen.findByRole("region", { name: "예산 현황" });
    expect(within(budget).getByText("15,000,000원")).toBeInTheDocument();
    expect(within(budget).getByText("18,000원")).toBeInTheDocument();
    expect(within(budget).getByText("14,982,000원")).toBeInTheDocument();
    expect(screen.getByText("가격 미확인 1종은 선택 금액에 포함되지 않았어요.")).toBeInTheDocument();
    await user.click(screen.getByRole("checkbox", { name: "어린 왕자 구입 선택" }));
    await waitFor(() => expect(within(budget).getByText("0원")).toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: /보류\s*1/ }));
    expect(screen.getByRole("button", { name: "어린 왕자" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "가격을 확인할 책" })).not.toBeInTheDocument();
  });

  it("adds a manual book, preserves edits on a server error, edits, deletes and restores it", async () => {
    const user = userEvent.setup(), { api, state } = fixture();
    render(<SimpleLibraryApp api={api} />);
    await user.click(await screen.findByRole("button", { name: "＋ 직접 추가" }));
    let dialog = screen.getByRole("dialog", { name: "책 직접 추가" });
    await user.type(within(dialog).getByLabelText(/책 제목/), "사서의 책");
    await user.type(within(dialog).getByLabelText("정가 (원)"), "12000");
    state.failSave = true;
    await user.click(within(dialog).getByRole("button", { name: "저장" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("저장 공간에 접근할 수 없습니다.");
    expect(within(dialog).getByLabelText(/책 제목/)).toHaveValue("사서의 책");
    state.failSave = false;
    await user.click(within(dialog).getByRole("button", { name: "저장" }));
    await screen.findByRole("button", { name: "사서의 책" });
    await user.click(screen.getByRole("button", { name: "사서의 책 수정" }));
    dialog = screen.getByRole("dialog", { name: "책 정보 수정" });
    await user.clear(within(dialog).getByLabelText("수량")); await user.type(within(dialog).getByLabelText("수량"), "3");
    await user.click(within(dialog).getByRole("button", { name: "저장" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(state.books[0].quantity).toBe(3);
    await user.click(screen.getByRole("button", { name: "사서의 책 수정" }));
    await user.click(screen.getByRole("button", { name: "이 책 삭제" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "사서의 책" })).not.toBeInTheDocument());
    await user.click(await screen.findByRole("button", { name: "삭제 취소" }));
    expect(await screen.findByRole("button", { name: "사서의 책" })).toBeInTheDocument();
  });

  it("previews and corrects import rows and submits the edited values", async () => {
    const user = userEvent.setup(), { api, state } = fixture(); render(<SimpleLibraryApp api={api} />);
    await user.click(await screen.findByRole("button", { name: "파일 가져오기" }));
    await user.upload(screen.getByLabelText("가져올 파일"), new File(["sheet"], "추천.xlsx", { type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" }));
    const title = await screen.findByLabelText("1행 책 제목");
    await user.clear(title); await user.type(title, "확인한 추천도서");
    await user.type(screen.getByLabelText("1행 정가"), "16000");
    await user.click(screen.getByRole("button", { name: "1건 가져오기" }));
    expect(await screen.findByRole("button", { name: "확인한 추천도서" })).toBeInTheDocument();
    expect(state.imports?.kind).toBe("recommendations");
    expect(state.imports?.rows[0]).toMatchObject({ title: "확인한 추천도서", price: 16000, source: "학교 추천.xlsx", raw_values: { 도서명: "가져온 제목" } });
  });

  it("enriches missing import fields without replacing the source title or confirmed price", async () => {
    const user = userEvent.setup(), { api, state } = fixture(); render(<SimpleLibraryApp api={api} />);
    await user.click(await screen.findByRole("button", { name: "파일 가져오기" }));
    await user.upload(screen.getByLabelText("가져올 파일"), new File(["sheet"], "추천.xlsx"));
    await screen.findByLabelText("1행 책 제목");
    await user.type(screen.getByLabelText("1행 정가"), "19000");
    await user.click(screen.getByRole("button", { name: "ISBN으로 빈 정보 채우기" }));
    await screen.findByText(/1건의 빈 정보를 채웠습니다/);
    expect(screen.getByLabelText("1행 책 제목")).toHaveValue("가져온 제목");
    expect(screen.getByLabelText("1행 정가")).toHaveValue(19000);
    expect(screen.getByLabelText("1행 저자")).toHaveValue("조회 저자");
    expect(state.lookupCount).toBe(1);
    await user.click(screen.getByRole("button", { name: "1건 가져오기" }));
    await waitFor(() => expect(state.imports?.rows[0].needs_review).toBe(true));
    expect(state.imports?.rows[0].source).toBe("학교 추천.xlsx");
  });

  it("keeps successful ISBN additions when another ISBN fails and does not re-add them", async () => {
    const user = userEvent.setup(), { api, state } = fixture(); render(<SimpleLibraryApp api={api} />);
    await user.click(await screen.findByRole("button", { name: "ISBN 추가" }));
    await user.type(screen.getByLabelText("ISBN"), "9788936434267\ninvalid");
    await user.click(screen.getByRole("button", { name: "조회하고 추가" }));
    await screen.findByText("ISBN 체크숫자가 올바르지 않습니다.");
    expect(state.books).toHaveLength(1); expect(state.books[0].needs_review).toBe(true);
    expect(state.books[0].warnings).toContain("예정가격을 확인해 주세요.");
    await user.click(screen.getByRole("button", { name: "조회하고 추가" }));
    await screen.findByText("이번 창에서 이미 추가한 ISBN입니다.");
    expect(state.books).toHaveLength(1);
  });

  it("exports a token-protected CSV and displays backend validation errors", async () => {
    const user = userEvent.setup(), { api, state, transport } = fixture([book("ready")]); render(<SimpleLibraryApp api={api} />);
    await user.click(await screen.findByRole("button", { name: "발주서 저장" }));
    const dialog = screen.getByRole("dialog", { name: "발주서 저장" });
    await user.click(within(dialog).getByRole("radio", { name: /CSV/ }));
    state.failExport = true;
    await user.click(within(dialog).getByRole("button", { name: "발주서 저장" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("선택한 책의 ISBN을 확인해 주세요.");
    state.failExport = false;
    await user.click(within(dialog).getByRole("button", { name: "발주서 저장" }));
    await within(dialog).findByText(/발주서 다운로드를 시작했습니다/);
    expect(state.exports).toEqual({ format: "csv" });
    expect(anchorClick).toHaveBeenCalledOnce();
    const exportCall = transport.mock.calls.find(call => requestUrl(call[0]).endsWith("/export"));
    expect(new Headers(exportCall?.[1]?.headers).get("X-Suseoro-Token")).toBe("test-token");
  });

  it("blocks final export for a selected unknown price without pretending it is zero", async () => {
    const user = userEvent.setup(), { api } = fixture([book("unpriced", { price: null })]); render(<SimpleLibraryApp api={api} />);
    await user.click(await screen.findByRole("button", { name: "발주서 저장" }));
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText("가격 미확인 1종의 정가를 입력해 주세요.")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "발주서 저장" })).toBeDisabled();
  });

  it("can end the local server from the browser fallback", async () => {
    const user = userEvent.setup(), { api } = fixture(); render(<SimpleLibraryApp api={api} />);
    await screen.findByRole("region", { name: "예산 현황" });
    await user.click(screen.getByRole("button", { name: "학교 설정 · 백업" }));
    await user.click(screen.getByRole("button", { name: "수서로 종료" }));
    expect(await screen.findByRole("heading", { name: "수서로를 종료했습니다." })).toBeInTheDocument();
  });

  it("requires a concrete backup replacement confirmation before restoration", async () => {
    const user = userEvent.setup(), { api, state } = fixture(); render(<SimpleLibraryApp api={api} />);
    await screen.findByRole("region", { name: "예산 현황" });
    await user.click(screen.getByRole("button", { name: "학교 설정 · 백업" }));
    await user.upload(screen.getByLabelText("복원할 백업 파일"), new File(["{}"], "우리학교-백업.json", { type: "application/json" }));
    const restore = screen.getByRole("button", { name: "이 백업으로 복원" });
    expect(restore).toBeDisabled(); expect(screen.getByText("우리학교-백업.json")).toBeInTheDocument();
    await user.click(screen.getByRole("checkbox", { name: "현재 자료가 교체되는 것을 확인했습니다." }));
    await user.click(restore);
    await waitFor(() => expect(state.restoreCount).toBe(1));
  });
});
