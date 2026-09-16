import { useState } from "react";
import type { LibraryApi } from "./api";
import { InlineError } from "./Modal";
import { errorMessage, type Settings } from "./types";

export function IsbnProviderSetup({ api, configured, onSettings, disabled = false }: { api: LibraryApi; configured: boolean; onSettings?: (settings: Settings) => void; disabled?: boolean }) {
  const [key, setKey] = useState("");
  const [registered, setRegistered] = useState(configured);
  const [busy, setBusy] = useState(false), [error, setError] = useState("");
  async function save() {
    setBusy(true); setError("");
    try {
      const settings = await api.settings({ nl_api_key: key.trim() });
      setKey(""); setRegistered(settings.nl_api_key_configured); onSettings?.(settings);
    } catch (caught) { setError(errorMessage(caught)); } finally { setBusy(false); }
  }
  return <div className="isbn-provider-setup soft-note">
    <strong>{registered ? "국립중앙도서관 인증키 등록됨" : "국내 도서 조회 설정"}</strong>
    <p className="field-help">{registered ? "국립중앙도서관을 먼저 조회합니다. 조회 결과에서 인증 상태와 일치하는 판본을 확인할 수 있습니다." : "승인된 인증키를 한 번 등록하면 ISBN만으로 국내 도서 정보를 불러옵니다. 미등록 시 Open Library에서 조회합니다."}</p>
    <details><summary>{registered ? "인증키 변경" : "국립중앙도서관 인증키 등록"}</summary>
      <p className="field-help"><a href="https://www.nl.go.kr/NL/contents/N31101020000.do" target="_blank" rel="noopener noreferrer">인증키 신청 안내 ↗</a> · 회원 가입 → ISBN 서지정보 API 신청 → 담당자 승인</p>
      <label>국립중앙도서관 인증키<input type="password" autoComplete="new-password" value={key} disabled={busy || disabled} onChange={event => setKey(event.target.value)} placeholder="승인된 인증키를 붙여 넣으세요" /></label>
      <button type="button" className="button secondary small" disabled={busy || disabled || !key.trim()} onClick={() => { void save(); }}>{busy ? "등록 중…" : "인증키 저장"}</button>
      <InlineError message={error} />
    </details>
  </div>;
}
