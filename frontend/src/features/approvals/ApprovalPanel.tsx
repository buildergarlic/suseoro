import { useEffect, useRef, useState } from "react";

import type { SuseoroApi, User, Workspace } from "../../api/client";
import type { components } from "../../api/types";
import { ModalDialog } from "../../components/ModalDialog";
import { canPerformAction } from "../workspaces/workflowPolicy";

type ApprovalDetail = components["schemas"]["ApprovalDetailResponse"];

interface ApprovalPanelProps {
  api: SuseoroApi;
  user: User;
  workspace: Workspace;
  onWorkspaceChange: (workspace: Workspace) => void;
}

const won = new Intl.NumberFormat("ko-KR");
const localDate = new Intl.DateTimeFormat("ko-KR", {
  year: "numeric",
  month: "numeric",
  day: "numeric",
});

function previousCopy(detail: ApprovalDetail): string {
  const previous = detail.metadata.previous_revision;
  if (previous.revision_number === null) return "첫 승인 요청입니다.";
  const parts = [
    previous.added_count ? `${previous.added_count}권 추가` : "",
    previous.removed_count ? `${previous.removed_count}권 제외` : "",
    previous.quantity_changed_count
      ? `수량 ${previous.quantity_changed_count}건 변경`
      : "",
    previous.price_changed_count
      ? `가격 ${previous.price_changed_count}건 변경`
      : "",
  ].filter(Boolean);
  return parts.length ? `이전 버전보다 ${parts.join(" · ")}` : "이전 버전과 같은 목록입니다.";
}

async function latestApproval(api: SuseoroApi, workspaceId: string) {
  const page = await api.listApprovals(workspaceId);
  const latest = page.items[0];
  return latest ? await api.getApproval(latest.id) : null;
}

function apiErrorCode(error: unknown): string | null {
  if (!error || typeof error !== "object" || !("detail" in error)) return null;
  const detail = error.detail;
  return detail && typeof detail === "object" && "code" in detail
    ? String(detail.code)
    : null;
}

export function ApprovalPanel({
  api,
  user,
  workspace,
  onWorkspaceChange,
}: ApprovalPanelProps) {
  const [detail, setDetail] = useState<ApprovalDetail | null>(null);
  const [message, setMessage] = useState("");
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [changesOpen, setChangesOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [reasonError, setReasonError] = useState("");
  const [staleConflict, setStaleConflict] = useState(false);
  const reasonRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    let active = true;
    void latestApproval(api, workspace.id)
      .then((approval) => {
        if (active) setDetail(approval);
      })
      .catch((error: unknown) => {
        if (active) {
          setMessage(
            error instanceof Error
              ? error.message
              : "승인 요청을 불러오지 못했습니다.",
          );
        }
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [api, workspace.id]);

  async function applyDecision(kind: "APPROVE" | "CHANGES", decisionReason: string) {
    if (!detail || submitting) return;
    setSubmitting(true);
    setMessage("");
    try {
      const result =
        kind === "APPROVE"
          ? await api.approveApproval(
              detail.revision_id,
              workspace.id,
              decisionReason,
              workspace.row_version,
            )
          : await api.requestApprovalChanges(
              detail.revision_id,
              workspace.id,
              decisionReason,
              workspace.row_version,
            );
      onWorkspaceChange({
        ...workspace,
        status: result.state,
        row_version: result.row_version,
      });
      setChangesOpen(false);
      setMessage(
        kind === "APPROVE"
          ? "목록을 승인했습니다. 담당자가 견적을 비교할 수 있습니다."
          : "수정 요청을 보냈습니다.",
      );
    } catch (error) {
      const code = apiErrorCode(error);
      if (["ROW_VERSION_CONFLICT", "CANDIDATE_COLLECTION_CHANGED", "APPROVAL_REVISION_STALE", "APPROVAL_REVISION_NOT_CURRENT"].includes(code ?? "")) {
        setStaleConflict(true);
        setMessage("");
        return;
      }
      setMessage(
        error instanceof Error ? error.message : "승인 결정을 반영하지 못했습니다.",
      );
    } finally {
      setSubmitting(false);
    }
  }

  async function reloadAfterConflict() {
    if (submitting) return;
    setSubmitting(true);
    try {
      const current = await api.getWorkspace(workspace.id);
      const approval = await latestApproval(api, workspace.id);
      setDetail(approval);
      onWorkspaceChange(current.data);
      setStaleConflict(false);
      setMessage("최신 승인 요청을 다시 불러왔습니다.");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "최신 승인 요청을 불러오지 못했습니다.");
    } finally {
      setSubmitting(false);
    }
  }

  if (loading) {
    return <p className="loading-state" role="status">승인 요청을 불러오고 있습니다…</p>;
  }
  if (!detail) {
    return <div className="calm-placeholder"><h3>승인 요청 확인</h3><p role="status">{message || "아직 승인 요청이 없습니다."}</p></div>;
  }

  const metadata = detail.metadata;
  const catalogDate = metadata.catalog_as_of_local_date
    ? localDate.format(new Date(`${metadata.catalog_as_of_local_date}T00:00:00`))
    : "확인 필요";
  return (
    <div className="procurement-panel approval-review-panel">
      <div className="section-intro">
        <div>
          <p className="eyebrow">승인 버전 {detail.revision_number}</p>
          <h3>목록의 핵심만 확인해 주세요</h3>
          <p>{metadata.requested_by_display_name} 님이 보낸 변경 불가능한 목록입니다.</p>
        </div>
      </div>
      <dl className="summary-grid" aria-label="승인 목록 요약">
        <div><dt>후보</dt><dd>후보 {metadata.candidate_count}권</dd></div>
        <div><dt>예상 금액</dt><dd>{won.format(detail.payload.expected_total_won)}원</dd></div>
        <div><dt>승인 예산</dt><dd>{won.format(detail.payload.budget_won)}원</dd></div>
        <div><dt>장서 기준일</dt><dd>장서 기준일 {catalogDate}</dd></div>
        <div>
          <dt>확인 필요</dt>
          <dd>{metadata.unresolved_complete && metadata.source_counts_verified ? "확인 필요 0건 · 처리 건수 검증됨" : "처리 건수 확인 필요"}</dd>
        </div>
        <div><dt>이전 변경</dt><dd>{previousCopy(detail)}</dd></div>
      </dl>
      <div className="quiet-summary">
        <strong>자동 제외 {metadata.auto_excluded_count}권</strong>
        {metadata.auto_exclusions.length ? (
          <span>{metadata.auto_exclusions.map((item) => `${item.reason} ${item.count}권`).join(" · ")}</span>
        ) : <span>자동 제외된 책이 없습니다.</span>}
      </div>
      {staleConflict ? (
        <div className="message message-error" role="alert">
          <p>승인 목록이 바뀌었습니다. 최신 승인 요청을 다시 확인해 주세요.</p>
          <button className="button button-secondary" disabled={submitting} onClick={() => void reloadAfterConflict()} type="button">최신 승인 요청 다시 불러오기</button>
        </div>
      ) : message ? <p className="live-status" role="status">{message}</p> : null}
      {canPerformAction(user, workspace.status, "APPROVE_LIST") ? (
        <div className="primary-action-row">
          <button className="button button-secondary" disabled={submitting} onClick={() => setChangesOpen(true)} type="button">수정 요청 보내기</button>
          <button className="button button-primary" data-major-action="APPROVE_LIST" disabled={submitting || !metadata.source_counts_verified} onClick={() => void applyDecision("APPROVE", "목록 검토 완료")} type="button">이 목록 승인</button>
        </div>
      ) : (
        <p className="calm-note" role="status">검토 담당자의 결정을 기다리고 있습니다.</p>
      )}
      {changesOpen ? (
        <ModalDialog labelledBy="changes-title" initialFocusRef={reasonRef} onClose={() => setChangesOpen(false)}>
          <div className="dialog-heading"><h2 id="changes-title">수정 요청 보내기</h2><p>담당자가 바로 고칠 수 있도록 필요한 내용을 적어 주세요.</p></div>
          <label>수정 요청 사유<textarea ref={reasonRef} rows={4} value={reason} onChange={(event) => { setReason(event.target.value); setReasonError(""); }} /></label>
          {reasonError ? <p className="message message-error" role="alert">{reasonError}</p> : null}
          <div className="dialog-actions">
            <button className="button button-quiet" onClick={() => setChangesOpen(false)} type="button">취소</button>
            <button className="button button-primary" disabled={submitting} onClick={() => {
              if (!reason.trim()) { setReasonError("수정 요청 사유를 적어 주세요."); return; }
              void applyDecision("CHANGES", reason.trim());
            }} type="button">수정 요청 보내기</button>
          </div>
        </ModalDialog>
      ) : null}
    </div>
  );
}

export function ChangesRequestedNotice({ api, workspace }: Pick<ApprovalPanelProps, "api" | "workspace">) {
  const [reason, setReason] = useState("수정 요청 내용을 불러오고 있습니다…");
  useEffect(() => {
    let active = true;
    void api.listApprovals(workspace.id).then(async (page) => page.items[0] ? await api.getApproval(page.items[0].id) : null).then((approval) => {
      if (active) setReason(approval?.metadata.decision_reason || "수정 요청 사유를 확인해 주세요.");
    }).catch(() => { if (active) setReason("수정 요청 내용을 다시 불러와 주세요."); });
    return () => { active = false; };
  }, [api, workspace.id]);
  return <aside className="changes-requested-notice" aria-labelledby="changes-requested-title"><h3 id="changes-requested-title">검토자가 수정을 요청했습니다</h3><p>{reason}</p><p>아래 후보를 고친 뒤 새 버전으로 다시 승인 요청해 주세요.</p></aside>;
}
