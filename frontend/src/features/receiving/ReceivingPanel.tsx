import { useEffect, useState } from "react";

import type { SuseoroApi, User, Workspace } from "../../api/client";
import type { components } from "../../api/types";
import {
  ProcurementImportStatus,
} from "../procurement/ProcurementImportStatus";
import {
  PROCUREMENT_FILE_ACCEPT,
  procurementImportMessage,
  type ProcurementImport,
  waitForProcurementJob,
} from "../procurement/procurementImport";
import { canPerformAction } from "../workspaces/workflowPolicy";
import { BarcodeScanner } from "./BarcodeScanner";

type CurrentOrder = components["schemas"]["CurrentOrder"];
type Status = components["schemas"]["ReceivingStatusResponse"];
type Difference = components["schemas"]["ReceivingDifference"];
type DifferenceDetails = components["schemas"]["ReceivingDifferenceDetails"];

const KIND_LABELS: Record<string, string> = {
  MISSING: "누락",
  OVER: "초과",
  QUANTITY: "수량 차이",
  UNIT_PRICE: "단가 차이",
  ISBN: "ISBN 차이",
  EDITION: "판본 차이",
  UNORDERED: "명세서에만 있는 책",
};

const DISPOSITIONS = [
  ["VENDOR_CHECK", "업체 확인"],
  ["RETURN_PLANNED", "반품 예정"],
  ["ADDITIONAL_DELIVERY", "추가 납품"],
  ["ACCEPTED", "그대로 수용"],
] as const;

function describeDifferenceBook(details: DifferenceDetails): string {
  const parts = [
    details.title,
    details.isbn13 ? `ISBN ${details.isbn13}` : null,
    details.edition,
  ].filter((value): value is string => Boolean(value));
  return parts.length ? parts.join(" · ") : "식별 정보가 없는 행";
}

function differenceActual(details: DifferenceDetails): string | number {
  return details.received ?? details.scanned ?? details.scanned_quantity ?? "—";
}

export function ReceivingPanel({ api, user, workspace, onWorkspaceChange }: { api: SuseoroApi; user: User; workspace: Workspace; onWorkspaceChange: (workspace: Workspace) => void }) {
  const [order, setOrder] = useState<CurrentOrder | null>(null);
  const [status, setStatus] = useState<Status | null>(null);
  const [differences, setDifferences] = useState<Difference[]>([]);
  const [imports, setImports] = useState<ProcurementImport[]>([]);
  const [choices, setChoices] = useState<Record<string, string>>({});
  const [deliveryFile, setDeliveryFile] = useState<File | null>(null);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [refresh, setRefresh] = useState(0);

  useEffect(() => {
    let active = true;
    void Promise.all([
      api.getCurrentOrder(workspace.id),
      api.getReceivingStatus(workspace.id),
      api.listReceivingDifferences(workspace.id),
      api.listDeliveries(workspace.id),
      api.listProcurementImports(workspace.id),
    ]).then(([current, progress, differencePage, , importPage]) => {
      if (!active) return;
      setOrder(current.order);
      setStatus(progress);
      setDifferences(differencePage.items);
      setImports(importPage.items);
      setChoices(Object.fromEntries(differencePage.items.map((difference) => [difference.id, difference.disposition ?? ""])));
    }).catch((error: unknown) => { if (active) setMessage(error instanceof Error ? error.message : "납품 진행 상황을 불러오지 못했습니다."); });
    return () => { active = false; };
  }, [api, refresh, workspace.id]);

  async function composeImport(item: ProcurementImport) {
    const result = await api.composeProcurementImport(
      item.import_id,
      workspace.row_version,
    );
    onWorkspaceChange({
      ...workspace,
      status: result.state,
      row_version: result.row_version,
    });
    setMessage("납품명세서를 누적 비교했습니다.");
    setRefresh((value) => value + 1);
  }

  async function addDelivery() {
    if (!order || !deliveryFile || busy) { setMessage("납품명세서 파일을 골라 주세요."); return; }
    setBusy(true);
    try {
      const upload = await api.uploadSources(workspace.id, {
        files: [deliveryFile],
        role: "VENDOR_QUOTE",
        vendorScope: `납품명세서:${order.vendor_name}`,
        procurementKind: "DELIVERY",
        targetRevisionId: order.revision_id,
        reason: "도착한 납품명세서 비교",
      });
      const accepted = upload.items.find((item) => item.status === "ACCEPTED");
      if (!accepted?.procurement_import_id) {
        throw new Error(
          accepted?.error?.message ?? upload.items[0]?.error?.message ??
            "파일을 안전하게 접수하지 못했습니다.",
        );
      }
      if (upload.job_id) await waitForProcurementJob(api, upload.job_id);
      const importPage = await api.listProcurementImports(workspace.id);
      setImports(importPage.items);
      const prepared = importPage.items.find(
        (item) => item.import_id === accepted.procurement_import_id,
      );
      if (!prepared) {
        throw new Error("접수한 파일의 처리 결과를 찾지 못했습니다.");
      }
      if (prepared.status !== "READY") {
        setMessage(procurementImportMessage(prepared));
        return;
      }
      await composeImport(prepared);
      setDeliveryFile(null);
    } catch (error) { setMessage(error instanceof Error ? error.message : "납품명세서를 비교하지 못했습니다."); }
    finally { setBusy(false); }
  }

  async function startScan() {
    if (!order || busy) return;
    setBusy(true);
    try {
      const result = await api.startScanSession(workspace.id, { order_revision_id: order.revision_id, reason: "실물 도서 바코드 검수 시작" }, workspace.row_version);
      onWorkspaceChange({ ...workspace, status: result.state, row_version: result.row_version });
      setMessage("바코드 검수를 시작했습니다.");
      setRefresh((value) => value + 1);
    } catch (error) { setMessage(error instanceof Error ? error.message : "바코드 검수를 시작하지 못했습니다."); }
    finally { setBusy(false); }
  }

  async function saveDisposition(difference: Difference) {
    const disposition = choices[difference.id];
    if (!disposition || busy) return;
    setBusy(true);
    try {
      const result = await api.setReceivingDisposition(difference.id, { workspace_id: workspace.id, disposition, reason: "납품 차이 처리 방침" }, difference.row_version);
      setDifferences((current) => current.map((item) => item.id === difference.id ? { ...item, disposition: result.disposition, row_version: result.row_version } : item));
      setMessage("처리 방침을 기록했습니다.");
      setRefresh((value) => value + 1);
    } catch (error) { setMessage(error instanceof Error ? error.message : "처리 방침을 기록하지 못했습니다."); }
    finally { setBusy(false); }
  }

  async function complete() {
    if (!status?.can_complete || busy) return;
    setBusy(true);
    try {
      const result = await api.completeReceiving(workspace.id, "주문 수량과 차이 처리 확인 완료", workspace.row_version);
      const committed = { ...workspace, status: result.state, row_version: result.row_version };
      onWorkspaceChange(committed);
      try { onWorkspaceChange((await api.getWorkspace(workspace.id)).data); } catch { /* mutation is authoritative */ }
    } catch (error) { setMessage(error instanceof Error ? error.message : "검수를 완료하지 못했습니다."); }
    finally { setBusy(false); }
  }

  if (!order || !status) return <p className="loading-state" role="status">납품 진행 상황을 불러오고 있습니다…</p>;
  return <div className="receiving-panel">
    <div className="section-intro"><div><p className="eyebrow">{order.vendor_name} · {status.delivery_count}차까지 누적</p><h3>도착한 책 확인</h3><p>명세서와 바코드 결과를 한 작업에 차곡차곡 더합니다.</p></div><dl aria-label="수령 진행 요약" className="receiving-progress-summary"><div><dt>납품명세서 반영</dt><dd>{status.delivered_quantity} / {status.ordered_quantity}권</dd></div><div><dt>바코드 확인</dt><dd>{status.scanned_quantity} / {status.ordered_quantity}권</dd></div><div><dt>처리 대기 차이</dt><dd>{status.unresolved_difference_count}건</dd></div></dl></div>
    <div className="receiving-intake">
      <label>납품명세서 파일<input accept={PROCUREMENT_FILE_ACCEPT} type="file" onChange={(event) => setDeliveryFile(event.target.files?.[0] ?? null)} /></label>
      <button className="button button-secondary" disabled={busy || !canPerformAction(user, workspace.status, "ADD_DELIVERY")} onClick={() => void addDelivery()} type="button">납품명세서 비교하기</button>
      {!status.active_session_id ? <button className="button button-primary" disabled={busy || !canPerformAction(user, workspace.status, "START_SCAN")} onClick={() => void startScan()} type="button">바코드 검수 시작</button> : null}
      <p className="field-note">CSV·엑셀은 서버 공통 파서로 읽고, 원본과 행별 근거를 작업에 보관합니다.</p>
    </div>
    <ProcurementImportStatus
      busy={busy}
      imports={imports}
      kind="DELIVERY"
      onCompose={(item) => {
        if (busy) return;
        setBusy(true);
        void composeImport(item)
          .catch((error: unknown) => setMessage(error instanceof Error ? error.message : "납품명세서를 반영하지 못했습니다."))
          .finally(() => setBusy(false));
      }}
    />
    {status.active_session_id ? <BarcodeScanner api={api} onProgress={() => setRefresh((value) => value + 1)} orderRows={status.rows} sessionId={status.active_session_id} workspaceId={workspace.id} /> : null}
    <section aria-labelledby="difference-title" className="difference-section"><h4 id="difference-title">발주와 다른 내용</h4>
      {differences.length ? <ul className="difference-list">{differences.map((difference) => <li key={difference.id}><div><strong>{KIND_LABELS[difference.kind] ?? difference.kind}</strong><p className="difference-identity">{describeDifferenceBook(difference.details)}</p><p>발주 기준 {difference.details.expected ?? "—"} · 도착 확인 {differenceActual(difference.details)}</p></div><div className="difference-action"><label>처리 방침<select aria-label="처리 방침" value={choices[difference.id] ?? ""} onChange={(event) => setChoices((current) => ({ ...current, [difference.id]: event.target.value }))}><option value="">선택해 주세요</option>{DISPOSITIONS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label><button className="button button-secondary" disabled={busy || !choices[difference.id]} onClick={() => void saveDisposition(difference)} type="button">방침 기록</button></div></li>)}</ul> : <p className="empty-state">현재 처리할 차이가 없습니다.</p>}
    </section>
    <div className="completion-card"><div><h4>검수 완료</h4>{status.blocking_reasons.length ? <ul>{status.blocking_reasons.map((reason) => <li key={reason}>{reason}</li>)}</ul> : <p>모든 주문 수량과 차이 처리 방침을 확인했습니다.</p>}</div><button className="button button-primary" disabled={busy || !status.can_complete || !canPerformAction(user, workspace.status, "COMPLETE_RECEIVING")} onClick={() => void complete()} type="button">검수 완료하고 작업 끝내기</button></div>
    {message ? <p className="live-status" role="status">{message}</p> : null}
  </div>;
}
