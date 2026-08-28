import { type KeyboardEvent, useEffect, useMemo, useState } from "react";

import type { Candidate, SuseoroApi, User, Workspace } from "../../api/client";
import { CandidateRow } from "./CandidateRow";

interface CandidatePanelProps {
  api: SuseoroApi;
  user: User;
  workspace: Workspace;
}

const OUTCOMES = ["NEEDS_REVIEW", "CANDIDATE", "EXCLUDED"] as const;
type Outcome = (typeof OUTCOMES)[number];

const OUTCOME_COPY: Record<Outcome, string> = {
  NEEDS_REVIEW: "확인 필요",
  CANDIDATE: "수서 후보",
  EXCLUDED: "제외된 책",
};

function matchesSearch(candidate: Candidate, search: string): boolean {
  const needle = search.trim().toLocaleLowerCase("ko-KR");
  if (!needle) return true;
  return [candidate.title, candidate.isbn13 ?? "", ...candidate.authors]
    .join(" ")
    .toLocaleLowerCase("ko-KR")
    .includes(needle);
}

export function CandidatePanel({ api, user, workspace }: CandidatePanelProps) {
  const [activeOutcome, setActiveOutcome] = useState<Outcome>("NEEDS_REVIEW");
  const [itemsByOutcome, setItemsByOutcome] = useState<Record<Outcome, Candidate[]>>({
    NEEDS_REVIEW: [],
    CANDIDATE: [],
    EXCLUDED: [],
  });
  const [search, setSearch] = useState("");
  const [announcement, setAnnouncement] = useState("");
  const [loading, setLoading] = useState(true);
  const [requestingApproval, setRequestingApproval] = useState(false);
  const [budgetWon, setBudgetWon] = useState("");

  useEffect(() => {
    let active = true;
    void Promise.all(
      OUTCOMES.map(async (outcome) => [
        outcome,
        (await api.listCandidates(workspace.id, { outcome })).items,
      ] as const),
    )
      .then((pages) => {
        if (!active) return;
        setItemsByOutcome(Object.fromEntries(pages) as Record<Outcome, Candidate[]>);
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
    return () => {
      active = false;
    };
  }, [api, workspace.id]);

  const visibleItems = useMemo(
    () => itemsByOutcome[activeOutcome].filter((candidate) => matchesSearch(candidate, search)),
    [activeOutcome, itemsByOutcome, search],
  );
  const expectedTotal = useMemo(
    () =>
      itemsByOutcome.CANDIDATE.reduce(
        (total, item) => total + (item.unit_price ?? 0) * item.quantity,
        0,
      ),
    [itemsByOutcome.CANDIDATE],
  );

  function moveResolvedCandidate(
    candidate: Candidate,
    outcome: "CANDIDATE" | "EXCLUDED",
  ) {
    setItemsByOutcome((current) => ({
      ...current,
      NEEDS_REVIEW: current.NEEDS_REVIEW.filter((item) => item.id !== candidate.id),
      [outcome]: [...current[outcome], candidate],
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

  async function restore(candidate: Candidate) {
    try {
      await api.updateCandidate(
        candidate.id,
        {
          workspace_id: workspace.id,
          changes: { outcome: "CANDIDATE" },
          reason: "제외 되돌리기",
        },
        candidate.row_version,
      );
      setItemsByOutcome((current) => ({
        ...current,
        EXCLUDED: current.EXCLUDED.filter((item) => item.id !== candidate.id),
        CANDIDATE: [...current.CANDIDATE, { ...candidate, outcome: "CANDIDATE" }],
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
    if (itemsByOutcome.NEEDS_REVIEW.length > 0 || approvedBudget <= 0) return;
    setRequestingApproval(true);
    try {
      await api.requestApproval(
        workspace.id,
        { budget_won: approvedBudget, reason: "후보 검토 완료" },
        workspace.row_version,
      );
      setAnnouncement("승인을 요청했습니다. 검토 담당자에게 전달했습니다.");
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
            {OUTCOME_COPY[outcome]} {itemsByOutcome[outcome].length}
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
                <button
                  aria-label={`${candidate.title} 제외 되돌리기`}
                  className="button button-secondary"
                  onClick={() => void restore(candidate)}
                  type="button"
                >
                  제외 되돌리기
                </button>
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
                  {activeOutcome === "NEEDS_REVIEW" ? <th scope="col">판정</th> : null}
                </tr>
              </thead>
              <tbody>
                {visibleItems.map((candidate) => (
                  <CandidateRow
                    announce={setAnnouncement}
                    api={api}
                    allowDecision={activeOutcome === "NEEDS_REVIEW"}
                    candidate={candidate}
                    key={candidate.id}
                    onDecided={moveResolvedCandidate}
                    user={user}
                    workspaceId={workspace.id}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <div aria-atomic="true" aria-live="polite" className="live-status" role="status">
        {announcement}
      </div>
      <div className="approval-action-row">
        <div className="approval-summary">
          <p>
            예상 금액 <strong>{expectedTotal.toLocaleString("ko-KR")}원</strong>
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
          {itemsByOutcome.NEEDS_REVIEW.length > 0 ? (
            <p className="field-note">확인 필요 {itemsByOutcome.NEEDS_REVIEW.length}권의 판정을 먼저 마쳐 주세요.</p>
          ) : null}
        </div>
        <button
          className="button button-primary"
          disabled={
            requestingApproval ||
            itemsByOutcome.NEEDS_REVIEW.length > 0 ||
            Number(budgetWon) <= 0
          }
          onClick={() => void requestApproval()}
          type="button"
        >
          {requestingApproval ? "승인을 요청하는 중…" : "후보 확정하고 승인 요청"}
        </button>
      </div>
    </div>
  );
}
