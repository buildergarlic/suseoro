import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { createLibraryApi } from "../src/simple/api";
import { ImportDialog } from "../src/simple/ImportDialog";
import { emptyBook, type ImportPreview, type PreviewRow } from "../src/simple/types";

const row = (title: string, sourceRow = 2): PreviewRow => ({ ...emptyBook(), title, author: "저자", publisher: "출판사", price: 10000, source: "추천기관", source_sheet: "추천", source_row: sourceRow });
const preview = (filename: string, rows = [row(filename)]): ImportPreview => ({ import_id: filename, filename, rows, warnings: [], headers: ["책 이름", "지은이", "값"], mapping: { title: "책 이름", author: "지은이", price: "값" } });
const file = (name: string) => new File([name], name);
function setup(initialKind: "recommendations" | "holdings" = "recommendations") {
  const api = Object.assign(createLibraryApi(), {
    preview: vi.fn(async (input: File) => preview(input.name)),
    remapPreview: vi.fn(async (id: string, _mapping: Record<string, string>) => preview(id)),
    importBatch: vi.fn(async (_id: string, imports: { import_id: string; rows: PreviewRow[] }[], _deduplicate: boolean, _requestId: string) => ({ added: imports.reduce((sum, item) => sum + item.rows.length, 0), merged: 0, input_count: imports.reduce((sum, item) => sum + item.rows.length, 0), warnings: [], operation_id: "operation-1" })),
    importRows: vi.fn(async (_id: string, _importId: string, rows: PreviewRow[], _kind: "recommendations" | "holdings") => ({ added: rows.length, warnings: [] })),
  });
  const onImported = vi.fn(async (_message: string, _operationId?: string) => undefined), onClose = vi.fn();
  render(<ImportDialog api={api} listId="list-1" initialKind={initialKind} onImported={onImported} onClose={onClose} />);
  return { api, onImported, onClose, user: userEvent.setup() };
}

describe("추천 파일 함께 가져오기", () => {
  it("retains good previews after one file fails and imports them atomically after the failed file is explicitly removed", async () => {
    const { api, user, onImported, onClose } = setup();
    api.preview.mockImplementation(async input => { if (input.name === "실패.xlsx") throw new Error("읽을 수 없는 파일"); return preview(input.name); });
    await user.upload(screen.getByLabelText("가져올 파일"), [file("첫째.xlsx"), file("실패.xlsx"), file("셋째.xlsx")]);
    expect(await screen.findByText("읽을 수 없는 파일")).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "셋째.xlsx 내용 확인" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "2건 가져오기" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "실패.xlsx 파일 제외" }));
    expect(screen.getByRole("checkbox", { name: "같은 책 합치기" })).toBeChecked();
    await user.click(screen.getByRole("button", { name: "2건 가져오기" }));
    await waitFor(() => expect(onClose).toHaveBeenCalledOnce());
    expect(api.importBatch).toHaveBeenCalledOnce();
    expect(api.importRows).not.toHaveBeenCalled();
    expect(api.importBatch.mock.calls[0].slice(0, 3)).toEqual(["list-1", [{ import_id: "첫째.xlsx", rows: [row("첫째.xlsx")] }, { import_id: "셋째.xlsx", rows: [row("셋째.xlsx")] }], true]);
    expect(onImported).toHaveBeenCalledWith(expect.stringContaining("2건"), "operation-1");
  });

  it("keeps each file's mapping and manual edits while switching files and applying a mapping", async () => {
    const { api, user } = setup();
    api.preview.mockImplementation(async input => preview(input.name, [row(input.name), row("제외할 책", 3)]));
    api.remapPreview.mockImplementation(async id => preview(id, [row("원본 재해석 제목"), row("제외할 책", 3)]));
    await user.upload(screen.getByLabelText("가져올 파일"), [file("첫째.xlsx"), file("둘째.xlsx")]);
    await user.click(await screen.findByRole("button", { name: "첫째.xlsx 내용 확인" }));
    fireEvent.change(screen.getByLabelText("1행 책 제목"), { target: { value: "직접 확인한 제목" } });
    await user.click(screen.getByRole("button", { name: "2행 제외" }));
    await user.click(screen.getByText("열 연결 확인 · 제목이나 가격이 다른 열에 있나요?"));
    await user.selectOptions(screen.getByRole("combobox", { name: "저자" }), "책 이름");
    await user.click(screen.getByRole("button", { name: "둘째.xlsx 내용 확인" }));
    expect(screen.getByLabelText("1행 책 제목")).toHaveValue("둘째.xlsx");
    expect(screen.getByRole("combobox", { name: "저자", hidden: true })).toHaveValue("지은이");
    await user.click(screen.getByRole("button", { name: "첫째.xlsx 내용 확인" }));
    expect(screen.getByLabelText("1행 책 제목")).toHaveValue("직접 확인한 제목");
    expect(screen.getByRole("combobox", { name: "저자", hidden: true })).toHaveValue("책 이름");
    await user.click(screen.getByRole("button", { name: "열 연결 적용" }));
    await waitFor(() => expect(api.remapPreview).toHaveBeenCalledWith("첫째.xlsx", expect.objectContaining({ author: "책 이름" })));
    await waitFor(() => expect(screen.getByRole("button", { name: "3건 가져오기" })).toBeEnabled());
    expect(screen.getByLabelText("1행 책 제목")).toHaveValue("직접 확인한 제목");
    expect(screen.queryByLabelText("2행 책 제목")).not.toBeInTheDocument();
  });

  it("renders at most 100 preview rows while retaining edits across pages and submitting all rows", async () => {
    const { api, user } = setup();
    api.preview.mockResolvedValue(preview("큰 목록.xlsx", Array.from({ length: 205 }, (_, index) => row(`책 ${index + 1}`, index + 2))));
    await user.upload(screen.getByLabelText("가져올 파일"), file("큰 목록.xlsx"));
    await screen.findByLabelText("100행 책 제목");
    expect(screen.queryByLabelText("101행 책 제목")).not.toBeInTheDocument();
    expect(screen.getAllByRole("row")).toHaveLength(101);
    fireEvent.change(screen.getByLabelText("1행 책 제목"), { target: { value: "첫 행 수정" } });
    await user.click(screen.getByRole("button", { name: "다음 미리보기 페이지" }));
    expect(screen.getByLabelText("101행 책 제목")).toHaveValue("책 101");
    fireEvent.change(screen.getByLabelText("101행 정가"), { target: { value: "15000" } });
    await user.click(screen.getByRole("button", { name: "이전 미리보기 페이지" }));
    expect(screen.getByLabelText("1행 책 제목")).toHaveValue("첫 행 수정");
    await user.click(screen.getByRole("button", { name: "205건 가져오기" }));
    await waitFor(() => expect(api.importBatch).toHaveBeenCalledOnce());
    expect(api.importBatch.mock.calls[0][1][0].rows).toHaveLength(205);
    expect(api.importBatch.mock.calls[0][1][0].rows[100].price).toBe(15000);
  });

  it("reuses the request ID after an uncertain failure but changes it when preview content changes", async () => {
    const { api, user } = setup();
    api.importBatch.mockRejectedValue(new Error("연결 끊김"));
    await user.upload(screen.getByLabelText("가져올 파일"), file("추천.xlsx"));
    await user.click(await screen.findByRole("button", { name: "1건 가져오기" }));
    await screen.findByRole("alert");
    await user.click(screen.getByRole("button", { name: "1건 가져오기" }));
    await waitFor(() => expect(api.importBatch).toHaveBeenCalledTimes(2));
    expect(api.importBatch.mock.calls[0][3]).toBeTruthy();
    expect(api.importBatch.mock.calls[1][3]).toBe(api.importBatch.mock.calls[0][3]);
    await waitFor(() => expect(screen.getByLabelText("1행 책 제목")).toBeEnabled());
    fireEvent.change(screen.getByLabelText("1행 책 제목"), { target: { value: "수정한 책" } });
    await user.click(screen.getByRole("button", { name: "1건 가져오기" }));
    await waitFor(() => expect(api.importBatch).toHaveBeenCalledTimes(3));
    expect(api.importBatch.mock.calls[2][3]).not.toBe(api.importBatch.mock.calls[0][3]);
  });

  it("does not import again when saving succeeded but refreshing the list failed", async () => {
    const { api, user, onImported, onClose } = setup();
    onImported.mockRejectedValueOnce(new Error("목록 새로고침 실패"));
    await user.upload(screen.getByLabelText("가져올 파일"), file("추천.xlsx"));
    await user.click(await screen.findByRole("button", { name: "1건 가져오기" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("저장되었습니다");
    expect(screen.getByLabelText("1행 책 제목")).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "저장된 결과 다시 열기" }));
    await waitFor(() => expect(onClose).toHaveBeenCalledOnce());
    expect(api.importBatch).toHaveBeenCalledOnce();
    expect(onImported).toHaveBeenCalledTimes(2);
  });

  it("stops before the next file and resumes the unprocessed queue without reading successful files again", async () => {
    const { api, user } = setup();
    let finish!: (result: ImportPreview) => void;
    api.preview.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    await user.upload(screen.getByLabelText("가져올 파일"), [file("첫째.xlsx"), file("둘째.xlsx")]);
    await user.click(screen.getByRole("button", { name: "파일 읽기 중지" }));
    await act(async () => { finish(preview("첫째.xlsx")); });
    expect(api.preview).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "1건 가져오기" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "남은 파일 읽기" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "2건 가져오기" })).toBeEnabled());
    expect(api.preview).toHaveBeenCalledTimes(2);
    expect(api.preview.mock.calls[1][0].name).toBe("둘째.xlsx");
  });

  it("restricts holdings to one complete file and uses the replacement operation", async () => {
    const { api, user } = setup("holdings");
    expect(screen.getByLabelText("가져올 파일")).not.toHaveAttribute("multiple");
    await user.upload(screen.getByLabelText("가져올 파일"), file("소장.xlsx"));
    await user.click(await screen.findByRole("button", { name: "1건 가져오기" }));
    await waitFor(() => expect(api.importRows).toHaveBeenCalledWith("list-1", "소장.xlsx", [row("소장.xlsx")], "holdings"));
    expect(api.importBatch).not.toHaveBeenCalled();
  });
});
