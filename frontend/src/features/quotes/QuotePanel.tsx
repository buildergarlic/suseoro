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
import { BudgetStrip } from "./BudgetStrip";

type Approval = components["schemas"]["ApprovalDetailResponse"];
type Quote = components["schemas"]["QuoteSummary"];

interface QuotePanelProps {
  api: SuseoroApi;
  user: User;
  workspace: Workspace;
  onWorkspaceChange: (workspace: Workspace) => void;
}

const won = new Intl.NumberFormat("ko-KR");

function quoteBlockerReasons(quote: Quote): string[] {
  const reasons: string[] = [];
  if (quote.out_of_stock_count > 0) reasons.push(`품절 도서 ${quote.out_of_stock_count}건을 먼저 조정해 주세요.`);
  if (quote.missing_price_count > 0) reasons.push(`가격이 없는 도서 ${quote.missing_price_count}건을 먼저 확인해 주세요.`);
  if (quote.list_mismatch_count > 0) reasons.push(`승인 목록과 가격·수량이 다른 도서 ${quote.list_mismatch_count}건을 먼저 확인해 주세요.`);
  if (quote.unmatched_count > 0) reasons.push(`승인 목록에 연결되지 않은 도서 ${quote.unmatched_count}건을 먼저 연결해 주세요.`);
  if (quote.needs_review_count > 0) reasons.push(`직접 확인할 도서 ${quote.needs_review_count}건의 연결을 마쳐 주세요.`);
  if (quote.requires_reapproval) reasons.push("승인 예산이나 목록이 바뀌어 다시 승인이 필요합니다.");
  return reasons;
}

async function loadQuoteReview(api: SuseoroApi, workspaceId: string) {
  const [approvals, quotePage, importPage] = await Promise.all([
    api.listApprovals(workspaceId),
    api.listQuotes(workspaceId),
    api.listProcurementImports(workspaceId),
  ]);
  const latest =
    approvals.items.find((item) => item.decision === "APPROVED") ??
    approvals.items[0];
  return {
    approval: latest ? await api.getApproval(latest.id) : null,
    quotes: quotePage.items,
    imports: importPage.items,
  };
}

export function QuotePanel({ api, user, workspace, onWorkspaceChange }: QuotePanelProps) {
  const [approval, setApproval] = useState<Approval | null>(null);
  const [quotes, setQuotes] = useState<Quote[]>([]);
  const [imports, setImports] = useState<ProcurementImport[]>([]);
  const [selectedQuoteId, setSelectedQuoteId] = useState<string | null>(null);
  const [vendorName, setVendorName] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [reloadRequest, setReloadRequest] = useState(0);

  function applyReview(review: Awaited<ReturnType<typeof loadQuoteReview>>) {
    setApproval(review.approval);
    setQuotes(review.quotes);
    setImports(review.imports);
    const firstUsable = review.quotes.find(
      (quote) => quoteBlockerReasons(quote).length === 0,
    );
    setSelectedQuoteId((current) =>
      current && review.quotes.some((quote) => quote.id === current)
        ? current
        : firstUsable?.id ?? null,
    );
  }

  async function reload() {
    const review = await loadQuoteReview(api, workspace.id);
    applyReview(review);
  }

  useEffect(() => {
    let active = true;
    void loadQuoteReview(api, workspace.id)
      .then((review) => {
        if (!active) return;
        setLoadError("");
        applyReview(review);
      })
      .catch((error: unknown) => {
        if (active) {
          setLoadError(
            error instanceof Error ? error.message : "견적을 불러오지 못했습니다.",
          );
        }
      })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [api, reloadRequest, workspace.id]);

  function retryLoad() {
    setLoading(true);
    setLoadError("");
    setReloadRequest((value) => value + 1);
  }

  const selected = quotes.find((quote) => quote.id === selectedQuoteId) ?? null;

  async function composeImport(item: ProcurementImport) {
    if (!canPerformAction(user, workspace.status, "ADD_QUOTE")) return;
    const result = await api.composeProcurementImport(
      item.import_id,
      workspace.row_version,
    );
    onWorkspaceChange({
      ...workspace,
      status: result.state,
      row_version: result.row_version,
    });
    setMessage("견적서를 비교했습니다.");
    await reload();
  }

  async function resolveMapping(
    item: ProcurementImport,
    mapping: Record<string, string>,
    remember: boolean,
  ) {
    if (!canPerformAction(user, workspace.status, "ADD_QUOTE")) return;
    const source = await api.getSource(item.source_id);
    await api.updateSourceMapping(
      item.source_id,
      {
        role: "VENDOR_QUOTE",
        mapping,
        remember_template: remember,
        vendor_scope: source.data.vendor_scope || item.vendor_name,
      },
      source.data.row_version,
    );
    const command = await api.parseSource(item.source_id);
    await waitForProcurementJob(api, command.job_id);
    await reload();
    setMessage("열 연결을 저장하고 견적 파일을 다시 읽었습니다.");
  }

  async function addQuote() {
    if (!approval || !file || !vendorName.trim() || busy) {
      setMessage("업체 이름과 견적 파일을 골라 주세요.");
      return;
    }
    setBusy(true);
    try {
      const upload = await api.uploadSources(workspace.id, {
        files: [file],
        role: "VENDOR_QUOTE",
        vendorScope: vendorName.trim(),
        procurementKind: "QUOTE",
        targetRevisionId: approval.revision_id,
        reason: "업체 견적서 비교",
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
      setFile(null);
      setVendorName("");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "견적서를 비교하지 못했습니다.");
    } finally {
      setBusy(false);
    }
  }

  async function selectQuote(quote: Quote) {
    if (!approval || busy) return;
    setBusy(true);
    setSelectedQuoteId(quote.id);
    try {
      const result = await api.createOrder(
        workspace.id,
        { approval_revision_id: approval.revision_id, quote_id: quote.id, reason: "선택한 견적으로 발주 준비", allocations: null, advanced_split_enabled: false },
        workspace.row_version,
      );
      if (result.status !== "READY") {
        setMessage("발주 행 연결을 다시 확인해 주세요.");
        return;
      }
      onWorkspaceChange({ ...workspace, status: result.state, row_version: result.row_version });
      setMessage("이 견적을 사용합니다. 발주파일을 받을 준비가 되었습니다.");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "견적을 선택하지 못했습니다.");
    } finally {
      setBusy(false);
    }
  }

  if (loading) return <p className="loading-state" role="status">승인 예산을 불러오고 있습니다…</p>;
  if (loadError) return <div className="message message-error" role="alert"><p>{loadError}</p><button className="button button-secondary" onClick={retryLoad} type="button">다시 불러오기</button></div>;
  if (!approval) return <div className="calm-placeholder"><h3>견적 비교</h3><p>승인된 목록을 찾지 못했습니다. 승인 요청 상태를 확인해 주세요.</p></div>;
  return (
    <div className="procurement-panel quote-panel">
      <div className="section-intro"><div><p className="eyebrow">승인된 목록을 기준으로 비교해요</p><h3>업체 견적 비교</h3><p>가격과 품절·누락을 함께 보고 선택합니다.</p></div></div>
      <BudgetStrip budgetWon={approval.payload.budget_won} quoteWon={selected?.total_won ?? null} />
      {canPerformAction(user, workspace.status, "ADD_QUOTE") ? (
        <div className="quote-upload-card">
          <label>업체 이름<input value={vendorName} onChange={(event) => setVendorName(event.target.value)} /></label>
          <label>견적 파일<input accept={PROCUREMENT_FILE_ACCEPT} type="file" onChange={(event) => setFile(event.target.files?.[0] ?? null)} /></label>
          <button className="button button-secondary" data-major-action="COMPARE_QUOTE" disabled={busy} onClick={() => void addQuote()} type="button">견적서 비교하기</button>
          <p className="field-note">CSV·엑셀 파일은 서버에서 표로 읽습니다. PDF·문서 파일도 원본을 보관하며, 표 변환이 필요하면 안내합니다.</p>
        </div>
      ) : null}
      <ProcurementImportStatus
        busy={busy}
        canMutate={canPerformAction(user, workspace.status, "ADD_QUOTE")}
        imports={imports}
        kind="QUOTE"
        onMap={resolveMapping}
        onCompose={(item) => {
          if (busy) return;
          setBusy(true);
          void composeImport(item)
            .catch((error: unknown) => setMessage(error instanceof Error ? error.message : "견적서를 반영하지 못했습니다."))
            .finally(() => setBusy(false));
        }}
      />
      <div className="quote-grid" aria-label="업체 견적 목록">
        {quotes.map((quote) => {
          const blockers = quoteBlockerReasons(quote);
          const canSelect = canPerformAction(user, workspace.status, "SELECT_QUOTE");
          const blockerId = `quote-blockers-${quote.id}`;
          return (
            <article className={`quote-card ${selectedQuoteId === quote.id ? "quote-selected" : ""}`} key={quote.id}>
              <div className="quote-card-heading"><h4>{quote.vendor_name}</h4><strong>{won.format(quote.total_won)}원</strong></div>
              <dl className="metric-list">
                <div><dt>할인</dt><dd>{won.format(quote.discount_won)}원</dd></div>
                <div><dt>품절</dt><dd>{quote.out_of_stock_count}건</dd></div>
                <div><dt>가격 누락</dt><dd>{quote.missing_price_count}건</dd></div>
                <div><dt>목록 불일치</dt><dd>{quote.list_mismatch_count + quote.unmatched_count}건</dd></div>
              </dl>
              {quote.budget_overrun_won > 0 ? <p className="overage-note"><strong>{won.format(quote.budget_overrun_won)}원 초과</strong> · 목록을 자동으로 줄이지 않습니다. 수량·가격을 조정하거나 다시 승인받아 주세요.</p> : null}
              {blockers.length ? <div className="field-note" id={blockerId}>{blockers.map((reason) => <p key={reason}>{reason}</p>)}</div> : null}
              {canSelect ? <button aria-describedby={blockers.length ? blockerId : undefined} className="button button-primary button-wide" data-major-action="SELECT_QUOTE" disabled={busy || blockers.length > 0} onClick={() => void selectQuote(quote)} type="button">이 견적 사용</button> : null}
            </article>
          );
        })}
      </div>
      {!quotes.length ? <p className="empty-state">아직 비교한 견적이 없습니다.</p> : null}
      {message ? <p className="live-status" role="status">{message}</p> : null}
    </div>
  );
}
