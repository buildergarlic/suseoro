import { useEffect, useState } from "react";
import type { LibraryApi } from "./api";
import { InlineError, Modal } from "./Modal";
import { errorMessage, money, type ListDetail, type Template } from "./types";

export function ExportDialog({ api, detail, school, onClose }: { api: LibraryApi; detail: ListDetail; school: string; onClose: () => void }) {
  const [format, setFormat] = useState<"xlsx" | "csv" | "html">("xlsx"), [templateId, setTemplateId] = useState("");
  const [templates, setTemplates] = useState<Template[]>([]), [busy, setBusy] = useState(false), [error, setError] = useState(""), [notice, setNotice] = useState("");
  useEffect(() => { let active = true; void api.templates().then(result => { if (active) setTemplates(result); }).catch((caught: unknown) => { if (active) setError(errorMessage(caught)); }); return () => { active = false; }; }, [api]);
  async function upload(file: File | undefined) { if (!file) return; setBusy(true); setError(""); try { const template = await api.uploadTemplate(file); setTemplates(previous => [...previous.filter(item => item.id !== template.id), template]); setTemplateId(template.id); setFormat("xlsx"); setNotice(template.warnings?.join(" · ") || "학교 양식을 추가했습니다."); } catch (caught) { setError(errorMessage(caught)); } finally { setBusy(false); } }
  async function save() { setError(""); setNotice(""); setBusy(true); try { await api.exportList(detail.list.id, format, format === "xlsx" && templateId ? templateId : undefined); setNotice("발주서 다운로드를 시작했습니다. 저장 위치를 확인해 주세요."); } catch (caught) { setError(errorMessage(caught)); } finally { setBusy(false); } }
  const selected = detail.books.filter(book => book.selected);
  const reviewCount = selected.filter(book => book.needs_review || !book.title.trim()).length;
  const missingCount = selected.filter(book => book.price === null).length;
  const blocked = detail.summary.remaining < 0 || !!reviewCount || !!missingCount || !selected.length;
  return <Modal title="발주서 저장" description="구입 선택한 책을 학교 발주서로 정리합니다." onClose={onClose} busy={busy}>
    <div className="modal-body"><div className="export-receipt"><span>{school || "우리 학교"}</span><strong>{detail.list.name}</strong><div><span>구입 선택 {selected.length}종 · {detail.summary.total_quantity}권</span><b>{money(detail.summary.order_total)}</b></div><small>목록 할인율 {detail.list.discount_percent}% 적용</small></div>
      {blocked && <div className="soft-note"><strong>저장 전에 확인해 주세요</strong><ul>{!selected.length && <li>구입할 책을 먼저 선택해 주세요.</li>}{missingCount > 0 && <li>가격 미확인 {missingCount}종의 정가를 입력해 주세요.</li>}{reviewCount > 0 && <li>확인 필요 {reviewCount}종의 내용을 확인해 주세요.</li>}{detail.summary.remaining < 0 && <li>선택금액이 예산보다 {money(-detail.summary.remaining)} 많습니다.</li>}</ul></div>}
      <fieldset className="format-options"><legend>저장 형식</legend>{([{ value: "xlsx", title: "Excel", text: ".xlsx · 편집 가능한 발주서" }, { value: "csv", title: "CSV", text: ".csv · 다른 시스템에 업로드" }, { value: "html", title: "인쇄용", text: ".html · 열어서 인쇄 / PDF 저장" }] as const).map(option => <label className={format === option.value ? "active" : ""} key={option.value}><input type="radio" name="export-format" value={option.value} checked={format === option.value} onChange={() => setFormat(option.value)} /><strong>{option.title}</strong><small>{option.text}</small></label>)}</fieldset>
      {format === "xlsx" && <div className="template-controls"><label>발주서 양식<select value={templateId} onChange={e => setTemplateId(e.target.value)}><option value="">수서로 기본 양식</option>{templates.map(template => <option key={template.id} value={template.id}>{template.name}</option>)}</select></label><label className="file-button">학교 양식 추가 (.xlsx)<input type="file" aria-label="학교 발주서 양식" accept=".xlsx" disabled={busy} onChange={e => { void upload(e.target.files?.[0]); e.target.value = ""; }} /></label><p className="field-help">학교에서 쓰는 Excel 양식의 제목·ISBN·정가 열을 자동 인식합니다.</p></div>}
      <InlineError message={error} />{notice && <p className="inline-success" role="status">{notice}</p>}
    </div><footer className="modal-footer"><button className="button secondary" onClick={onClose} disabled={busy}>닫기</button><span className="spacer" /><button className="button primary" onClick={() => { void save(); }} disabled={busy || blocked}>{busy ? "만드는 중…" : "발주서 저장"}</button></footer>
  </Modal>;
}
