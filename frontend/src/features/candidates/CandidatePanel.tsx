import { type KeyboardEvent, useEffect, useMemo, useState } from "react";

import type { Candidate, SuseoroApi, User, Workspace } from "../../api/client";
import type { components } from "../../api/types";
import { canOperate } from "../workspaces/workflowPolicy";
import { CandidateRow } from "./CandidateRow";

type CandidateSummary = components["schemas"]["CandidateSummary"];

interface CandidatePanelProps {
  api: SuseoroApi;
  user: User;
  workspace: Workspace;
  onWorkspaceChange?: (workspace: Workspace) => void;
}

const OUTCOMES = ["NEEDS_REVIEW", "CANDIDATE", "EXCLUDED"] as const;
type Outcome = (typeof OUTCOMES)[number];

interface PageState {
  items: Candidate[];
  nextCursor: string | null;
  totalCount: number;
}

const OUTCOME_COPY: Record<Outcome, string> = {
  NEEDS_REVIEW: "확인 필요",
  CANDIDATE: "수서 후보",
  EXCLUDED: "제외된 책",
};

const EMPTY_PAGES: Record<Outcome, PageState> = {
  NEEDS_REVIEW: { items: [], nextCursor: null, totalCount: 0 },
  CANDIDATE: { items: [], nextCursor: null, totalCount: 0 },
  EXCLUDED: { items: [], nextCursor: null, totalCount: 0 },
};

const EMPTY_SUMMARY: CandidateSummary = {
  total_count: 0,
  candidate_count: 0,
  needs_review_count: 0,
  excluded_count: 0,
  unresolved_count: 0,
  expected_total_won: 0,
};

function matchesSearch(candidate: Candidate, search: string): boolean {
  const needle = search.trim().toLocaleLowerCase("ko-KR");
  if (!needle) return true;
  return [candidate.title, candidate.isbn13 ?? "", ...candidate.authors]
    .join(" ")
    .toLocaleLowerCase("ko-KR")
    .includes(needle);
}

function summaryCount(summary: CandidateSummary, outcome: Outcome): number {
  if (outcome === "CANDIDATE") return summary.candidate_count;
  if (outcome === "NEEDS_REVIEW") return summary.needs_review_count;
  return summary.excluded_count;
}

function candidateAmount(candidate: Candidate): number {
  return (candidate.unit_price ?? 0) * candidate.quantity;
}

export function CandidatePanel({
  api,
  user,
  workspace,
  onWorkspaceChange = () => undefined,
}: CandidatePanelProps) {
  const [activeOutcome, setActiveOutcome] = useState<Outcome>("NEEDS_REVIEW");
  const [pages, setPages] = useState<Record<Outcome, PageState>>(EMPTY_PAGES);
  const [summary, setSummary] = useState<CandidateSummary>(EMPTY_SUMMARY);
  const [search, setSearch] = useState("");
  const [announcement, setAnnouncement] = useState("");
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [requestingApproval, setRequestingApproval] = useState(false);
  const [budgetWon, setBudgetWon] = useState("");
  const operator = canOperate(user);

  useEffect(() => {
    let active = true;
    const timer = window.setTimeout(() => {
      void Promise.all(
        OUTCOMES.map(async (outcome) => [
          outcome,
          await api.listCandidates(workspace.id, {
            outcome,
            search: search.trim() || undefined,
            limit: 100,
          }),
        ] as const),
      )
        .then((results) => {
          if (!active) return;
          const next = Object.fromEntries(
            results.map(([outcome, page]) => [
              outcome,
              {
                items: page.items,
                nextCursor: page.next_cursor,
                totalCount: page.total_count,
              },
            ]),
          ) as Record<Outcome, PageState>;
          setPages(next);
          setSummary(results[0]?.[1].summary ?? EMPTY_SUMMARY);
        })
        .catch((error: unknown) => {
          if (active) {
            setAnnouncement(
              error instanceof Error
                ? error.message
                : "후보를 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.",
            );
          }
        })
        .finally(() => {
          if (active) setLoading(false);
        });
    }, search ? 200 : 0);
    return () => {
      active = false;
      window.clearTimeout(timer);
    };
  }, [api, search, workspace.id]);

  const visibleItems = useMemo(
    () => pages[activeOutcome].items.filter((item) => matchesSearch(item, search)),
    [activeOutcome, pages, search],
  );

  function updateCandidate(updated: Candidate, previous: Candidate) {
    const oldAmount = previous.outcome === "CANDIDATE" ? candidateAmount(previous) : 0;
    const newAmount = updated.outcome === "CANDIDATE" ? candidateAmount(updated) : 0;
    const previousOutcome = OUTCOMES.find((outcome) => outcome === previous.outcome);
    const updatedOutcome = OUTCOMES.find((outcome) => outcome === updated.outcome);
    setPages((current) => {
      const next = { ...current };
      for (const outcome of OUTCOMES) {
        const hadCandidate = current[outcome].items.some((item) => item.id === updated.id);
        const movingAway = previousOutcome === outcome && updatedOutcome !== outcome;
        const movingHere = updatedOutcome === outcome && previousOutcome !== outcome;
        next[outcome] = {
          ...current[outcome],
          totalCount:
            current[outcome].totalCount + (movingHere ? 1 : 0) - (movingAway ? 1 : 0),
          items: movingAway
            ? current[outcome].items.filter((item) => item.id !== updated.id)
            : movingHere && !hadCandidate
              ? [...current[outcome].items, updated]
              : current[outcome].items.map((item) =>
                  item.id === updated.id ? updated : item,
                ),
        };
      }
      return next;
    });
    setSummary((current) => ({
      ...current,
      candidate_count:
        current.candidate_count +
        (updatedOutcome === "CANDIDATE" ? 1 : 0) -
        (previousOutcome === "CANDIDATE" ? 1 : 0),
      needs_review_count:
        current.needs_review_count +
        (updatedOutcome === "NEEDS_REVIEW" ? 1 : 0) -
        (previousOutcome === "NEEDS_REVIEW" ? 1 : 0),
      excluded_count:
        current.excluded_count +
        (updatedOutcome === "EXCLUDED" ? 1 : 0) -
        (previousOutcome === "EXCLUDED" ? 1 : 0),
      unresolved_count:
        current.unresolved_count +
        (updatedOutcome === "NEEDS_REVIEW" ? 1 : 0) -
        (previousOutcome === "NEEDS_REVIEW" ? 1 : 0),
      expected_total_won: current.expected_total_won - oldAmount + newAmount,
    }));
  }

  function moveResolvedCandidate(
    candidate: Candidate,
    outcome: "CANDIDATE" | "EXCLUDED",
  ) {
    setPages((current) => ({
      ...current,
      NEEDS_REVIEW: {
        ...current.NEEDS_REVIEW,
        totalCount: Math.max(0, current.NEEDS_REVIEW.totalCount - 1),
        items: current.NEEDS_REVIEW.items.filter((item) => item.id !== candidate.id),
      },
      [outcome]: {
        ...current[outcome],
        totalCount: current[outcome].totalCount + 1,
        items: [...current[outcome].items, candidate],
      },
    }));
    setSummary((current) => ({
      ...current,
      needs_review_count: Math.max(0, current.needs_review_count - 1),
      unresolved_count: Math.max(0, current.unresolved_count - 1),
      candidate_count:
        current.candidate_count + (outcome === "CANDIDATE" ? 1 : 0),
      excluded_count: current.excluded_count + (outcome === "EXCLUDED" ? 1 : 0),
      expected_total_won:
        current.expected_total_won + (outcome === "CANDIDATE" ? candidateAmount(candidate) : 0),
    }));
    setAnnouncement(
      outcome === "CANDIDATE" ? "수서 후보로 옮겼습니다." : "제외된 책으로 옮겼습니다.",
    );
  }

  function moveTabFocus(event: KeyboardEvent<HTMLButtonElement>, outcome: Outcome) {
    const currentIndex = OUTCOMES.indexOf(outcome);
    let nextIndex: number | null = null;
    if (event.key === "ArrowRight") nextIndex = (currentIndex + 1) % OUTCOMES.length;
    if (event.key === "ArrowLeft") {
      nextIndex = (currentIndex - 1 + OUTCOMES.length) % OUTCOMES.length;
    }
    if (event.key === "Home") nextIndex = 0;
    if (event.key === "End") nextIndex = OUTCOMES.length - 1;
    if (nextIndex === null) return;
    event.preventDefault();
    const next = OUTCOMES[nextIndex];
    setActiveOutcome(next);
    window.requestAnimationFrame(() => {
      document.getElementById(`candidate-tab-${next}`)?.focus();
    });
  }

  async function loadMore() {
    const cursor = pages[activeOutcome].nextCursor;
    if (!cursor) return;
    setLoadingMore(true);
    try {
      const page = await api.listCandidates(workspace.id, {
        outcome: activeOutcome,
        search: search.trim() || undefined,
        cursor,
        limit: 100,
      });
      setPages((current) => ({
        ...current,
        [activeOutcome]: {
          items: [...current[activeOutcome].items, ...page.items],
          nextCursor: page.next_cursor,
          totalCount: page.total_count,
        },
      }));
      setSummary(page.summary);
    } catch (error) {
      setAnnouncement(
        error instanceof Error
          ? error.message
          : "다음 후보를 불러오지 못했습니다. 다시 시도해 주세요.",
      );
    } finally {
      setLoadingMore(false);
    }
  }

  async function restore(candidate: Candidate) {
    try {
      await api.lockCandidate(candidate.id, workspace.id);
      const updated = await api.updateCandidate(
        candidate.id,
        {
          workspace_id: workspace.id,
          changes: { outcome: "CANDIDATE" },
          reason: "제외 되돌리기",
        },
        candidate.row_version,
      );
      const restored: Candidate = {
        ...candidate,
        ...updated.data,
        outcome: "CANDIDATE",
        reason: null,
      };
      setPages((current) => ({
        ...current,
        EXCLUDED: {
          ...current.EXCLUDED,
          totalCount: Math.max(0, current.EXCLUDED.totalCount - 1),
          items: current.EXCLUDED.items.filter((item) => item.id !== candidate.id),
        },
        CANDIDATE: {
          ...current.CANDIDATE,
          totalCount: current.CANDIDATE.totalCount + 1,
          items: [...current.CANDIDATE.items, restored],
        },
      }));
      setSummary((current) => ({
        ...current,
        candidate_count: current.candidate_count + 1,
        excluded_count: Math.max(0, current.excluded_count - 1),
        expected_total_won: current.expected_total_won + candidateAmount(restored),
      }));
      setAnnouncement("수서 후보로 되돌렸습니다.");
    } catch (error) {
      setAnnouncement(
        error instanceof Error ? error.message : "되돌리지 못했습니다. 다시 시도해 주세요.",
      );
    }
  }

  async function requestApproval() {
    const approvedBudget = Number(budgetWon);
    if (summary.unresolved_count > 0 || approvedBudget <= 0) return;
    setRequestingApproval(true);
    try {
      const transition = await api.requestApproval(
        workspace.id,
        { budget_won: approvedBudget, reason: "후보 검토 완료" },
        workspace.row_version,
      );
      onWorkspaceChange({
        ...workspace,
        status: transition.state,
        row_version: transition.row_version,
      });
      setAnnouncement("승인을 요청했습니다. 검토 담당자에게 전달했습니다.");
      try {
        const current = await api.getWorkspace(workspace.id);
        onWorkspaceChange(current.data);
      } catch {
        // The mutation response is authoritative; a failed refresh must not
        // turn a committed approval request into a retryable user action.
      }
    } catch (error) {
      setAnnouncement(
        error instanceof Error
          ? error.message
          : "승인을 요청하지 못했습니다. 잠시 후 다시 시도해 주세요.",
      );
    } finally {
      setRequestingApproval(false);
    }
  }

  return (
    <div className="candidate-panel">
      <div className="section-intro">
        <div>
          <p className="eyebrow">판본과 수량을 차분히 살펴봐요</p>
          <h3>후보 확인</h3>
          <p>확인이 필요한 책을 먼저 모았습니다. 바꾼 값은 자리를 옮길 때 저장됩니다.</p>
        </div>
        <label className="search-field">
          <span>후보 필터</span>
          <input
            onChange={(event) => setSearch(event.target.value)}
            placeholder="제목, 저자, ISBN"
            type="search"
            value={search}
          />
        </label>
      </div>

      <div aria-label="후보 분류" className="tabs" role="tablist">
        {OUTCOMES.map((outcome) => (
          <button
            aria-controls="candidate-panel"
            aria-selected={activeOutcome === outcome}
            className="tab"
            id={`candidate-tab-${outcome}`}
            key={outcome}
            onClick={() => setActiveOutcome(outcome)}
            onKeyDown={(event) => moveTabFocus(event, outcome)}
            role="tab"
            tabIndex={activeOutcome === outcome ? 0 : -1}
            type="button"
          >
            {OUTCOME_COPY[outcome]} {summaryCount(summary, outcome)}
          </button>
        ))}
      </div>

      <section
        aria-labelledby={`candidate-tab-${activeOutcome}`}
        id="candidate-panel"
        role="tabpanel"
      >
        {loading ? (
          <p>후보를 불러오고 있습니다…</p>
        ) : visibleItems.length === 0 ? (
          <p className="empty-state">조건에 맞는 책이 없습니다.</p>
        ) : activeOutcome === "EXCLUDED" ? (
          <ul className="excluded-list">
            {visibleItems.map((candidate) => (
              <li key={candidate.id}>
                <div>
                  <strong>{candidate.title}</strong>
                  <p>
                    {candidate.reason === "EXACT_ISBN_MATCH"
                      ? "보유 장서와 ISBN이 정확히 일치합니다."
                      : candidate.reason === "LIBRARIAN_DECISION"
                        ? "사서가 내용을 확인하여 제외했습니다."
                        : candidate.reason ?? "제외 사유를 확인해 주세요."}
                  </p>
                </div>
                {operator ? (
                  <button
                    aria-label={`${candidate.title} 제외 되돌리기`}
                    className="button button-secondary"
                    onClick={() => void restore(candidate)}
                    type="button"
                  >
                    제외 되돌리기
                  </button>
                ) : null}
              </li>
            ))}
          </ul>
        ) : (
          <div className="table-scroll">
            <table aria-label={`${OUTCOME_COPY[activeOutcome]} 목록`} className="candidate-table">
              <thead>
                <tr>
                  <th scope="col">책</th>
                  <th scope="col">ISBN</th>
                  <th scope="col">수량</th>
                  <th scope="col">가격</th>
                  <th scope="col">저장 상태</th>
                  {activeOutcome === "NEEDS_REVIEW" && operator ? <th scope="col">판정</th> : null}
                </tr>
              </thead>
              <tbody>
                {visibleItems.map((candidate) => (
                  <CandidateRow
                    allowDecision={activeOutcome === "NEEDS_REVIEW" && operator}
                    announce={setAnnouncement}
                    api={api}
                    candidate={candidate}
                    editable={operator}
                    key={candidate.id}
                    onCandidateUpdated={updateCandidate}
                    onDecided={moveResolvedCandidate}
                    user={user}
                    workspaceId={workspace.id}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}
        {pages[activeOutcome].nextCursor ? (
          <button
            className="button button-secondary load-more"
            disabled={loadingMore}
            onClick={() => void loadMore()}
            type="button"
          >
            {loadingMore ? "후보를 더 불러오는 중…" : `${OUTCOME_COPY[activeOutcome]} 더 보기`}
          </button>
        ) : null}
      </section>

      <div aria-atomic="true" aria-live="polite" className="live-status" role="status">
        {announcement}
      </div>
      {operator ? (
        <div className="approval-action-row">
          <div className="approval-summary">
            <p>
              예상 금액 <strong>{summary.expected_total_won.toLocaleString("ko-KR")}원</strong>
            </p>
            <label>
              승인 예산
              <input
                inputMode="numeric"
                min={1}
                onChange={(event) => setBudgetWon(event.target.value)}
                type="number"
                value={budgetWon}
              />
            </label>
            {summary.unresolved_count > 0 ? (
              <p className="field-note">
                확인 필요 {summary.unresolved_count}권의 판정을 먼저 마쳐 주세요.
              </p>
            ) : null}
          </div>
          <button
            className="button button-primary"
            disabled={
              requestingApproval ||
              summary.unresolved_count > 0 ||
              Number(budgetWon) <= 0
            }
            onClick={() => void requestApproval()}
            type="button"
          >
            {requestingApproval ? "승인을 요청하는 중…" : "후보 확정하고 승인 요청"}
          </button>
        </div>
      ) : null}
    </div>
  );
}
