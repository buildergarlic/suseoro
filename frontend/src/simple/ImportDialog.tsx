import { useRef, useState } from "react";
import type { LibraryApi } from "./api";
import { BookFieldsForm } from "./BookDialog";
import { InlineError, Modal } from "./Modal";
import { errorMessage, money, type ImportPreview, type PreviewRow } from "./types";

const columns = [{ key: "title", label: "책 제목" }, { key: "author", label: "저자" }, { key: "publisher", label: "출판사" }, { key: "isbn", label: "ISBN" }, { key: "price", label: "정가" }, { key: "quantity", label: "수량" }, { key: "source", label: "추천 출처" }, { key: "category", label: "분류" }, { key: "requester", label: "요청자" }, { key: "audience", label: "대상" }, { key: "priority", label: "우선순위" }, { key: "note", label: "메모" }, { key: "published_date", label: "발행일" }];
export function ImportDialog({ api, listId, onClose, onImported }: { api: LibraryApi; listId: string; onClose: () => void; onImported: (message: string) => Promise<void> }) {
  const [preview, setPreview] = useState<ImportPreview | null>(null), [kind, setKind] = useState<"recommendations" | "holdings">("recommendations");
  const [error, setError] = useState(""), [busy, setBusy] = useState(false), [editing, setEditing] = useState<number | null>(null);
  const [mapping, setMapping] = useState<Record<string, string>>({});
  const [lookupProgress, setLookupProgress] = useState(""), [lookupNotice, setLookupNotice] = useState("");
  const cancelLookup = useRef(false);
  async function fillMissing() {
    if (!preview) return;
    const candidates = preview.rows.map((row, index) => ({ row, index })).filter(({ row }) => row.isbn && (!row.title || !row.author || !row.publisher || row.price === null)).slice(0, 50);
    if (!candidates.length) { setLookupNotice("ISBN이 있고 빈 정보가 있는 행이 없습니다."); return; }
    setBusy(true); setError(""); setLookupNotice(""); cancelLookup.current = false; let filled = 0, failed = 0;
    for (const [position, candidate] of candidates.entries()) {
      if (cancelLookup.current) break;
      setLookupProgress(`${position + 1} / ${candidates.length}권 조회 중`);
      try {
        const result = await api.lookup(candidate.row.isbn);
        if (!result.found || !result.book) { failed += 1; continue; }
        const found = result.book; filled += 1;
        setPreview(previous => previous ? { ...previous, rows: previous.rows.map((row, index) => index === candidate.index ? { ...row, title: row.title || found.title || "", author: row.author || found.author || "", publisher: row.publisher || found.publisher || "", price: row.price ?? found.price ?? null, published_date: row.published_date || found.published_date || "", link: row.link || found.link || "", source: row.source || found.source || "", warnings: [...new Set([...row.warnings, ...(found.warnings ?? []), ...result.warnings, ...(found.source ? [`ISBN 조회 출처: ${found.source}`] : [])])], needs_review: true } : row) } : previous);
      } catch { failed += 1; }
    }
    setLookupNotice(`${filled}건의 빈 정보를 채웠습니다.${failed ? ` ${failed}건은 조회하지 못했습니다.` : ""}${cancelLookup.current ? " 조회를 중지했습니다." : ""} 채운 내용과 가격을 확인해 주세요.`); setLookupProgress(""); setBusy(false);
  }
  async function upload(file: File | undefined) {
    if (!file) return; setError(""); setBusy(true);
    try { const result = await api.preview(file); setPreview(result); setEditing(null); setMapping(Object.fromEntries(Object.entries(result.mapping ?? {}).filter((entry): entry is [string, string] => typeof entry[1] === "string"))); }
    catch (caught) { setError(errorMessage(caught)); } finally { setBusy(false); }
  }
  async function remap() {
    if (!preview) return; setError(""); setBusy(true);
    try { const result = await api.remapPreview(preview.import_id, mapping); setPreview(result); setEditing(null); }
    catch (caught) { setError(errorMessage(caught)); } finally { setBusy(false); }
  }
  function editRow(index: number, fields: Partial<PreviewRow>) { setPreview(previous => previous ? { ...previous, rows: previous.rows.map((row, rowIndex) => rowIndex === index ? { ...row, ...fields } : row) } : previous); }
  async function save() {
    if (!preview) return; setBusy(true); setError("");
    try { const result = await api.importRows(listId, preview.import_id, preview.rows, kind); await onImported(`${kind === "holdings" ? "소장목록" : "추천도서"} ${result.added}건을 가져왔습니다.${result.warnings.length ? ` ${result.warnings.join(" · ")}` : ""}`); onClose(); }
    catch (caught) { setError(errorMessage(caught)); } finally { setBusy(false); }
  }
  const activeRow = editing === null ? null : preview?.rows[editing];
  return <Modal title="파일에서 책 가져오기" description="여러 기관의 추천목록과 학교 소장목록을 한곳에 모으세요." onClose={onClose} wide busy={busy}>
    <div className="modal-body">
      <div className="import-kind" role="group" aria-label="가져올 자료 종류"><label className={kind === "recommendations" ? "active" : ""}><input type="radio" name="import-kind" value="recommendations" checked={kind === "recommendations"} onChange={() => setKind("recommendations")} disabled={busy} /><span><strong>추천도서 목록</strong><small>구입 후보로 목록에 추가</small></span></label><label className={kind === "holdings" ? "active" : ""}><input type="radio" name="import-kind" value="holdings" checked={kind === "holdings"} onChange={() => setKind("holdings")} disabled={busy} /><span><strong>학교 소장목록</strong><small>이미 소장한 책 표시</small></span></label></div>
      <label className="upload-zone"><span className="upload-symbol" aria-hidden="true">↥</span><strong>{busy ? "파일을 읽고 있습니다…" : preview ? preview.filename : "가져올 파일 선택"}</strong><span>Excel, CSV, PDF, HWP, HWPX, DOCX, TXT</span><input type="file" aria-label="가져올 파일" accept=".xlsx,.xls,.csv,.pdf,.hwp,.hwpx,.docx,.txt,.tsv" onChange={e => { void upload(e.target.files?.[0]); e.target.value = ""; }} disabled={busy} /></label>
      {preview && <>
        <div className="lookup-inline"><button type="button" className="button secondary small" disabled={busy} onClick={() => { void fillMissing(); }}>ISBN으로 빈 정보 채우기</button><small>한 번에 최대 50권 · 입력한 정보 유지</small>{lookupProgress && <><span role="status">{lookupProgress}</span><button className="text-button" onClick={() => { cancelLookup.current = true; }}>조회 중지</button></>}</div>{lookupNotice && <p className="inline-success" role="status">{lookupNotice}</p>}
        {!!preview.warnings.length && <div className="soft-note"><strong>확인할 점</strong><ul>{preview.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul></div>}
        {!!preview.headers?.length && <details className="mapping-details"><summary>열 연결 확인 · 제목이나 가격이 다른 열에 있나요?</summary><p className="field-help">파일의 열을 연결하고 적용하면 미리보기를 다시 읽습니다. 수정한 내용은 원본에서 다시 채워집니다.</p><div className="mapping-grid">{columns.map(column => <label key={column.key}>{column.label}<select value={mapping[column.key] ?? ""} onChange={e => setMapping(previous => ({ ...previous, [column.key]: e.target.value }))} disabled={busy}><option value="">연결 안 함</option>{preview.headers?.map(header => <option key={header} value={header}>{header}</option>)}</select></label>)}</div><button type="button" className="button secondary" onClick={() => { void remap(); }} disabled={busy}>열 연결 적용</button></details>}
        {activeRow && editing !== null ? <div className="preview-editor"><div className="section-title"><h3>{editing + 1}번째 책 수정</h3><button className="button secondary small" onClick={() => setEditing(null)}>미리보기로 돌아가기</button></div><BookFieldsForm value={activeRow} onChange={value => editRow(editing, value)} />{activeRow.raw_text && <details><summary>원문 보기</summary><pre>{activeRow.raw_text}</pre></details>}</div> : <>
          <div className="section-title"><h3>가져오기 미리보기 <span>{preview.rows.length}건</span></h3><small>책 제목·정가를 바로 고칠 수 있어요.</small></div>
          <div className="preview-table-wrap"><table className="preview-table"><thead><tr><th scope="col">책 제목 / 저자</th><th scope="col">ISBN</th><th scope="col">정가 (원)</th><th scope="col">수량</th><th scope="col">확인</th><th scope="col"><span className="sr-only">행 관리</span></th></tr></thead><tbody>{preview.rows.map((row, index) => <tr key={index}><td><input aria-label={`${index + 1}행 책 제목`} value={row.title} onChange={e => editRow(index, { title: e.target.value })} placeholder="서명 미확인" /><input className="secondary-input" aria-label={`${index + 1}행 저자`} value={row.author} onChange={e => editRow(index, { author: e.target.value })} placeholder="저자" /></td><td><input aria-label={`${index + 1}행 ISBN`} value={row.isbn} onChange={e => editRow(index, { isbn: e.target.value })} /></td><td><input type="number" min="0" step="1" aria-label={`${index + 1}행 정가`} value={row.price ?? ""} onChange={e => editRow(index, { price: e.target.value === "" ? null : Number(e.target.value) })} placeholder="미확인" /></td><td><input type="number" min="1" step="1" aria-label={`${index + 1}행 수량`} value={row.quantity} onChange={e => editRow(index, { quantity: Number(e.target.value) })} /></td><td>{row.needs_review ? <span className="badge amber">확인 필요</span> : <span className="badge">준비됨</span>}<small>{row.price === null ? "가격 미확인" : money(row.price * row.quantity)}</small></td><td><button className="text-button" onClick={() => setEditing(index)} aria-label={`${index + 1}행 상세 수정`}>상세</button><button className="icon-button" aria-label={`${index + 1}행 제외`} onClick={() => setPreview(previous => previous ? { ...previous, rows: previous.rows.filter((_, rowIndex) => index !== rowIndex) } : previous)}>×</button></td></tr>)}</tbody></table>{!preview.rows.length && <p className="empty-hint">가져올 행이 없습니다. 다른 파일을 선택해 주세요.</p>}</div>
        </>}
      </>}
      <InlineError message={error} />
    </div><footer className="modal-footer"><p className="field-help">{kind === "holdings" ? `기존 소장목록을 위의 ${preview?.rows.length ?? 0}건으로 교체합니다. 학교의 전체 소장목록인지 확인해 주세요.` : "가격 미확인·확인 필요 자료도 후보로 보관할 수 있습니다."}</p><span className="spacer" /><button className="button secondary" onClick={onClose} disabled={busy}>취소</button><button className="button primary" disabled={busy || !preview?.rows.length} onClick={() => { void save(); }}>{busy ? "처리 중…" : `${preview?.rows.length ?? 0}건 가져오기`}</button></footer>
  </Modal>;
}
