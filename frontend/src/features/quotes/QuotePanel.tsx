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

  async function reload() {
    const review = await loadQuoteReview(api, workspace.id);
    setApproval(review.approval);
    setQuotes(review.quotes);
    setImports(review.imports);
    const firstUsable = review.quotes.find(
      (quote) => !quote.requires_reapproval && quote.needs_review_count === 0,
    );
    setSelectedQuoteId((current) =>
      current && review.quotes.some((quote) => quote.id === current)
        ? current
        : firstUsable?.id ?? null,
    );
  }

  useEffect(() => {
    let active = true;
    void loadQuoteReview(api, workspace.id)
      .then((review) => {
        if (!active) return;
        setApproval(review.approval);
        setQuotes(review.quotes);
        setImports(review.imports);
        const firstUsable = review.quotes.find(
          (quote) => !quote.requires_reapproval && quote.needs_review_count === 0,
        );
        setSelectedQuoteId(firstUsable?.id ?? null);
      })
      .catch((error: unknown) => {
        if (active) {
          setMessage(
            error instanceof Error ? error.message : "견적을 불러오지 못했습니다.",
          );
        }
      });
    return () => { active = false; };
  }, [api, workspace.id]);

  const selected = quotes.find((quote) => quote.id === selectedQuoteId) ?? null;

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
    setMessage("견적서를 비교했습니다.");
    await reload();
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

  if (!approval) return <p className="loading-state" role="status">승인 예산을 불러오고 있습니다…</p>;
  return (
    <div className="procurement-panel quote-panel">
      <div className="section-intro"><div><p className="eyebrow">승인된 목록을 기준으로 비교해요</p><h3>업체 견적 비교</h3><p>가격과 품절·누락을 함께 보고 선택합니다.</p></div></div>
      <BudgetStrip budgetWon={approval.payload.budget_won} quoteWon={selected?.total_won ?? null} />
      {canPerformAction(user, workspace.status, "ADD_QUOTE") ? (
        <div className="quote-upload-card">
          <label>업체 이름<input value={vendorName} onChange={(event) => setVendorName(event.target.value)} /></label>
          <label>견적 파일<input accept={PROCUREMENT_FILE_ACCEPT} type="file" onChange={(event) => setFile(event.target.files?.[0] ?? null)} /></label>
          <button className="button button-secondary" disabled={busy} onClick={() => void addQuote()} type="button">견적서 비교하기</button>
          <p className="field-note">CSV·엑셀 파일은 서버에서 표로 읽습니다. PDF·문서 파일도 원본을 보관하며, 표 변환이 필요하면 안내합니다.</p>
        </div>
      ) : null}
      <ProcurementImportStatus
        busy={busy}
        imports={imports}
        kind="QUOTE"
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
          const usable = !quote.requires_reapproval && quote.needs_review_count === 0 && quote.unmatched_count === 0;
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
              {usable && canPerformAction(user, workspace.status, "SELECT_QUOTE") ? <button className="button button-primary button-wide" disabled={busy} onClick={() => void selectQuote(quote)} type="button">이 견적 사용</button> : <p className="field-note">가격 누락·불일치를 먼저 확인해 주세요.</p>}
            </article>
          );
        })}
      </div>
      {!quotes.length ? <p className="empty-state">아직 비교한 견적이 없습니다.</p> : null}
      {message ? <p className="live-status" role="status">{message}</p> : null}
    </div>
  );
}
