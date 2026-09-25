import { useMemo, useRef, useState } from "react";
import type { LibraryApi } from "./api";
import { BookFieldsForm } from "./BookDialog";
import { InlineError, Modal } from "./Modal";
import { applyPreview, updatePreviewRow, type ImportFileState } from "./importPreviewState";
import { errorMessage, money, type PreviewRow } from "./types";
import "./import-batch.css";

const pageSize = 100;
const requiresReview = (row: PreviewRow) => row.needs_review || row.price === null || !row.title.trim();
function sourceLocation(row: PreviewRow) {
  const origin = row.provenance && typeof row.provenance === "object" ? row.provenance as Record<string, unknown> : {};
  const sheet = row.source_sheet || (typeof origin.sheet === "string" ? origin.sheet : "");
  const number = row.source_row || (typeof origin.row === "number" ? origin.row : null);
  const page = row.source_page || (typeof origin.page === "number" ? origin.page : null);
  return [sheet, number ? `원본 ${number}행` : "", page ? `${page}쪽` : ""].filter(Boolean).join(" · ");
}
const columns = [{ key: "title", label: "책 제목" }, { key: "author", label: "저자" }, { key: "publisher", label: "출판사" }, { key: "isbn", label: "ISBN" }, { key: "price", label: "정가" }, { key: "quantity", label: "수량" }, { key: "source", label: "추천 출처" }, { key: "category", label: "분류" }, { key: "requester", label: "요청자" }, { key: "audience", label: "대상" }, { key: "priority", label: "우선순위" }, { key: "note", label: "메모" }, { key: "published_date", label: "발행일" }, { key: "link", label: "참고 링크" }];
type SavedImport = { message: string; operationId?: string };

export function ImportDialog({ api, listId, initialKind = "recommendations", onClose, onImported }: { api: LibraryApi; listId: string; initialKind?: "recommendations" | "holdings"; onClose: () => void; onImported: (message: string, operationId?: string) => Promise<void> }) {
  const [files, setFiles] = useState<ImportFileState[]>([]), [activeId, setActiveId] = useState("");
  const [kind, setKind] = useState<"recommendations" | "holdings">(initialKind);
  const [error, setError] = useState(""), [busy, setBusy] = useState(false), [editing, setEditing] = useState<number | null>(null);
  const [lookupProgress, setLookupProgress] = useState(""), [lookupNotice, setLookupNotice] = useState("");
  const [readProgress, setReadProgress] = useState(""), [readNotice, setReadNotice] = useState("");
  const [reviewOnly, setReviewOnly] = useState(false), [page, setPage] = useState(0), [diagnosticPage, setDiagnosticPage] = useState(0);
  const [editedRows, setEditedRows] = useState<Set<number>>(() => new Set());
  const [deduplicate, setDeduplicate] = useState(true), [saved, setSaved] = useState<SavedImport | null>(null);
  const cancelLookup = useRef(false), cancelReading = useRef(false), inFlight = useRef(false);
  const requestId = useRef("");
  const active = files.find(item => item.id === activeId), preview = active?.preview ?? null;
  const locked = busy || saved !== null;
  const totalRows = files.reduce((sum, item) => sum + (item.preview?.rows.length ?? 0), 0);
  const readyFiles = files.filter(item => item.status === "ready");
  const pendingFiles = files.filter(item => item.status === "queued");
  const failedFiles = files.filter(item => item.status === "error");
  const unappliedMapping = files.some(item => item.mappingChanged);

  function changeFile(id: string) {
    setActiveId(id); setEditing(null); setPage(0); setDiagnosticPage(0); setReviewOnly(false); setEditedRows(new Set()); setLookupNotice("");
  }
  function updateFile(id: string, update: (item: ImportFileState) => ImportFileState) {
    setFiles(previous => previous.map(item => item.id === id ? update(item) : item));
  }
  function begin() { if (inFlight.current || saved) return false; inFlight.current = true; setBusy(true); setError(""); return true; }
  function end() { inFlight.current = false; setBusy(false); }
  function invalidate() { requestId.current = ""; }

  async function readFiles(queue: ImportFileState[]) {
    if (!queue.length || !begin()) return;
    cancelReading.current = false; setReadNotice(""); let processed = 0;
    for (const [position, item] of queue.entries()) {
      if (cancelReading.current) break;
      setReadProgress(`${position + 1} / ${queue.length}개 파일 읽는 중 · ${item.file.name}`);
      updateFile(item.id, current => ({ ...current, status: "reading", error: "" }));
      try {
        const result = await api.preview(item.file);
        updateFile(item.id, current => applyPreview(current, result));
        setActiveId(current => current || item.id);
      } catch (caught) {
        updateFile(item.id, current => ({ ...current, status: "error", error: errorMessage(caught) }));
      }
      processed += 1;
    }
    setReadNotice(cancelReading.current ? `${processed}개 파일까지 읽고 중지했습니다. 남은 파일은 이어서 읽을 수 있습니다.` : `${processed}개 파일 읽기를 마쳤습니다.`);
    setReadProgress(""); end();
  }
  async function upload(selected: File[]) {
    if (!selected.length || locked || inFlight.current) return;
    if (kind === "holdings" && (selected.length > 1 || files.length > 0)) { setError("학교 소장목록은 전체 목록 파일 1개만 가져옵니다. 기존 미리보기를 제외한 후 파일을 선택해 주세요."); return; }
    invalidate();
    const additions: ImportFileState[] = selected.map(file => ({ id: crypto.randomUUID(), file, status: "queued", error: "", preview: null, mapping: {}, mappingChanged: false, rowKeys: [], edits: {}, removed: new Set() }));
    setFiles(previous => [...previous, ...additions]);
    await readFiles([...pendingFiles, ...additions]);
  }
  function removeFile(id: string) {
    if (locked) return;
    invalidate(); setFiles(previous => previous.filter(item => item.id !== id));
    if (activeId === id) changeFile(files.find(item => item.id !== id && item.preview)?.id ?? "");
  }
  function changeKind(next: "recommendations" | "holdings") {
    if (next === "holdings" && files.length > 1) { setError("학교 소장목록은 전체 목록 파일 1개만 사용할 수 있습니다. 나머지 파일을 제외해 주세요."); return; }
    invalidate(); setKind(next); setError("");
  }
  async function remap() {
    if (!active || !preview || !begin()) return;
    try {
      const result = await api.remapPreview(preview.import_id, active.mapping);
      invalidate(); updateFile(active.id, item => applyPreview(item, result));
      setEditing(null); setPage(0); setDiagnosticPage(0); setReviewOnly(false); setEditedRows(new Set());
    } catch (caught) { setError(errorMessage(caught)); } finally { end(); }
  }
  function editRow(index: number, fields: Partial<PreviewRow>) {
    if (!active || locked) return;
    invalidate(); if (reviewOnly) setEditedRows(previous => new Set(previous).add(index));
    updateFile(active.id, item => updatePreviewRow(item, index, fields));
  }
  function removeRow(index: number) {
    if (!active || locked) return;
    invalidate();
    updateFile(active.id, item => ({ ...item, removed: new Set(item.removed).add(item.rowKeys[index]), rowKeys: item.rowKeys.filter((_, rowIndex) => rowIndex !== index), preview: item.preview ? { ...item.preview, rows: item.preview.rows.filter((_, rowIndex) => rowIndex !== index) } : null }));
    setEditedRows(previous => new Set([...previous].filter(rowIndex => rowIndex !== index).map(rowIndex => rowIndex > index ? rowIndex - 1 : rowIndex)));
  }
  async function fillMissing() {
    if (!active || !preview) return;
    const candidates = preview.rows.map((row, index) => ({ row, index })).filter(({ row }) => row.isbn && (!row.title || !row.author || !row.publisher || row.price === null)).slice(0, 50);
    if (!candidates.length) { setLookupNotice("ISBN이 있고 빈 정보가 있는 행이 없습니다."); return; }
    if (!begin()) return;
    setLookupNotice(""); cancelLookup.current = false; let filled = 0, failed = 0;
    for (const [position, candidate] of candidates.entries()) {
      if (cancelLookup.current) break;
      setLookupProgress(`${position + 1} / ${candidates.length}권 조회 중`);
      try {
        const result = await api.lookup(candidate.row.isbn);
        if (!result.found || !result.book) { failed += 1; continue; }
        const found = result.book; filled += 1; invalidate();
        updateFile(active.id, item => {
          const row = item.preview?.rows[candidate.index];
          return row ? updatePreviewRow(item, candidate.index, { title: row.title || found.title || "", author: row.author || found.author || "", publisher: row.publisher || found.publisher || "", price: row.price ?? found.price ?? null, category: row.category || found.category || "", published_date: row.published_date || found.published_date || "", link: row.link || found.link || "", source: row.source || found.source || "", warnings: [...new Set([...row.warnings, ...(found.warnings ?? []), ...result.warnings, ...(found.source ? [`ISBN 조회 출처: ${found.source}`] : [])])], needs_review: true }) : item;
        });
      } catch { failed += 1; }
    }
    setLookupNotice(`${filled}건의 빈 정보를 채웠습니다.${failed ? ` ${failed}건은 조회하지 못했습니다.` : ""}${cancelLookup.current ? " 조회를 중지했습니다." : ""} 채운 내용과 가격을 확인해 주세요.`); setLookupProgress(""); end();
  }
  async function save() {
    if (inFlight.current || !totalRows || (!saved && (pendingFiles.length || failedFiles.length || unappliedMapping))) return;
    inFlight.current = true; setBusy(true); setError("");
    let completed = saved;
    try {
      if (!completed) {
        if (kind === "holdings") {
          const only = readyFiles[0].preview!;
          const result = await api.importRows(listId, only.import_id, only.rows, kind);
          completed = { message: `소장목록 ${result.added}건을 가져왔습니다.${result.warnings.length ? ` ${result.warnings.join(" · ")}` : ""}` };
        } else {
          requestId.current ||= crypto.randomUUID();
          const result = await api.importBatch(listId, readyFiles.filter(item => item.preview?.rows.length).map(item => ({ import_id: item.preview!.import_id, rows: item.preview!.rows })), deduplicate, requestId.current);
          completed = { message: `추천도서 ${result.input_count}건에서 ${result.added}건 추가, ${result.merged}건 합치기 완료. 추천 출처를 함께 보관했습니다.${result.warnings.length ? ` ${result.warnings.join(" · ")}` : ""}`, operationId: result.operation_id };
        }
        setSaved(completed);
      }
      await onImported(completed.message, completed.operationId); onClose();
    } catch (caught) { setError(completed ? `목록에 저장되었습니다. 화면을 새로 불러오지 못했습니다. 저장된 결과 다시 열기를 눌러 주세요. ${errorMessage(caught)}` : errorMessage(caught)); }
    finally { end(); }
  }

  const reviewCount = useMemo(() => preview?.rows.filter(requiresReview).length ?? 0, [preview]);
  const visibleRows = useMemo(() => preview?.rows.map((row, index) => ({ row, index })).filter(({ row, index }) => !reviewOnly || requiresReview(row) || editedRows.has(index)) ?? [], [preview, reviewOnly, editedRows]);
  const pageCount = Math.max(1, Math.ceil(visibleRows.length / pageSize)), currentPage = Math.min(page, pageCount - 1);
  const shownRows = visibleRows.slice(currentPage * pageSize, (currentPage + 1) * pageSize);
  const diagnostics = preview?.diagnostics?.filter(item => item.kind !== "header") ?? [];
  const diagnosticPages = Math.max(1, Math.ceil(diagnostics.length / pageSize)), currentDiagnosticPage = Math.min(diagnosticPage, diagnosticPages - 1);
  const activeRow = editing === null ? null : preview?.rows[editing];
  return <Modal title="파일에서 책 가져오기" description="여러 기관의 추천목록과 학교 소장목록을 한곳에 모으세요." onClose={onClose} wide busy={busy}>
    <div className="modal-body">
      <ol className="import-steps" aria-label="가져오기 단계"><li className={!preview ? "active" : ""}>1. 파일 선택</li><li className={preview ? "active" : ""}>2. 내용 확인</li><li>3. 목록에 추가</li></ol>
      <div className="import-kind" role="group" aria-label="가져올 자료 종류"><label className={kind === "recommendations" ? "active" : ""}><input type="radio" name="import-kind" value="recommendations" checked={kind === "recommendations"} onChange={() => changeKind("recommendations")} disabled={locked} /><span><strong>추천도서 목록</strong><small>여러 파일을 함께 추가</small></span></label><label className={kind === "holdings" ? "active" : ""}><input type="radio" name="import-kind" value="holdings" checked={kind === "holdings"} onChange={() => changeKind("holdings")} disabled={locked} /><span><strong>학교 소장목록</strong><small>전체 목록 파일 1개로 교체</small></span></label></div>
      <label className={`upload-zone${files.length ? " has-file" : ""}`}><span className="upload-symbol" aria-hidden="true">↥</span><strong>{readProgress ? "파일을 읽고 있습니다…" : files.length ? "파일 더 선택하기" : "가져올 파일 선택"}</strong><span>Excel, CSV, PDF, HWP, HWPX, DOCX, TXT</span><input type="file" multiple={kind === "recommendations"} aria-label="가져올 파일" accept=".xlsx,.xls,.csv,.pdf,.hwp,.hwpx,.docx,.txt,.tsv" onChange={e => { void upload(Array.from(e.target.files ?? [])); e.target.value = ""; }} disabled={locked || (kind === "holdings" && files.length > 0)} /></label>
      {kind === "holdings" && <p className="field-help">학교 소장목록은 파일 1개만 받습니다. 학교의 전체 목록을 선택해 주세요.</p>}
      {readProgress && <div className="import-queue-progress"><span role="status">{readProgress}</span><button type="button" className="button secondary small" onClick={() => { cancelReading.current = true; setReadNotice("현재 파일을 마치면 읽기를 중지합니다."); }}>파일 읽기 중지</button></div>}
      {readNotice && <p className="field-help" role="status">{readNotice}</p>}
      {!!files.length && <ul className="import-file-list" aria-label="가져올 파일 목록">{files.map(item => <li key={item.id} className={item.id === activeId ? "active" : ""}>
        <button type="button" className="import-file-open" aria-label={`${item.file.name} 내용 확인`} aria-pressed={item.id === activeId} disabled={busy || !item.preview} onClick={() => changeFile(item.id)}><strong>{item.file.name}</strong><span>{item.status === "ready" ? `${item.preview?.rows.length ?? 0}건${item.mappingChanged ? " · 열 연결 미적용" : ""}` : item.status === "reading" ? "읽는 중" : item.status === "error" ? "읽기 실패" : "읽기 대기"}</span></button>
        {item.status === "error" && <><p className="import-file-error">{item.error}</p><button type="button" className="button secondary small" disabled={locked} aria-label={`${item.file.name} 다시 읽기`} onClick={() => { void readFiles([item]); }}>다시 읽기</button></>}
        <button type="button" className="icon-button" aria-label={`${item.file.name} 파일 제외`} disabled={locked} onClick={() => removeFile(item.id)}>×</button>
      </li>)}</ul>}
      {!!pendingFiles.length && !readProgress && <button type="button" className="button secondary" disabled={locked} onClick={() => { void readFiles(pendingFiles); }}>남은 파일 읽기</button>}
      {!!failedFiles.length && <p className="soft-note">실패한 파일을 다시 읽거나 제외한 후 가져오세요. 이미 읽은 파일과 수정 내용은 유지됩니다.</p>}
      {!!files.length && kind === "recommendations" && <div className="import-batch-options"><label className="check-label"><input type="checkbox" checked={deduplicate} disabled={locked} onChange={event => { invalidate(); setDeduplicate(event.target.checked); }} />같은 책 합치기</label><p className="field-help">전체 {readyFiles.length}개 파일 · {totalRows}건을 함께 가져옵니다. 같은 책을 합쳐도 추천기관·파일·원본 행을 보관합니다. 판·권차가 다른 책은 따로 남깁니다.</p></div>}
      {preview && active && <>
        <div className="import-summary" role="status"><strong>{preview.filename} · 도서 {preview.rows.length}건 인식</strong><span>확인 필요 {reviewCount}건</span><span>원본 파일은 변경하지 않습니다.</span></div>
        {!!diagnostics.length && <details className="mapping-details"><summary>안내문·반복 머리글·합계 {diagnostics.length}행 별도 보관</summary><p className="field-help">도서 행에서 제외한 원문입니다. 필요한 내용이 빠졌는지 확인하세요.</p>{diagnostics.slice(currentDiagnosticPage * pageSize, (currentDiagnosticPage + 1) * pageSize).map((item, index) => <div key={index} className="source-location"><strong>{[item.sheet, item.row ? `원본 ${item.row}행` : "", item.page ? `${item.page}쪽` : ""].filter(Boolean).join(" · ")}</strong><p>{item.message}</p><pre>{item.raw_text}</pre></div>)}{diagnosticPages > 1 && <nav className="import-pagination" aria-label="별도 보관 행 페이지"><button type="button" className="button secondary small" disabled={busy || currentDiagnosticPage === 0} onClick={() => setDiagnosticPage(currentDiagnosticPage - 1)}>이전 원문</button><span>{currentDiagnosticPage + 1} / {diagnosticPages}페이지</span><button type="button" className="button secondary small" disabled={busy || currentDiagnosticPage + 1 === diagnosticPages} onClick={() => setDiagnosticPage(currentDiagnosticPage + 1)}>다음 원문</button></nav>}</details>}
        <div className="lookup-inline"><button type="button" className="button secondary small" disabled={locked} onClick={() => { void fillMissing(); }}>ISBN으로 빈 정보 채우기</button><small>현재 파일에서 한 번에 최대 50권 · 입력한 정보 유지</small>{lookupProgress && <><span role="status">{lookupProgress}</span><button type="button" className="text-button" onClick={() => { cancelLookup.current = true; }}>조회 중지</button></>}</div>{lookupNotice && <p className="inline-success" role="status">{lookupNotice}</p>}
        {!!preview.warnings.length && <div className="soft-note"><strong>확인할 점</strong><ul>{preview.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul></div>}
        {!!preview.headers?.length && <details className="mapping-details"><summary>열 연결 확인 · 제목이나 가격이 다른 열에 있나요?</summary><p className="field-help">현재 파일의 열을 연결하고 적용하면 미리보기를 다시 읽습니다. 직접 수정한 값과 제외한 행은 유지합니다.</p><div className="mapping-grid">{columns.map(column => <label key={column.key}>{column.label}<select value={active.mapping[column.key] ?? ""} onChange={e => { const value = e.target.value; invalidate(); updateFile(active.id, item => ({ ...item, mapping: { ...item.mapping, [column.key]: value }, mappingChanged: true })); }} disabled={locked}><option value="">연결 안 함</option>{preview.headers?.map(header => <option key={header} value={header}>{header}</option>)}</select></label>)}</div><button type="button" className="button secondary" onClick={() => { void remap(); }} disabled={locked}>열 연결 적용</button></details>}
        {activeRow && editing !== null ? <div className="preview-editor"><div className="section-title"><h3>{editing + 1}번째 책 수정</h3><button type="button" className="button secondary small" disabled={busy} onClick={() => setEditing(null)}>미리보기로 돌아가기</button></div><p className="source-location">{preview.filename} · {sourceLocation(activeRow)}</p><BookFieldsForm disabled={locked} value={activeRow} onChange={value => editRow(editing, value)} />{activeRow.raw_text && <details><summary>원문 보기</summary><pre>{activeRow.raw_text}</pre></details>}</div> : <>
          <div className="section-title"><h3>가져오기 미리보기 <span>{preview.rows.length}건</span></h3><label className="check-label"><input type="checkbox" checked={reviewOnly} onChange={event => { setReviewOnly(event.target.checked); setEditedRows(new Set()); setPage(0); }} disabled={busy} />확인 필요한 행만 보기 ({reviewCount})</label></div><p className="field-help">제목·정가·수량을 바로 수정할 수 있습니다. 필터와 페이지에 관계없이 모든 파일에 남아 있는 {totalRows}건 전체를 가져옵니다.</p>
          <div className="preview-table-wrap"><table className="preview-table"><thead><tr><th scope="col">책 제목 / 저자</th><th scope="col">ISBN</th><th scope="col">정가 (원)</th><th scope="col">수량</th><th scope="col">확인</th><th scope="col"><span className="sr-only">행 관리</span></th></tr></thead><tbody>{shownRows.map(({ row, index }) => <tr key={active.rowKeys[index]}><td><input disabled={locked} aria-label={`${index + 1}행 책 제목`} value={row.title} onChange={e => editRow(index, { title: e.target.value })} placeholder="서명 미확인" /><input className="secondary-input" aria-label={`${index + 1}행 저자`} value={row.author} onChange={e => editRow(index, { author: e.target.value })} placeholder="저자" disabled={locked} /><small className="source-location">{sourceLocation(row)}</small></td><td><input disabled={locked} aria-label={`${index + 1}행 ISBN`} value={row.isbn} onChange={e => editRow(index, { isbn: e.target.value })} /></td><td><input disabled={locked} type="number" min="0" step="1" aria-label={`${index + 1}행 정가`} value={row.price ?? ""} onChange={e => editRow(index, { price: e.target.value === "" ? null : Number(e.target.value) })} placeholder="미확인" /></td><td><input disabled={locked} type="number" min="1" step="1" aria-label={`${index + 1}행 수량`} value={row.quantity} onChange={e => editRow(index, { quantity: Number(e.target.value) })} /></td><td>{requiresReview(row) ? <span className="badge amber">확인 필요</span> : <span className="badge">준비됨</span>}<small>{row.price === null ? "가격 미확인" : money(row.price * row.quantity)}</small>{!!row.warnings.length && <details className="row-warnings"><summary>확인 사유 {row.warnings.length}개</summary><ul>{row.warnings.map((warning, warningIndex) => <li key={warningIndex}>{warning}</li>)}</ul></details>}</td><td><button type="button" disabled={busy} className="text-button" onClick={() => setEditing(index)} aria-label={`${index + 1}행 상세 수정`}>상세</button><button type="button" disabled={locked} className="icon-button" aria-label={`${index + 1}행 제외`} onClick={() => removeRow(index)}>×</button></td></tr>)}</tbody></table>{reviewOnly && !!preview.rows.length && !visibleRows.length && <p className="empty-hint">확인이 필요한 행이 없습니다. 전체 행을 보려면 필터를 해제하세요.</p>}{!preview.rows.length && <p className="empty-hint">가져올 행이 없습니다. 다른 파일을 선택해 주세요.</p>}</div>
          {pageCount > 1 && <nav className="import-pagination" aria-label="미리보기 페이지"><button type="button" className="button secondary small" aria-label="이전 미리보기 페이지" disabled={busy || currentPage === 0} onClick={() => setPage(currentPage - 1)}>이전</button><span role="status">{currentPage + 1} / {pageCount}페이지 · {currentPage * pageSize + 1}–{Math.min((currentPage + 1) * pageSize, visibleRows.length)} / {visibleRows.length}건</span><button type="button" className="button secondary small" aria-label="다음 미리보기 페이지" disabled={busy || currentPage + 1 === pageCount} onClick={() => setPage(currentPage + 1)}>다음</button></nav>}
        </>}
      </>}
      {unappliedMapping && <p className="soft-note">변경한 열 연결을 적용한 후 가져오세요.</p>}
      <InlineError message={error} />
    </div><footer className="modal-footer"><p className="field-help">{kind === "holdings" ? `기존 소장목록을 위의 ${totalRows}건으로 교체합니다. 학교의 전체 소장목록인지 확인해 주세요.` : "ISBN·가격 미확인 자료도 후보로 보관할 수 있습니다."}</p><span className="spacer" /><button type="button" className="button secondary" onClick={onClose} disabled={busy}>{saved ? "닫기" : "취소"}</button><button type="button" className="button primary" disabled={busy || !totalRows || (!saved && (!!pendingFiles.length || !!failedFiles.length || unappliedMapping))} onClick={() => { void save(); }}>{busy ? "처리 중…" : saved ? "저장된 결과 다시 열기" : `${totalRows}건 가져오기`}</button></footer>
  </Modal>;
}
