import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createLibraryApi } from "../src/simple/api";
import { SimpleLibraryApp } from "../src/simple/SimpleLibraryApp";
import { emptyBook, type AcquisitionList, type Book, type BookFields, type PreviewRow } from "../src/simple/types";

const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } });
const requestUrl = (input: RequestInfo | URL) => typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
const anchorClick = vi.fn();
const book = (id: string, fields: Partial<BookFields> = {}): Book => ({ ...emptyBook(), id, title: "어린 왕자", author: "생텍쥐페리", publisher: "문학출판", isbn: "9788936434267", price: 10_000, held: false, duplicate: false, ...fields });
function fixture(initial: Book[] = []) {
  const state = { books: initial, deleted: null as Book | null, failSave: false, failExport: false, imports: null as null | { kind: string; rows: PreviewRow[] }, exports: null as null | Record<string, unknown>, lookupCount: 0, restoreCount: 0 };
  const list: AcquisitionList = { id: "list-1", name: "2026 2학기 도서 구입", year: 2026, budget: 15_000_000, discount_percent: 10, created_at: "2026-09-15" };
  const preview: PreviewRow = { ...emptyBook(), title: "가져온 제목", isbn: "9788936434267", price: null, needs_review: true, source: "학교 추천.xlsx", raw_values: { 도서명: "가져온 제목" }, provenance: { filename: "학교 추천.xlsx", row: 2 } };
  const transport = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = requestUrl(input).replace("/api/library", ""), method = init?.method ?? "GET";
    const payload = typeof init?.body === "string" ? JSON.parse(init.body) as Record<string, unknown> : {};
    if (path !== "/bootstrap" && method !== "GET") expect(new Headers(init?.headers).get("X-Suseoro-Token")).toBe("test-token");
    if (path === "/bootstrap") return json({ version: "2.0.0", csrf_token: "test-token", settings: { school_name: "햇살초등학교", nl_api_key_configured: false }, lists: [list], update: null });
    if (path === "/lists/list-1" && method === "GET") {
      const selected = state.books.filter(item => item.selected), amount = selected.reduce((total, item) => total + Math.round((item.price ?? 0) * .9) * item.quantity, 0);
      return json({ list, books: state.books, summary: { selected_count: selected.length, total_quantity: selected.reduce((total, item) => total + item.quantity, 0), list_total: selected.reduce((total, item) => total + (item.price ?? 0) * item.quantity, 0), order_total: amount, remaining: list.budget - amount, missing_price_count: selected.filter(item => item.price === null).length, review_count: state.books.filter(item => item.needs_review).length, held_count: 0, duplicate_count: 0 } });
    }
    if (path === "/lists/list-1/books" && method === "POST") {
      if (state.failSave) return json({ detail: "저장 공간에 접근할 수 없습니다." }, 503);
      const added = book(`book-${state.books.length + 1}`, payload); state.books.push(added); return json(added);
    }
    if (/\/books\/[^/]+\/restore$/.test(path)) { if (state.deleted) state.books.push(state.deleted); return json(state.deleted); }
    if (/\/books\/[^/]+$/.test(path)) {
      const id = path.split("/").at(-1), current = state.books.find(item => item.id === id);
      if (method === "DELETE") { state.deleted = current ?? null; state.books = state.books.filter(item => item.id !== id); return json({ ok: true }); }
      if (method === "PATCH" && current) { if (state.failSave) return json({ detail: "저장 공간에 접근할 수 없습니다." }, 503); Object.assign(current, payload); return json(current); }
    }
    if (path === "/lookup") { state.lookupCount += 1; if (payload.isbn === "invalid") return json({ detail: "ISBN 체크숫자가 올바르지 않습니다." }, 400); return json({ found: true, book: { ...emptyBook(), title: "조회한 다른 제목", author: "조회 저자", publisher: "조회 출판사", isbn: String(payload.isbn), price: 15_000, source: "국립중앙도서관", needs_review: true }, warnings: ["예정가격을 확인해 주세요."] }); }
    if (path === "/imports/preview") return json({ import_id: "import-1", filename: "학교 추천.xlsx", rows: [preview], warnings: [] });
    if (path === "/lists/list-1/imports") { state.imports = payload as typeof state.imports; const rows = payload.rows as PreviewRow[]; if (payload.kind === "recommendations") state.books.push(...rows.map((row, index) => book(`imported-${index}`, row))); return json({ added: rows.length, warnings: [] }); }
    if (path === "/templates") return json([]);
    if (path === "/lists/list-1/export") { state.exports = payload; return state.failExport ? json({ detail: "선택한 책의 ISBN을 확인해 주세요." }, 400) : new Response("export-content", { headers: { "Content-Disposition": "attachment; filename=order.csv" } }); }
    if (path === "/settings") return json({ school_name: payload.school_name ?? "햇살초등학교", nl_api_key_configured: !!payload.nl_api_key });
    if (path === "/restore") { state.restoreCount += 1; return json({ ok: true }); }
    return json({ detail: `Unexpected route: ${method} ${path}` }, 404);
  });
  return { state, transport, api: createLibraryApi(transport) };
}

beforeEach(() => {
  Object.defineProperty(URL, "createObjectURL", { configurable: true, value: vi.fn(() => "blob:export") });
  Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: vi.fn() });
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(anchorClick);
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
