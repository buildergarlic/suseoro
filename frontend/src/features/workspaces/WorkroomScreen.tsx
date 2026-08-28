import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";

import type { SuseoroApi, User, Workspace } from "../../api/client";
import { CandidatePanel } from "../candidates/CandidatePanel";
import { IngestionPanel } from "../ingestion/IngestionPanel";
import { canOperate, workflowPolicy } from "./workflowPolicy";

interface WorkroomScreenProps {
  api: SuseoroApi;
  user: User;
}

const STAGE_TITLES = ["1. 후보 만들기", "2. 승인·발주", "3. 납품 검수"] as const;

function completedSummary(stage: number): string {
  return stage === 0
    ? "추천자료와 보유 장서를 비교해 수서 후보를 만들었습니다."
    : "후보 승인과 서점 발주를 마쳤습니다.";
}

function CurrentStage({
  api,
  user,
  workspace,
  stage,
  onWorkspaceChange,
  analysisPollError,
  onRetryAnalysis,
}: {
  api: SuseoroApi;
  user: User;
  workspace: Workspace;
  stage: number;
  onWorkspaceChange: (workspace: Workspace) => void;
  analysisPollError: string;
  onRetryAnalysis: () => void;
}) {
  if (stage === 0) {
    const mode = workflowPolicy(workspace.status).mode;
    if (mode === "CANDIDATES") {
      return (
        <CandidatePanel
          api={api}
          onWorkspaceChange={onWorkspaceChange}
          user={user}
          workspace={workspace}
        />
      );
    }
    if (mode === "ANALYZING") {
      return (
        <div className="calm-placeholder" role="status">
          <h3>도서 비교 중</h3>
          <p>도서관 장서와 추천자료를 비교하고 있습니다.</p>
          <p>이 화면을 그대로 두어도 완료되면 후보가 열립니다.</p>
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
    return (
      <div className="calm-placeholder">
        <h3>승인과 발주 진행</h3>
        {workspace.status === "APPROVAL_PENDING" ? (
          <p role="status">승인을 요청했습니다. 검토 담당자에게 전달했습니다.</p>
        ) : (
          <p>현재 승인 상태와 발주 준비 내용을 이 자리에서 확인합니다.</p>
        )}
      </div>
    );
  }
  return (
    <div className="calm-placeholder">
      <h3>납품된 책 확인</h3>
      <p>도착한 책을 스캔하고 주문 내용과 차분히 맞춰봅니다.</p>
    </div>
  );
}

export function WorkroomScreen({ api, user }: WorkroomScreenProps) {
  const { workspaceId = "" } = useParams();
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [error, setError] = useState<{ workspaceId: string; message: string } | null>(null);
  const [analysisPollError, setAnalysisPollError] = useState("");
  const [analysisRefresh, setAnalysisRefresh] = useState(0);

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
  }, [analysisRefresh, api, workspace?.status, workspaceId]);

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
                  analysisPollError={analysisPollError}
                  api={api}
                  onWorkspaceChange={setWorkspace}
                  onRetryAnalysis={() => {
                    setAnalysisPollError("");
                    setAnalysisRefresh((current) => current + 1);
                  }}
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
