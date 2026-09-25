import { expect, it } from "vitest";
import { applyPreview, updatePreviewRow, type ImportFileState } from "../src/simple/importPreviewState";
import { emptyBook, type ImportPreview } from "../src/simple/types";

it("preserves only fields actually edited in the detail form when a mapping is reapplied", () => {
  const original = { ...emptyBook(), title: "원본 제목", author: "잘못 연결된 저자", source_row: 7, source_sheet: "추천" };
  const state: ImportFileState = { id: "file-1", file: new File(["sheet"], "추천.xlsx"), status: "queued", error: "", preview: null, mapping: {}, mappingChanged: false, rowKeys: [], edits: {}, removed: new Set() };
  const parsed: ImportPreview = { import_id: "import-1", filename: "추천.xlsx", rows: [original], warnings: [] };
  const edited = updatePreviewRow(applyPreview(state, parsed), 0, { ...original, title: "사서가 확인한 제목" });
  const remapped = applyPreview(edited, { ...parsed, rows: [{ ...original, title: "새로 읽은 제목", author: "정확한 저자", price: 14000 }] });
  expect(remapped.preview?.rows[0]).toMatchObject({ title: "사서가 확인한 제목", author: "정확한 저자", price: 14000, source_row: 7 });
});
