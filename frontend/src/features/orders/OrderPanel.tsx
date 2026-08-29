import { useEffect, useState } from "react";

import type { SuseoroApi, User, Workspace } from "../../api/client";
import type { components } from "../../api/types";
import { BudgetStrip } from "../quotes/BudgetStrip";
import { canPerformAction } from "../workspaces/workflowPolicy";

type CurrentOrder = components["schemas"]["CurrentOrder"];

export function OrderPanel({ api, user, workspace, onWorkspaceChange }: { api: SuseoroApi; user: User; workspace: Workspace; onWorkspaceChange: (workspace: Workspace) => void }) {
  const [order, setOrder] = useState<CurrentOrder | null>(null);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let active = true;
    void api.getCurrentOrder(workspace.id).then((result) => { if (active) setOrder(result.order); }).catch((error: unknown) => { if (active) setMessage(error instanceof Error ? error.message : "발주 정보를 불러오지 못했습니다."); });
    return () => { active = false; };
  }, [api, workspace.id]);

  async function download() {
    if (!order || busy) return;
    setBusy(true);
    try {
      const result = await api.downloadOrder(order.revision_id);
      const url = URL.createObjectURL(result.blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = result.filename;
      anchor.click();
      URL.revokeObjectURL(url);
      setMessage("발주파일이 완성되었습니다. 실제 주문 전 업체에 직접 전달해 주세요.");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "발주파일을 받지 못했습니다.");
    } finally { setBusy(false); }
  }

  async function markSent() {
    if (!order || busy) return;
    setBusy(true);
    try {
      const result = await api.markOrderSent(order.revision_id, workspace.id, "업체에 직접 전달 확인", workspace.row_version);
      onWorkspaceChange({ ...workspace, status: result.state, row_version: result.row_version });
      setMessage("업체 전달을 기록했습니다. 이제 도착한 책을 확인합니다.");
    } catch (error) { setMessage(error instanceof Error ? error.message : "전달 확인을 기록하지 못했습니다."); }
    finally { setBusy(false); }
  }

  if (!order) return <p className="loading-state" role="status">발주파일을 준비하고 있습니다…</p>;
  return <div className="procurement-panel order-panel">
    <div className="section-intro"><div><p className="eyebrow">{order.vendor_name}</p><h3>발주파일 준비</h3><p>수서로는 파일만 만들며 업체에 자동 전송하지 않습니다.</p></div></div>
    <BudgetStrip budgetWon={order.budget_won} quoteWon={order.total_won} />
    <div className="order-actions">
      <button className="button button-primary" data-major-action="DOWNLOAD_ORDER" disabled={busy || !canPerformAction(user, workspace.status, "DOWNLOAD_ORDER")} onClick={() => void download()} type="button">발주파일 받기</button>
      <p>파일을 받은 뒤 학교의 평소 방식으로 업체에 직접 전달해 주세요.</p>
      <button className="button button-secondary" data-major-action="MARK_ORDER_SENT" disabled={busy || !canPerformAction(user, workspace.status, "MARK_ORDER_SENT")} onClick={() => void markSent()} type="button">업체에 전달했어요</button>
    </div>
    {message ? <p className="live-status" role="status">{message}</p> : null}
  </div>;
}
