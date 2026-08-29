import { type KeyboardEvent, useEffect, useMemo, useRef, useState } from "react";

import type { Candidate, SuseoroApi, User, Workspace } from "../../api/client";
import type { components } from "../../api/types";
import { canOperate } from "../workspaces/workflowPolicy";
import { CandidateRow } from "./CandidateRow";

type CandidateSummary = components["schemas"]["CandidateSummary"];
type CandidatePage = components["schemas"]["CandidatePage"];

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

function candidateOutcome(candidate: Candidate): Outcome | null {
  return OUTCOMES.find((outcome) => outcome === candidate.outcome) ?? null;
}

function replaceCandidateInSummary(
  summary: CandidateSummary,
  previous: Candidate,
  replacement: Candidate,
): CandidateSummary {
  const previousOutcome = candidateOutcome(previous);
  const replacementOutcome = candidateOutcome(replacement);
  if (!previousOutcome || !replacementOutcome) return summary;
  const previousAmount =
    previousOutcome === "CANDIDATE" ? candidateAmount(previous) : 0;
  const replacementAmount =
    replacementOutcome === "CANDIDATE" ? candidateAmount(replacement) : 0;
  return {
    ...summary,
    candidate_count: Math.max(
      0,
      summary.candidate_count -
        (previousOutcome === "CANDIDATE" ? 1 : 0) +
        (replacementOutcome === "CANDIDATE" ? 1 : 0),
    ),
    needs_review_count: Math.max(
      0,
      summary.needs_review_count -
        (previousOutcome === "NEEDS_REVIEW" ? 1 : 0) +
        (replacementOutcome === "NEEDS_REVIEW" ? 1 : 0),
    ),
    excluded_count: Math.max(
      0,
      summary.excluded_count -
        (previousOutcome === "EXCLUDED" ? 1 : 0) +
        (replacementOutcome === "EXCLUDED" ? 1 : 0),
    ),
    unresolved_count: Math.max(
      0,
      summary.unresolved_count -
        (previousOutcome === "NEEDS_REVIEW" ? 1 : 0) +
        (replacementOutcome === "NEEDS_REVIEW" ? 1 : 0),
    ),
    expected_total_won: Math.max(
      0,
      summary.expected_total_won - previousAmount + replacementAmount,
    ),
  };
}

function sameSummary(
  left: CandidateSummary,
  right: CandidateSummary,
): boolean {
  return (
    left.total_count === right.total_count &&
    left.candidate_count === right.candidate_count &&
    left.needs_review_count === right.needs_review_count &&
    left.excluded_count === right.excluded_count &&
    left.unresolved_count === right.unresolved_count &&
    left.expected_total_won === right.expected_total_won
  );
}

function summaryFingerprint(summary: CandidateSummary): string {
  return [
    summary.total_count,
    summary.candidate_count,
    summary.needs_review_count,
    summary.excluded_count,
    summary.unresolved_count,
    summary.expected_total_won,
  ].join(":");
}

async function loadCoherentCandidatePages(
  api: SuseoroApi,
  workspaceId: string,
  search: string,
): Promise<ReadonlyArray<readonly [Outcome, CandidatePage]>> {
  const pages = new Map<Outcome, CandidatePage>();
  let outcomesToRead = [...OUTCOMES];
  for (let attempt = 0; attempt < 8; attempt += 1) {
    const refreshed = await Promise.all(
      outcomesToRead.map(async (outcome) => [
        outcome,
        await api.listCandidates(workspaceId, {
          outcome,
          search: search || undefined,
          limit: 100,
        }),
      ] as const),
    );
    for (const [outcome, page] of refreshed) pages.set(outcome, page);
    if (pages.size !== OUTCOMES.length) {
      outcomesToRead = OUTCOMES.filter((outcome) => !pages.has(outcome));
      continue;
    }

    const latestRevision = Math.max(
      ...OUTCOMES.map((outcome) => pages.get(outcome)!.workspace_revision),
    );
    const summaryFrequency = new Map<string, number>();
    for (const outcome of OUTCOMES) {
      const page = pages.get(outcome)!;
      if (page.workspace_revision !== latestRevision) continue;
      const fingerprint = summaryFingerprint(page.summary);
      summaryFrequency.set(fingerprint, (summaryFrequency.get(fingerprint) ?? 0) + 1);
    }
    const [authoritativeSummary] = [...summaryFrequency.entries()].sort(
      ([leftKey, leftCount], [rightKey, rightCount]) =>
        rightCount - leftCount || leftKey.localeCompare(rightKey),
    )[0];
    outcomesToRead = OUTCOMES.filter((outcome) => {
      const page = pages.get(outcome)!;
      return (
        page.workspace_revision !== latestRevision ||
        summaryFingerprint(page.summary) !== authoritativeSummary
      );
    });
    if (outcomesToRead.length === 0) {
      return OUTCOMES.map((outcome) => [outcome, pages.get(outcome)!] as const);
    }
  }
  throw new Error("후보 목록이 계속 변경되고 있습니다. 잠시 후 다시 확인해 주세요.");
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
  const [mutationRefresh, setMutationRefresh] = useState(0);
  const [acceptedSnapshot, setAcceptedSnapshot] = useState<{
    identity: string;
    revision: number;
  } | null>(null);
  const queryGenerationRef = useRef(0);
  const mutationEpochRef = useRef(0);
  const mutationInFlightRef = useRef(0);
  const snapshotRevisionRef = useRef<number | null>(null);
  const hasLoadedSnapshotRef = useRef(false);
  const authoritativeCandidatesRef = useRef(new Map<string, Candidate>());
  const operator = canOperate(user);
  const snapshotIdentity = `${workspace.id}\u0000${search}\u0000${mutationRefresh}`;
  const snapshotRevision =
    acceptedSnapshot?.identity === snapshotIdentity
      ? acceptedSnapshot.revision
      : null;

  useEffect(() => {
    authoritativeCandidatesRef.current.clear();
    snapshotRevisionRef.current = null;
    hasLoadedSnapshotRef.current = false;
  }, [workspace.id]);

  function reconcileCandidate(serverCandidate: Candidate): Candidate {
    const committed = authoritativeCandidatesRef.current.get(serverCandidate.id);
    if (!committed || serverCandidate.row_version > committed.row_version) {
      authoritativeCandidatesRef.current.set(serverCandidate.id, serverCandidate);
      return serverCandidate;
    }
    return committed;
  }

  useEffect(() => {
    let active = true;
    const generation = ++queryGenerationRef.current;
    const mutationEpoch = mutationEpochRef.current;
    const mutationWasInFlight = mutationInFlightRef.current > 0;
    snapshotRevisionRef.current = null;
    const timer = window.setTimeout(() => {
      if (!hasLoadedSnapshotRef.current) setLoading(true);
      void loadCoherentCandidatePages(api, workspace.id, search.trim())
        .then((results) => {
          if (
            !active ||
            queryGenerationRef.current !== generation ||
            mutationEpochRef.current !== mutationEpoch ||
            mutationWasInFlight ||
            mutationInFlightRef.current > 0
          ) return;
          const pageState = (outcome: Outcome): PageState => {
            const page = results.find(
              ([resultOutcome]) => resultOutcome === outcome,
            )?.[1];
            return {
              items: [],
              nextCursor: page?.next_cursor ?? null,
              totalCount: page?.total_count ?? 0,
            };
          };
          const next: Record<Outcome, PageState> = {
            NEEDS_REVIEW: pageState("NEEDS_REVIEW"),
            CANDIDATE: pageState("CANDIDATE"),
            EXCLUDED: pageState("EXCLUDED"),
          };
          const canonical = new Map<string, Candidate>();
          for (const outcome of OUTCOMES) {
            const page = results.find(([resultOutcome]) => resultOutcome === outcome)?.[1];
            for (const candidate of page?.items ?? []) {
              const existing = canonical.get(candidate.id);
              if (!existing || candidate.row_version >= existing.row_version) {
                canonical.set(candidate.id, candidate);
              }
            }
          }
          const summaryBasis = results[0]?.[1].summary ?? EMPTY_SUMMARY;
          const summaryBasisCandidates = new Map<string, Candidate>();
          for (const [, page] of results) {
            if (!sameSummary(page.summary, summaryBasis)) continue;
            for (const candidate of page.items) {
              const existing = summaryBasisCandidates.get(candidate.id);
              if (!existing || candidate.row_version >= existing.row_version) {
                summaryBasisCandidates.set(candidate.id, candidate);
              }
            }
          }
          let reconciledSummary = summaryBasis;
          for (const serverCandidate of canonical.values()) {
            const candidate = reconcileCandidate(serverCandidate);
            const summaryBasisCandidate = summaryBasisCandidates.get(candidate.id);
            if (summaryBasisCandidate) {
              reconciledSummary = replaceCandidateInSummary(
                reconciledSummary,
                summaryBasisCandidate,
                candidate,
              );
            } else if (candidate !== serverCandidate) {
              reconciledSummary = replaceCandidateInSummary(
                reconciledSummary,
                serverCandidate,
                candidate,
              );
            }
            const outcome = candidateOutcome(candidate);
            if (outcome && matchesSearch(candidate, search)) {
              next[outcome].items.push(candidate);
            }
          }
          for (const outcome of OUTCOMES) {
            next[outcome].totalCount = summaryCount(reconciledSummary, outcome);
          }
          const acceptedRevision = results[0]?.[1].workspace_revision ?? null;
          snapshotRevisionRef.current = acceptedRevision;
          hasLoadedSnapshotRef.current = true;
          if (acceptedRevision !== null) {
            setAcceptedSnapshot({
              identity: snapshotIdentity,
              revision: acceptedRevision,
            });
          }
          setPages(next);
          setSummary(reconciledSummary);
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
  }, [api, mutationRefresh, search, snapshotIdentity, workspace.id]);

  function beginMutation() {
    mutationEpochRef.current += 1;
    mutationInFlightRef.current += 1;
    snapshotRevisionRef.current = null;
    setAcceptedSnapshot(null);
  }

  function finishMutation() {
    mutationInFlightRef.current = Math.max(0, mutationInFlightRef.current - 1);
    queryGenerationRef.current += 1;
    if (mutationInFlightRef.current === 0) {
      setMutationRefresh((current) => current + 1);
    }
  }

  const visibleItems = useMemo(
    () => pages[activeOutcome].items.filter((item) => matchesSearch(item, search)),
    [activeOutcome, pages, search],
  );

  function updateCandidate(updated: Candidate, previous: Candidate) {
    const oldAmount = previous.outcome === "CANDIDATE" ? candidateAmount(previous) : 0;
    const newAmount = updated.outcome === "CANDIDATE" ? candidateAmount(updated) : 0;
    const previousOutcome = OUTCOMES.find((outcome) => outcome === previous.outcome);
    const updatedOutcome = OUTCOMES.find((outcome) => outcome === updated.outcome);
    if (updatedOutcome) authoritativeCandidatesRef.current.set(updated.id, updated);
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
  ) {
    const authoritativeOutcome: "CANDIDATE" | "EXCLUDED" =
      candidate.outcome === "EXCLUDED" ? "EXCLUDED" : "CANDIDATE";
    authoritativeCandidatesRef.current.set(candidate.id, candidate);
    setPages((current) => {
      const alreadyInTarget = current[authoritativeOutcome].items.some(
        (item) => item.id === candidate.id,
      );
      return {
        ...current,
        NEEDS_REVIEW: {
          ...current.NEEDS_REVIEW,
          totalCount: Math.max(0, current.NEEDS_REVIEW.totalCount - 1),
          items: current.NEEDS_REVIEW.items.filter(
            (item) => item.id !== candidate.id,
          ),
        },
        [authoritativeOutcome]: {
          ...current[authoritativeOutcome],
          totalCount:
            current[authoritativeOutcome].totalCount +
            (alreadyInTarget ? 0 : 1),
          items: [
            ...current[authoritativeOutcome].items.filter(
              (item) => item.id !== candidate.id,
            ),
            candidate,
          ],
        },
      };
    });
    setSummary((current) => ({
      ...current,
      needs_review_count: Math.max(0, current.needs_review_count - 1),
      unresolved_count: Math.max(0, current.unresolved_count - 1),
      candidate_count:
        current.candidate_count + (authoritativeOutcome === "CANDIDATE" ? 1 : 0),
      excluded_count:
        current.excluded_count + (authoritativeOutcome === "EXCLUDED" ? 1 : 0),
      expected_total_won:
        current.expected_total_won +
        (authoritativeOutcome === "CANDIDATE" ? candidateAmount(candidate) : 0),
    }));
    setAnnouncement(
      authoritativeOutcome === "CANDIDATE"
        ? "수서 후보로 옮겼습니다."
        : "제외된 책으로 옮겼습니다.",
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
    const requestedRevision = snapshotRevisionRef.current;
    if (!cursor || requestedRevision === null) return;
    const generation = queryGenerationRef.current;
    const mutationEpoch = mutationEpochRef.current;
    const mutationWasInFlight = mutationInFlightRef.current > 0;
    const outcome = activeOutcome;
    const requestedSearch = search.trim();
    setLoadingMore(true);
    try {
      const page = await api.listCandidates(workspace.id, {
        outcome: activeOutcome,
        search: search.trim() || undefined,
        cursor,
        limit: 100,
      });
      if (
        queryGenerationRef.current !== generation ||
        mutationEpochRef.current !== mutationEpoch ||
        mutationWasInFlight ||
        mutationInFlightRef.current > 0 ||
        activeOutcome !== outcome ||
        search.trim() !== requestedSearch
      ) return;
      if (
        page.workspace_revision !== requestedRevision ||
        !sameSummary(page.summary, summary)
      ) {
        snapshotRevisionRef.current = null;
        setAcceptedSnapshot(null);
        queryGenerationRef.current += 1;
        setLoading(true);
        setAnnouncement("후보 목록이 변경되어 최신 내용을 다시 불러옵니다.");
        setMutationRefresh((current) => current + 1);
        return;
      }
      const canonical = new Map<string, Candidate>();
      for (const candidate of page.items) {
        const existing = canonical.get(candidate.id);
        if (!existing || candidate.row_version >= existing.row_version) {
          canonical.set(candidate.id, candidate);
        }
      }
      const reconciled = new Map<string, Candidate>();
      let reconciledSummary = page.summary;
      for (const serverCandidate of canonical.values()) {
        const committed = authoritativeCandidatesRef.current.get(serverCandidate.id);
        const candidate = reconcileCandidate(serverCandidate);
        if (
          committed &&
          serverCandidate.row_version > committed.row_version &&
          sameSummary(page.summary, summary)
        ) {
          reconciledSummary = replaceCandidateInSummary(
            reconciledSummary,
            committed,
            serverCandidate,
          );
        } else if (candidate !== serverCandidate) {
          reconciledSummary = replaceCandidateInSummary(
            reconciledSummary,
            serverCandidate,
            candidate,
          );
        }
        reconciled.set(candidate.id, candidate);
      }
      setPages((current) => {
        const incomingIds = new Set(canonical.keys());
        const next = Object.fromEntries(
          OUTCOMES.map((pageOutcome) => [
            pageOutcome,
            {
              ...current[pageOutcome],
              items: current[pageOutcome].items.filter(
                (candidate) => !incomingIds.has(candidate.id),
              ),
              nextCursor:
                pageOutcome === outcome
                  ? page.next_cursor
                  : current[pageOutcome].nextCursor,
              totalCount: summaryCount(reconciledSummary, pageOutcome),
            },
          ]),
        ) as Record<Outcome, PageState>;
        for (const candidate of reconciled.values()) {
          const candidatePage = candidateOutcome(candidate);
          if (candidatePage && matchesSearch(candidate, requestedSearch)) {
            next[candidatePage].items.push(candidate);
          }
        }
        return next;
      });
      setSummary(reconciledSummary);
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
      beginMutation();
      try {
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
          reason: null,
        };
        const restoredOutcome: "CANDIDATE" | "EXCLUDED" =
          restored.outcome === "EXCLUDED" ? "EXCLUDED" : "CANDIDATE";
        authoritativeCandidatesRef.current.set(restored.id, restored);
        updateCandidate(restored, candidate);
        setAnnouncement(
          restoredOutcome === "CANDIDATE"
            ? "수서 후보로 되돌렸습니다."
            : "서버의 현재 판정을 유지했습니다.",
        );
      } finally {
        finishMutation();
      }
    } catch (error) {
      setAnnouncement(
        error instanceof Error ? error.message : "되돌리지 못했습니다. 다시 시도해 주세요.",
      );
    }
  }

  async function requestApproval() {
    const approvedBudget = Number(budgetWon);
    if (
      snapshotRevisionRef.current === null ||
      summary.unresolved_count > 0 ||
      approvedBudget <= 0
    ) return;
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
                    onMutationFinished={finishMutation}
                    onMutationStarted={beginMutation}
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
              snapshotRevision === null ||
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
