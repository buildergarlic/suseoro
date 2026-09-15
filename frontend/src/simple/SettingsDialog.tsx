import { useState, type FormEvent } from "react";
import type { LibraryApi } from "./api";
import { InlineError, Modal } from "./Modal";
import { errorMessage, type AcquisitionList, type Settings, type UpdateInfo } from "./types";

export function ListDialog({ list, api, onClose, onSaved }: { list?: AcquisitionList; api: LibraryApi; onClose: () => void; onSaved: (list: AcquisitionList) => Promise<void> }) {
  const [name, setName] = useState(list?.name ?? `${new Date().getFullYear()} 도서 구입 목록`), [year, setYear] = useState(list?.year ?? new Date().getFullYear());
  const [budget, setBudget] = useState(list?.budget ?? 15_000_000), [discount, setDiscount] = useState(list?.discount_percent ?? 0);
  const [busy, setBusy] = useState(false), [error, setError] = useState("");
  async function save(event: FormEvent) { event.preventDefault(); setBusy(true); setError(""); try { const data = { name: name.trim(), year, budget, discount_percent: discount }; const result = list ? await api.updateList(list.id, data) : await api.createList(data); await onSaved(result); onClose(); } catch (caught) { setError(errorMessage(caught)); } finally { setBusy(false); } }
  return <Modal title={list ? "목록과 예산 설정" : "새 구입 목록"} description="학기나 사업별로 목록을 나누어 관리할 수 있어요." onClose={onClose} busy={busy}><form onSubmit={event => { void save(event); }}><div className="modal-body book-form-grid"><label className="span-2">목록 이름<input required maxLength={120} value={name} onChange={e => setName(e.target.value)} /></label><label>연도<input type="number" min="2000" max="2200" required value={year} onChange={e => setYear(Number(e.target.value))} /></label><label>예산 (원)<input type="number" min="0" step="1" required value={budget} onChange={e => setBudget(Number(e.target.value))} /></label><label>할인율 (%)<input type="number" min="0" max="100" step="0.01" required value={discount} onChange={e => setDiscount(Number(e.target.value))} /></label><p className="field-help">할인 후 한 권의 금액을 원 단위로 반올림하고, 수량을 곱합니다.</p><div className="span-2"><InlineError message={error} /></div></div><footer className="modal-footer"><span className="spacer" /><button type="button" className="button secondary" onClick={onClose} disabled={busy}>취소</button><button type="submit" className="button primary" disabled={busy}>{busy ? "저장 중…" : "저장"}</button></footer></form></Modal>;
}

export function SettingsDialog({ settings, version, update, api, onClose, onSettings, onRestored, onExited, onUpdate, onUpdateStarted }: { settings: Settings; version: string; update: UpdateInfo | null; api: LibraryApi; onClose: () => void; onSettings: (settings: Settings) => void; onRestored: () => Promise<void>; onExited: () => void; onUpdate: (update: UpdateInfo) => void; onUpdateStarted: () => void }) {
  const [school, setSchool] = useState(settings.school_name), [key, setKey] = useState("");
  const [keyConfigured, setKeyConfigured] = useState(settings.nl_api_key_configured);
  const [busy, setBusy] = useState(false), [error, setError] = useState(""), [notice, setNotice] = useState("");
  const [restoreFile, setRestoreFile] = useState<File | null>(null), [confirmRestore, setConfirmRestore] = useState(false);
  async function action(work: () => Promise<void>) { setError(""); setNotice(""); setBusy(true); try { await work(); } catch (caught) { setError(errorMessage(caught)); } finally { setBusy(false); } }
  async function save(event: FormEvent) { event.preventDefault(); await action(async () => { const result = await api.settings({ school_name: school.trim(), ...(key.trim() ? { nl_api_key: key.trim() } : {}) }); onSettings(result); setKeyConfigured(result.nl_api_key_configured); setKey(""); setNotice("설정을 저장했습니다."); }); }
  const updateUrl = update?.url && /^https:\/\/github\.com\//i.test(update.url) ? update.url : undefined;
  const automaticOnExit = update?.auto_supported === true && update.auto_enabled === true;
  const updateWorking = update?.phase === "checking" || update?.phase === "downloading";
  const unsavedSettings = school.trim() !== settings.school_name || !!key.trim();
  const updateTitle = update?.phase === "checking" ? "새 버전을 확인하고 있습니다."
    : update?.phase === "downloading" ? "새 버전을 내려받고 있습니다."
      : update?.phase === "ready" ? `새 버전 ${update.latest_version ?? ""}이 준비되었습니다.`
        : update?.phase === "error" ? "업데이트를 완료하지 못했습니다."
          : update?.available ? `새 버전 ${update.latest_version ?? ""}을 사용할 수 있어요.`
            : update?.message || "업데이트 상태를 확인해 주세요.";
  return <Modal title="학교 설정" description="이 컴퓨터에서 사용할 정보를 한 번만 입력하세요." onClose={onClose} busy={busy}>
    <div className="modal-body settings-body"><form onSubmit={event => { void save(event); }}><section><h3>우리 학교</h3><label>학교 이름<input value={school} onChange={e => setSchool(e.target.value)} placeholder="예: 햇살초등학교" maxLength={120} /></label><label>국립중앙도서관 인증키 <span className="optional">선택</span><input type="password" value={key} onChange={e => setKey(e.target.value)} placeholder={keyConfigured ? "인증키가 등록되어 있습니다" : "인증키가 있다면 붙여 넣으세요"} autoComplete="new-password" /></label><p className="field-help">등록하지 않아도 Open Library에서 ISBN을 조회할 수 있습니다. 저장한 인증키는 화면과 백업에 표시하지 않습니다.</p><div className="settings-action-row">{keyConfigured && <button type="button" className="text-button" disabled={busy} onClick={() => { void action(async () => { const result = await api.settings({ nl_api_key: "" }); onSettings(result); setKeyConfigured(false); setKey(""); setNotice("인증키를 삭제했습니다."); }); }}>등록한 인증키 삭제</button>}<span className="spacer" /><button type="submit" className="button primary small" disabled={busy}>설정 저장</button></div></section></form>
      <section><h3>자료 백업과 복원</h3><p className="field-help">목록은 이 컴퓨터에 자동 저장됩니다. 다른 컴퓨터로 옮기기 전에는 백업 파일을 보관하세요.</p><div className="settings-action-row"><button className="button secondary small" disabled={busy} onClick={() => { void action(async () => { await api.backup(); setNotice("백업 파일 다운로드를 시작했습니다."); }); }}>백업 파일 저장</button><label className={`file-button ${busy ? "disabled" : ""}`}>백업 불러오기<input type="file" aria-label="복원할 백업 파일" accept=".json" disabled={busy} onChange={e => { setRestoreFile(e.target.files?.[0] ?? null); setConfirmRestore(false); e.target.value = ""; }} /></label></div>
        {restoreFile && <div className="restore-confirm"><strong>{restoreFile.name}</strong><p>이 파일의 자료로 현재 모든 목록과 소장목록, 학교 설정을 바꿉니다. 복원 직전 자료는 자동으로 별도 백업됩니다.</p><label className="check-label"><input type="checkbox" checked={confirmRestore} onChange={e => setConfirmRestore(e.target.checked)} disabled={busy} />현재 자료가 교체되는 것을 확인했습니다.</label><div className="settings-action-row"><button className="button secondary small" onClick={() => setRestoreFile(null)} disabled={busy}>취소</button><button className="button danger small" disabled={busy || !confirmRestore} onClick={() => { void action(async () => { await api.restore(restoreFile); await onRestored(); onClose(); }); }}>이 백업으로 복원</button></div></div>}
      </section>
      <section aria-label="업데이트 설정"><div className="section-title"><h3>수서로 <span>v{version}</span></h3><button className="button secondary small" disabled={busy || updateWorking} onClick={() => { void action(async () => { onUpdate(await api.updates()); }); }}>업데이트 확인</button></div>
        <label className="check-label automatic-update-option"><input type="checkbox" checked={update?.auto_enabled === true} disabled={busy || update?.auto_supported !== true} onChange={event => { const enabled = event.target.checked; void action(async () => { onUpdate(await api.updatePreferences(enabled)); setNotice(enabled ? "자동 업데이트를 켰습니다." : "자동 업데이트를 껐습니다."); }); }} />자동 업데이트</label>
        <p className="field-help">{update?.auto_supported === false ? "자동 업데이트는 Windows 설치형에서 사용할 수 있습니다. 무설치 버전에서는 업데이트 확인 후 설치 파일을 직접 실행해 주세요." : update?.auto_supported === true ? automaticOnExit ? "새 버전을 미리 내려받고 수서로를 정상 종료할 때 설치합니다. 작업 중인 창을 자동으로 닫지 않습니다." : "자동으로 내려받거나 종료할 때 설치하지 않습니다. 업데이트 확인과 설치는 직접 할 수 있습니다." : "업데이트 확인을 누르면 이 실행 방식에서 자동 업데이트를 사용할 수 있는지 확인할 수 있습니다."}</p>
        {update && <div className={`update-result${update.phase === "error" ? " update-error" : ""}`} role="status"><strong>{updateTitle}</strong>
          {update.phase === "ready" && <p className="field-help">{automaticOnExit ? "수서로를 닫으면 새 버전이 자동으로 설치됩니다." : "원할 때 지금 업데이트를 눌러 설치할 수 있습니다."}</p>}
          {update.phase === "downloading" && <p className="field-help">수서 작업을 계속하셔도 됩니다. 내려받기가 끝나면 알려드립니다.</p>}
          {update.message && update.message !== updateTitle && <p className="field-help">{update.message}</p>}
          {update.available && !updateWorking && <><p className="field-help">{unsavedSettings ? "바꾼 학교 정보를 먼저 설정 저장한 뒤 업데이트해 주세요." : "설치를 누르면 수서로를 닫고 설치 프로그램을 엽니다."}</p><div className="settings-action-row"><button className="button primary small" disabled={busy || unsavedSettings} onClick={() => { void action(async () => { const result = await api.installUpdate(); if (!result.started) throw new Error("설치 프로그램을 시작하지 못했습니다. 다운로드 링크를 이용해 주세요."); onUpdateStarted(); }); }}>{update.phase === "ready" ? "지금 업데이트" : "업데이트 설치"}</button>{updateUrl && <a className="text-button" href={updateUrl} target="_blank" rel="noreferrer">설치 파일 다운로드 ↗</a>}</div></>}
        </div>}
      </section>
      <InlineError message={error} />{notice && <p className="inline-success" role="status">{notice}</p>}
    </div><footer className="modal-footer"><button className="text-button" disabled={busy} onClick={() => { void action(async () => { await api.shutdown(); onExited(); }); }}>수서로 종료</button><span className="spacer" /><button className="button secondary" onClick={onClose} disabled={busy}>닫기</button></footer>
  </Modal>;
}
