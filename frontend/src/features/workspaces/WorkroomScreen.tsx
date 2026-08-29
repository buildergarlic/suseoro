import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";

import type { Job, SuseoroApi, User, Workspace } from "../../api/client";
import { ApprovalPanel, ChangesRequestedNotice } from "../approvals/ApprovalPanel";
import { CandidatePanel } from "../candidates/CandidatePanel";
import { IngestionPanel } from "../ingestion/IngestionPanel";
import { OrderPanel } from "../orders/OrderPanel";
import { QuotePanel } from "../quotes/QuotePanel";
import { ReceivingPanel } from "../receiving/ReceivingPanel";
import { canOperate, workflowPolicy } from "./workflowPolicy";

interface WorkroomScreenProps {
  api: SuseoroApi;
  user: User;
}

const STAGE_TITLES = ["1. 후보 만들기", "2. 승인·발주", "3. 납품 검수"] as const;
const ACTIVE_JOB_STATES = new Set(["QUEUED", "RUNNING", "CANCEL_REQUESTED"]);
const RETRYABLE_JOB_STATES = new Set(["FAILED", "PARTIAL", "CANCELLED"]);

function completedSummary(stage: number): string {
  return stage === 0
    ? "추천자료와 보유 장서를 비교해 수서 후보를 만들었습니다."
    : stage === 1
      ? "후보 승인과 서점 발주를 마쳤습니다."
      : "도착한 책의 납품 검수까지 마쳐 이 작업을 완료했습니다.";
}

function CurrentStage({
  api,
  user,
  workspace,
  stage,
  onWorkspaceChange,
  analysisPollError,
  onRetryAnalysis,
  analysisJob,
  onRetryComparison,
  retryingComparison,
  analysisCanCorrectSources,
}: {
  api: SuseoroApi;
  user: User;
  workspace: Workspace;
  stage: number;
  onWorkspaceChange: (workspace: Workspace) => void;
  analysisPollError: string;
  onRetryAnalysis: () => void;
  analysisJob: Job | null;
  onRetryComparison: () => void;
  retryingComparison: boolean;
  analysisCanCorrectSources: boolean;
}) {
  if (stage === 0) {
    const mode = workflowPolicy(workspace.status).mode;
    if (mode === "CANDIDATES") {
      return (
        <>
          {workspace.status === "CHANGES_REQUESTED" ? (
            <ChangesRequestedNotice api={api} workspace={workspace} />
          ) : null}
          <CandidatePanel
            api={api}
            onWorkspaceChange={onWorkspaceChange}
            user={user}
            workspace={workspace}
          />
        </>
      );
    }
    if (mode === "ANALYZING") {
      if (analysisCanCorrectSources && canOperate(user)) {
        return (
          <IngestionPanel
            api={api}
            comparisonJob={analysisJob}
            key={workspace.id}
            onWorkspaceChange={onWorkspaceChange}
            workspace={workspace}
          />
        );
      }
      return (
        <div className="calm-placeholder" role="status">
          <h3>도서 비교 중</h3>
          <p>도서관 장서와 추천자료를 비교하고 있습니다.</p>
          <p>이 화면을 그대로 두어도 완료되면 후보가 열립니다.</p>
          {analysisJob && RETRYABLE_JOB_STATES.has(analysisJob.status) ? (
            <>
              <p role="alert">
                {analysisJob.error?.message ?? "도서 비교를 마치지 못했습니다. 다시 시도해 주세요."}
              </p>
              {canOperate(user) ? (
                <button
                  className="button button-secondary"
                  disabled={retryingComparison}
                  onClick={onRetryComparison}
                  type="button"
                >
                  {retryingComparison
                    ? "도서 비교를 다시 시작하는 중…"
                    : "도서 비교 다시 시도"}
                </button>
              ) : null}
            </>
          ) : null}
          {analysisPollError ? (
            <>
              <p role="alert">{analysisPollError}</p>
              <button
                className="button button-secondary"
                onClick={onRetryAnalysis}
                type="button"
              >
                비교 상태 다시 확인
              </button>
            </>
          ) : null}
        </div>
      );
    }
    return canOperate(user) ? (
      <IngestionPanel
        api={api}
        key={workspace.id}
        onWorkspaceChange={onWorkspaceChange}
        workspace={workspace}
      />
    ) : (
      <div className="calm-placeholder">
        <h3>추천자료 준비</h3>
        <p>담당자가 추천자료를 준비하고 있습니다.</p>
      </div>
    );
  }
  if (stage === 1) {
    if (workspace.status === "APPROVAL_PENDING") {
      return <ApprovalPanel api={api} onWorkspaceChange={onWorkspaceChange} user={user} workspace={workspace} />;
    }
    if (workspace.status === "ORDER_READY") {
      return <OrderPanel api={api} onWorkspaceChange={onWorkspaceChange} user={user} workspace={workspace} />;
    }
    return <QuotePanel api={api} onWorkspaceChange={onWorkspaceChange} user={user} workspace={workspace} />;
  }
  return <ReceivingPanel api={api} onWorkspaceChange={onWorkspaceChange} user={user} workspace={workspace} />;
}

interface WorkspaceWorkroomProps extends WorkroomScreenProps {
  workspaceId: string;
}

function WorkspaceWorkroom({ api, user, workspaceId }: WorkspaceWorkroomProps) {
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [error, setError] = useState<{ workspaceId: string; message: string } | null>(null);
  const [analysisPollError, setAnalysisPollError] = useState("");
  const [analysisRefresh, setAnalysisRefresh] = useState(0);
  const [analysisJob, setAnalysisJob] = useState<Job | null>(null);
  const [retryingComparison, setRetryingComparison] = useState(false);
  const [analysisCanCorrectSources, setAnalysisCanCorrectSources] = useState(false);
  const scopedAnalysisJob =
    analysisJob?.workspace_id === workspaceId ? analysisJob : null;
  const analysisJobId = scopedAnalysisJob?.id;
  const analysisJobStatus = scopedAnalysisJob?.status;

  useEffect(() => {
    let active = true;
    void api
      .getWorkspace(workspaceId)
      .then((result) => {
        if (active) {
          setWorkspace(result.data);
          setError(null);
          setAnalysisPollError("");
        }
      })
      .catch((reason: unknown) => {
        if (active) {
          setError({
            workspaceId,
            message:
              reason instanceof Error
                ? reason.message
                : "작업실을 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.",
          });
        }
      });
    return () => {
      active = false;
    };
  }, [api, workspaceId]);

  useEffect(() => {
    if (workspace?.status !== "ANALYZING") return;
    let active = true;
    void api
      .listWorkspaceJobs(workspaceId, { type: "COMPARE", limit: 100 })
      .then(async (page) => {
        if (!active) return;
        const discovered = page.items.find((job) => job.type === "COMPARE") ?? null;
        setAnalysisJob(discovered);
        setAnalysisCanCorrectSources(
          discovered !== null && RETRYABLE_JOB_STATES.has(discovered.status),
        );
        if (discovered && ["SUCCEEDED", "PARTIAL"].includes(discovered.status)) {
          const refreshed = await api.getWorkspace(workspaceId);
          if (active) {
            setWorkspace(refreshed.data);
            setAnalysisPollError("");
          }
        }
      })
      .catch((reason: unknown) => {
        if (active) {
          setAnalysisPollError(
            reason instanceof Error
              ? reason.message
              : "비교 작업 상태를 불러오지 못했습니다. 다시 확인해 주세요.",
          );
        }
      });
    return () => {
      active = false;
    };
  }, [analysisRefresh, api, workspace?.status, workspaceId]);

  useEffect(() => {
    if (analysisJobId) return;
    if (workspace?.status !== "ANALYZING") return;
    let active = true;
    let timer = 0;
    let attempt = 0;
    const schedule = () => {
      timer = window.setTimeout(poll, Math.min(250 * 2 ** attempt, 2_000));
    };
    const poll = () => {
      void api
        .getWorkspace(workspaceId)
        .then((result) => {
          if (!active) return;
          attempt = 0;
          setAnalysisPollError("");
          setWorkspace(result.data);
          if (result.data.status === "ANALYZING") schedule();
        })
        .catch(() => {
          if (!active) return;
          attempt += 1;
          if (attempt >= 5) {
            setAnalysisPollError(
              "진행 상태를 계속 불러오지 못했습니다. 연결을 확인한 뒤 다시 확인해 주세요.",
            );
          } else {
            schedule();
          }
        });
    };
    poll();
    return () => {
      active = false;
      window.clearTimeout(timer);
    };
  }, [analysisJobId, analysisRefresh, api, workspace?.status, workspaceId]);

  useEffect(() => {
    if (workspace?.status !== "ANALYZING" || !analysisJobId || !analysisJobStatus) return;
    if (!ACTIVE_JOB_STATES.has(analysisJobStatus)) return;
    const jobId = analysisJobId;
    let active = true;
    let timer = 0;
    let errors = 0;
    const schedule = () => {
      timer = window.setTimeout(poll, Math.min(250 * 2 ** errors, 2_000));
    };
    const poll = () => {
      void api
        .getJob(jobId)
        .then(async (current) => {
          if (!active) return;
          errors = 0;
          setAnalysisPollError("");
          setAnalysisJob(current);
          if (ACTIVE_JOB_STATES.has(current.status)) {
            schedule();
            return;
          }
          if (["SUCCEEDED", "PARTIAL"].includes(current.status)) {
            try {
              const refreshed = await api.getWorkspace(workspaceId);
              if (active) setWorkspace(refreshed.data);
            } catch (reason) {
              if (active) {
                setAnalysisPollError(
                  reason instanceof Error
                    ? reason.message
                    : "후보 화면을 불러오지 못했습니다. 다시 확인해 주세요.",
                );
              }
            }
          }
        })
        .catch(() => {
          if (!active) return;
          errors += 1;
          if (errors >= 5) {
            setAnalysisPollError(
              "비교 진행 상태를 계속 불러오지 못했습니다. 상태를 다시 확인해 주세요.",
            );
          } else {
            schedule();
          }
        });
    };
    poll();
    return () => {
      active = false;
      window.clearTimeout(timer);
    };
  }, [
    analysisJobId,
    analysisJobStatus,
    analysisRefresh,
    api,
    workspace?.status,
    workspaceId,
  ]);

  async function retryComparison() {
    if (!scopedAnalysisJob || !RETRYABLE_JOB_STATES.has(scopedAnalysisJob.status)) return;
    setRetryingComparison(true);
    setAnalysisPollError("");
    try {
      const current = await api.getJob(scopedAnalysisJob.id);
      setAnalysisJob(current);
      if (ACTIVE_JOB_STATES.has(current.status)) return;
      if (current.status === "SUCCEEDED") {
        const refreshed = await api.getWorkspace(workspaceId);
        setWorkspace(refreshed.data);
        return;
      }
      if (!RETRYABLE_JOB_STATES.has(current.status)) {
        setAnalysisPollError("현재 상태에서는 도서 비교를 다시 시작할 수 없습니다.");
        return;
      }
      const queued = await api.retryJob(current.id);
      setAnalysisJob({ ...current, ...queued, items: current.items });
    } catch (reason) {
      setAnalysisPollError(
        reason instanceof Error ? reason.message : "도서 비교를 다시 시작하지 못했습니다.",
      );
    } finally {
      setRetryingComparison(false);
    }
  }

  if (error?.workspaceId === workspaceId) {
    return (
      <section className="page-shell">
        <h1>수서 작업실</h1>
        <p role="alert">{error.message}</p>
        <Link to="/workspaces">내 수서 업무로 돌아가기</Link>
      </section>
    );
  }

  if (!workspace || workspace.id !== workspaceId) {
    return (
      <p className="loading-state" role="status">
        작업실을 불러오고 있습니다…
      </p>
    );
  }

  const currentStage = workflowPolicy(workspace.status).stage;

  return (
    <section aria-labelledby="workroom-title" className="page-shell workroom-shell">
      <nav aria-label="수서 업무 위치" className="breadcrumb">
        <Link to="/workspaces">내 수서 업무</Link>
        <span aria-hidden="true">/</span>
        <span>{workspace.name}</span>
      </nav>
      <div className="page-heading workroom-heading">
        <p className="eyebrow">{workspace.name}</p>
        <h1 id="workroom-title">수서 작업실</h1>
        <p>지금 할 과정만 열어두었습니다. 바꾼 내용은 입력을 마치면 자동으로 저장됩니다.</p>
      </div>

      <div aria-label="수서 진행 과정" className="stage-list">
        {STAGE_TITLES.map((title, index) => {
          const current = index === currentStage;
          const complete = index < currentStage;
          return (
            <section
              aria-current={current ? "step" : undefined}
              aria-disabled={index > currentStage ? "true" : undefined}
              aria-labelledby={`stage-title-${index}`}
              className={`stage-card ${current ? "stage-current" : "stage-collapsed"}`}
              key={title}
              role="region"
            >
              <div className="stage-heading">
                <h2 id={`stage-title-${index}`}>{title}</h2>
                <span className="stage-state">
                  {current ? "현재 과정" : complete ? "완료" : "앞으로 할 일"}
                </span>
              </div>
              {current ? (
                <CurrentStage
                  analysisJob={scopedAnalysisJob}
            analysisCanCorrectSources={analysisCanCorrectSources}
                  analysisPollError={analysisPollError}
                  api={api}
                  onWorkspaceChange={setWorkspace}
                  onRetryAnalysis={() => {
                    setAnalysisPollError("");
                    setAnalysisRefresh((current) => current + 1);
                  }}
                  onRetryComparison={() => void retryComparison()}
                  retryingComparison={retryingComparison}
                  stage={index}
                  user={user}
                  workspace={workspace}
                />
              ) : (
                <p className="stage-summary">
                  {complete ? completedSummary(index) : "앞 과정이 끝나면 이곳이 열립니다."}
                </p>
              )}
            </section>
          );
        })}
      </div>
    </section>
  );
}

export function WorkroomScreen({ api, user }: WorkroomScreenProps) {
  const { workspaceId = "" } = useParams();
  return (
    <WorkspaceWorkroom
      api={api}
      key={workspaceId}
      user={user}
      workspaceId={workspaceId}
    />
  );
}
