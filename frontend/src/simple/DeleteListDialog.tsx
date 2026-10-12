import { useCallback, useRef, useState } from "react";
import type { LibraryApi } from "./api";
import { InlineError, Modal } from "./Modal";
import { errorMessage, type AcquisitionList } from "./types";

export function DeleteListDialog({ list, api, onClose, onDeleted, returnFocusTarget }: { list: AcquisitionList; api: LibraryApi; onClose: () => void; onDeleted: (list: AcquisitionList) => void; returnFocusTarget: () => HTMLElement | null }) {
  const [busy, setBusy] = useState(false), [error, setError] = useState("");
  const succeeded = useRef(false);
  const returnFocus = useCallback(() => succeeded.current ? returnFocusTarget() : null, [returnFocusTarget]);
  async function remove() {
    setBusy(true); setError("");
    try { const deleted = await api.deleteList(list.id); succeeded.current = true; onDeleted(deleted); onClose(); }
    catch (caught) { setError(errorMessage(caught)); }
    finally { setBusy(false); }
  }
  return <Modal title="구입 목록을 삭제할까요?" description={`‘${list.name}’ 목록을 삭제합니다.`} onClose={onClose} busy={busy} returnFocusTarget={returnFocus}>
    <div className="modal-body"><p>목록에 담긴 구입 후보와 추천 자료가 함께 숨겨집니다. 학교 소장목록은 그대로입니다.</p><p>삭제한 목록과 책은 보관되며, 메뉴의 <strong>삭제한 목록</strong>에서 언제든 복원할 수 있습니다.</p><InlineError message={error} /></div>
    <footer className="modal-footer"><span className="spacer" /><button className="button secondary" disabled={busy} onClick={onClose}>취소</button><button className="button danger" disabled={busy} onClick={() => { void remove(); }}>{busy ? "삭제 중…" : "목록 삭제"}</button></footer>
  </Modal>;
}
