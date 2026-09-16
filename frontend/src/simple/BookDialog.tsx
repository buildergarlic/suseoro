import { useState, type FormEvent } from "react";
import type { LibraryApi } from "./api";
import { BookLinks } from "./BookLinks";
import { IsbnProviderSetup } from "./IsbnProviderSetup";
import { InlineError, Modal } from "./Modal";
import { emptyBook, errorMessage, type Book, type BookFields, type Settings } from "./types";

export function BookFieldsForm({ value, onChange, disabled = false }: { value: BookFields; onChange: (value: BookFields) => void; disabled?: boolean }) {
  const set = <K extends keyof BookFields>(key: K, next: BookFields[K]) => onChange({ ...value, [key]: next });
  return <fieldset className="book-form-grid book-fields" disabled={disabled}>
    <label className="span-2">책 제목 <span className="required">*</span><input autoComplete="off" value={value.title} onChange={e => set("title", e.target.value)} placeholder="책 제목을 입력해 주세요" /></label>
    <label>저자<input value={value.author} onChange={e => set("author", e.target.value)} /></label>
    <label>출판사<input value={value.publisher} onChange={e => set("publisher", e.target.value)} /></label>
    <label className="span-2">ISBN<input value={value.isbn} onChange={e => set("isbn", e.target.value)} placeholder="10자리 또는 13자리 · 없는 경우 비워 두세요" /></label>
    <div className="span-2"><BookLinks isbn={value.isbn} title={value.title} link={value.link} /></div>
    <label>정가 (원)<input aria-label="정가 (원)" type="number" min="0" step="1" value={value.price ?? ""} onChange={e => set("price", e.target.value === "" ? null : Number(e.target.value))} placeholder="가격 미확인" /><small>빈칸은 가격 미확인으로 저장합니다.</small></label>
    <label>수량<input type="number" min="1" max="9999" step="1" required value={value.quantity} onChange={e => set("quantity", Number(e.target.value))} /></label>
    <label>분류<input value={value.category} onChange={e => set("category", e.target.value)} placeholder="예: 문학 / 813.8" /></label>
    <label>요청자<input value={value.requester} onChange={e => set("requester", e.target.value)} placeholder="예: 김선생님, 3학년" /></label>
    <label>대상<input value={value.audience} onChange={e => set("audience", e.target.value)} placeholder="예: 초등 3–4학년" /></label>
    <label>우선순위<select value={value.priority} onChange={e => set("priority", e.target.value as BookFields["priority"])}><option value="high">높음</option><option value="normal">보통</option><option value="low">낮음</option></select></label>
    <label>추천 출처<input value={value.source} onChange={e => set("source", e.target.value)} placeholder="예: 어린이도서연구회" /></label>
    <label>발행일<input value={value.published_date} onChange={e => set("published_date", e.target.value)} placeholder="예: 2026-03-01" /></label>
    <label className="span-2">참고 링크<input type="url" value={value.link} onChange={e => set("link", e.target.value)} placeholder="https://" /></label>
    <label className="span-2">메모<textarea rows={3} value={value.note} onChange={e => set("note", e.target.value)} placeholder="선정 이유나 확인할 내용을 남겨 주세요" /></label>
    <div className="span-2 form-checks"><label className="check-label"><input type="checkbox" checked={value.selected} onChange={e => set("selected", e.target.checked)} />구입할 책으로 선택</label><label className="check-label"><input type="checkbox" checked={value.needs_review} onChange={e => set("needs_review", e.target.checked)} />확인 필요 표시</label></div>
    {!!value.warnings.length && <div className="span-2 soft-note"><strong>가져올 때 확인할 점</strong><ul>{value.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul><small>내용을 확인한 뒤 위의 ‘확인 필요 표시’를 해제할 수 있습니다.</small></div>}
  </fieldset>;
}

export function BookDialog({ book, api, listId, onClose, onSaved, onDeleted }: { book?: Book; api: LibraryApi; listId: string; onClose: () => void; onSaved: () => Promise<void>; onDeleted: (book: Book) => Promise<void> }) {
  const [value, setValue] = useState<BookFields>(book ?? emptyBook());
  const [busy, setBusy] = useState(false), [error, setError] = useState("");
  async function save(event: FormEvent) {
    event.preventDefault(); setError("");
    if (!value.title.trim() && !value.needs_review) { setError("책 제목을 입력해 주세요. 아직 모르는 경우 ‘확인 필요 표시’를 켜 주세요."); return; }
    if (!Number.isInteger(value.quantity) || value.quantity < 1) { setError("수량은 1권 이상의 정수로 입력해 주세요."); return; }
    setBusy(true);
    try { if (book) await api.updateBook(listId, book.id, value); else await api.addBook(listId, value); await onSaved(); onClose(); }
    catch (caught) { setError(errorMessage(caught)); } finally { setBusy(false); }
  }
  async function remove() { if (!book) return; setBusy(true); setError(""); try { await api.deleteBook(listId, book.id); await onDeleted(book); onClose(); } catch (caught) { setError(errorMessage(caught)); } finally { setBusy(false); } }
  async function lookupMissing() {
    setBusy(true); setError("");
    try {
      const result = await api.lookup(value.isbn);
      if (!result.found || !result.book) { setError(result.warnings.join(" · ") || "일치하는 책을 찾지 못했습니다."); return; }
      const found = result.book;
      setValue(previous => ({ ...previous, title: previous.title || found.title || "", author: previous.author || found.author || "", publisher: previous.publisher || found.publisher || "", price: previous.price ?? found.price ?? null, category: previous.category || found.category || "", published_date: previous.published_date || found.published_date || "", link: previous.link || found.link || "", source: previous.source === "직접 입력" ? found.source || previous.source : previous.source || found.source || "", warnings: [...new Set([...previous.warnings, ...(found.warnings ?? []), ...result.warnings])], needs_review: previous.needs_review || !!found.needs_review || result.warnings.length > 0 }));
    } catch (caught) { setError(errorMessage(caught)); } finally { setBusy(false); }
  }
  return <Modal className={book ? "detail-drawer" : ""} title={book ? "책 정보 수정" : "책 직접 추가"} description="모르는 정보는 비워 두고, 확인한 정보부터 채워 주세요." onClose={onClose} busy={busy}>
    <form onSubmit={event => { void save(event); }}><div className="modal-body"><div className="lookup-inline"><button type="button" className="button secondary small" disabled={busy || !value.isbn.trim()} onClick={() => { void lookupMissing(); }}>ISBN으로 빈 정보 채우기</button><small>입력한 제목·가격은 유지합니다.</small></div><BookFieldsForm disabled={busy} value={value} onChange={setValue} /><InlineError message={error} /></div><footer className="modal-footer">{book && <button type="button" className="danger-text" onClick={() => { void remove(); }} disabled={busy}>이 책 삭제</button>}<span className="spacer" /><button type="button" className="button secondary" onClick={onClose} disabled={busy}>취소</button><button type="submit" className="button primary" disabled={busy}>{busy ? "처리 중…" : "저장"}</button></footer></form>
  </Modal>;
}

interface LookupOutcome { isbn: string; title?: string; success: boolean; message: string }
export function IsbnDialog({ api, listId, onClose, onAdded, onManual, keyConfigured = false, onSettings }: { keyConfigured?: boolean; onSettings?: (settings: Settings) => void; api: LibraryApi; listId: string; onClose: () => void; onAdded: () => Promise<void>; onManual: () => void }) {
  const [value, setValue] = useState(""), [busy, setBusy] = useState(false), [error, setError] = useState("");
  const [results, setResults] = useState<LookupOutcome[]>([]), [progress, setProgress] = useState("");
  const [saved, setSaved] = useState<string[]>([]);
  async function lookup(event: FormEvent) {
    event.preventDefault(); setError("");
    const codes = [...new Set(value.split(/[\n,;]+/).map(code => code.replace(/[\s-]/g, "").trim()).filter(Boolean))];
    if (!codes.length) { setError("조회할 ISBN을 입력해 주세요."); return; }
    if (codes.length > 100) { setError("한 번에 최대 100개까지 조회할 수 있습니다."); return; }
    setBusy(true); setResults([]); let added = 0;
    for (const [index, isbn] of codes.entries()) {
      setProgress(`${index + 1} / ${codes.length}권 조회 중`);
      if (saved.includes(isbn)) { setResults(previous => [...previous, { isbn, success: true, message: "이번 창에서 이미 추가한 ISBN입니다." }]); continue; }
      try {
        const result = await api.lookup(isbn);
        if (!result.found || !result.book) { setResults(previous => [...previous, { isbn, success: false, message: result.warnings.join(" · ") || "일치하는 책을 찾지 못했습니다. 직접 추가할 수 있습니다." }]); continue; }
        const warnings = [...new Set([...(result.book.warnings ?? []), ...result.warnings])];
        const fields = { ...emptyBook(), ...result.book, warnings, needs_review: result.book.needs_review || warnings.length > 0 };
        await api.addBook(listId, fields); added += 1; setSaved(previous => [...previous, isbn]);
        setResults(previous => [...previous, { isbn, title: fields.title, success: true, message: fields.price === null ? "추가됨 · 가격은 직접 확인해 주세요." : "목록에 추가했습니다." }]);
      } catch (caught) { setResults(previous => [...previous, { isbn, success: false, message: errorMessage(caught) }]); }
    }
    if (added) { try { await onAdded(); } catch (caught) { setError(errorMessage(caught)); } }
    setProgress(""); setBusy(false);
  }
  return <Modal title="ISBN으로 책 추가" description="ISBN을 한 줄에 하나씩 붙여 넣으세요. 쉼표로 구분해도 됩니다." onClose={onClose} busy={busy}>
    <IsbnProviderSetup api={api} configured={keyConfigured} onSettings={onSettings} disabled={busy} /><form onSubmit={event => { void lookup(event); }}><div className="modal-body"><label>ISBN<textarea className="isbn-input" rows={5} value={value} onChange={e => setValue(e.target.value)} placeholder={"9788936434267\n9788936442808"} disabled={busy} /></label><p className="field-help">일치하는 판본을 찾으면 목록에 추가합니다. 무료 서지정보에는 가격이 없을 수 있습니다.</p><InlineError message={error} />{busy && <p className="progress-label" role="status">{progress}</p>}{!!results.length && <div className="lookup-results" aria-live="polite">{results.map((result, index) => <div className={`lookup-result ${result.success ? "success" : "attention"}`} key={`${result.isbn}-${index}`}><span aria-hidden="true">{result.success ? "✓" : "!"}</span><div><strong>{result.title || result.isbn}</strong>{result.title && <small>{result.isbn}</small>}<p>{result.message}</p></div></div>)}</div>}</div><footer className="modal-footer"><button type="button" className="text-button" onClick={onManual} disabled={busy}>ISBN 없이 직접 추가</button><span className="spacer" /><button type="button" className="button secondary" onClick={onClose} disabled={busy}>{results.length ? "닫기" : "취소"}</button><button type="submit" className="button primary" disabled={busy || !value.trim()}>{busy ? "조회 중…" : "조회하고 추가"}</button></footer></form>
  </Modal>;
}
