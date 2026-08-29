import { useEffect, useRef, useState } from "react";

import type { SuseoroApi } from "../../api/client";
import type { components } from "../../api/types";
import { playScanAudio, type ScanCode } from "./scanFeedback";

type ProgressRow = components["schemas"]["ReceivingProgressRow"];

const FEEDBACK: Record<ScanCode, { title: string; help: string }> = {
  NORMAL: { title: "확인됨", help: "주문한 수량 안에서 확인했습니다." },
  OVER: { title: "수량 초과", help: "주문 수량보다 많이 스캔했습니다." },
  NOT_ORDERED: { title: "미주문", help: "현재 발주 목록에 없는 ISBN입니다." },
  EDITION_MISMATCH: { title: "판본 차이", help: "예상한 책과 ISBN 또는 판본이 다릅니다." },
};

function feedbackCode(code: string): ScanCode {
  return code === "UNORDERED" ? "NOT_ORDERED" : code in FEEDBACK ? code as ScanCode : "NOT_ORDERED";
}

function normalizeBarcode(value: string): string {
  return value.trim().toUpperCase().replaceAll(/[^0-9X]/g, "");
}

function commandKey(): string {
  return typeof crypto.randomUUID === "function"
    ? crypto.randomUUID()
    : `scan-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

export function BarcodeScanner({ api, workspaceId, sessionId, orderRows, onProgress }: {
  api: SuseoroApi;
  workspaceId: string;
  sessionId: string;
  orderRows: ProgressRow[];
  onProgress: () => void;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const queueRef = useRef(Promise.resolve());
  const retryRef = useRef<{ isbn: string; key: string } | null>(null);
  const inputVersionRef = useRef(0);
  const [value, setValue] = useState("");
  const [expectedRowId, setExpectedRowId] = useState("");
  const [feedback, setFeedback] = useState<{ code: ScanCode; title: string; help: string } | null>(null);

  useEffect(() => { inputRef.current?.focus(); }, [sessionId]);

  function submit() {
    const isbn = normalizeBarcode(value);
    if (!isbn) {
      inputRef.current?.focus();
      return;
    }
    const retry = retryRef.current?.isbn === isbn ? retryRef.current : null;
    const key = retry?.key ?? commandKey();
    const submittedInputVersion = inputVersionRef.current;
    setValue("");
    const run = async () => {
      try {
        const result = await api.recordScan(
          sessionId,
          { workspace_id: workspaceId, isbn, expected_order_row_id: expectedRowId || null },
          { commandKey: key },
        );
        retryRef.current = null;
        const code = feedbackCode(result.code);
        setFeedback({ code, ...FEEDBACK[code] });
        playScanAudio(code);
        onProgress();
      } catch (error) {
        if (inputVersionRef.current === submittedInputVersion) {
          retryRef.current = { isbn, key };
          setValue(isbn);
        }
        setFeedback({ code: "NOT_ORDERED", title: "다시 확인", help: error instanceof Error ? error.message : "스캔 결과를 확인하지 못했습니다. Enter로 다시 시도해 주세요." });
      } finally {
        window.setTimeout(() => { inputRef.current?.focus(); }, 0);
      }
    };
    const queued = queueRef.current.then(run, run);
    queueRef.current = queued.then(() => undefined, () => undefined);
  }

  return <section className="scanner-focus" aria-labelledby="scanner-title">
    <div><p className="eyebrow">클릭 없이 계속 스캔해요</p><h4 id="scanner-title">바코드 집중 모드</h4></div>
    <div className="scanner-inputs">
      <label>ISBN 바코드<input autoComplete="off" inputMode="numeric" ref={inputRef} value={value} onChange={(event) => { inputVersionRef.current += 1; setValue(event.target.value); }} onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); submit(); } }} /></label>
      <label className="scanner-mismatch-field">판본 차이를 확인할 예상 도서 (선택)<select value={expectedRowId} onChange={(event) => setExpectedRowId(event.target.value)}><option value="">자동으로 찾기</option>{orderRows.map((row) => <option key={row.order_row_id} value={row.order_row_id}>{row.title}{row.edition ? ` · ${row.edition}` : ""}</option>)}</select></label>
    </div>
    <div aria-atomic="true" aria-live="assertive" className={`scan-feedback ${feedback ? `scan-${feedback.code.toLocaleLowerCase()}` : ""}`} role="status">
      {feedback ? <><strong>{feedback.title}</strong><span>{feedback.help}</span></> : <><strong>스캔 대기</strong><span>바코드를 읽으면 바로 결과를 알려드립니다.</span></>}
    </div>
  </section>;
}
