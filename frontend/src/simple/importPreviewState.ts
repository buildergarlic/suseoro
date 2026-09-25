import type { ImportPreview, PreviewRow } from "./types";

export interface ImportFileState {
  id: string; file: File; status: "queued" | "reading" | "ready" | "error";
  error: string; preview: ImportPreview | null; mapping: Record<string, string>; mappingChanged: boolean;
  rowKeys: string[]; edits: Record<string, Partial<PreviewRow>>; removed: Set<string>;
}
const mappingFrom = (preview: ImportPreview): Record<string, string> => Object.fromEntries(Object.entries(preview.mapping ?? {}).filter((entry): entry is [string, string] => typeof entry[1] === "string"));

// Source positions survive column remapping; occurrence suffixes keep repeated raw rows distinct.
function sourceKeys(rows: PreviewRow[]): string[] {
  const counts = new Map<string, number>();
  return rows.map((row, index) => {
    const origin = row.provenance && typeof row.provenance === "object" ? row.provenance as Record<string, unknown> : {};
    const sourceRow = row.source_row ?? origin.row;
    const identity = sourceRow != null ? JSON.stringify([row.source_sheet ?? origin.sheet, row.source_page ?? origin.page, sourceRow]) : JSON.stringify(row.raw_values ?? row.raw ?? row.raw_text ?? ["position", index]);
    const count = counts.get(identity) ?? 0; counts.set(identity, count + 1);
    return `${identity}:${count}`;
  });
}
export function applyPreview(item: ImportFileState, preview: ImportPreview): ImportFileState {
  const keys = sourceKeys(preview.rows);
  const included = preview.rows.map((row, index) => ({ row, key: keys[index] })).filter(({ key }) => !item.removed.has(key));
  return { ...item, status: "ready", error: "", mapping: mappingFrom(preview), mappingChanged: false, preview: { ...preview, rows: included.map(({ row, key }) => ({ ...row, ...item.edits[key] })) }, rowKeys: included.map(({ key }) => key) };
}
export function updatePreviewRow(item: ImportFileState, index: number, fields: Partial<PreviewRow>): ImportFileState {
  if (!item.preview) return item;
  const key = item.rowKeys[index];
  const current = item.preview.rows[index];
  const changes = Object.fromEntries(Object.entries(fields).filter(([field, value]) => current[field as keyof PreviewRow] !== value));
  return { ...item, edits: { ...item.edits, [key]: { ...item.edits[key], ...changes } }, preview: { ...item.preview, rows: item.preview.rows.map((row, rowIndex) => rowIndex === index ? { ...row, ...changes } : row) } };
}
