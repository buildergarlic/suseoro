import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";

import type { SuseoroApi, User, Workspace } from "../../api/client";
import { CandidatePanel } from "../candidates/CandidatePanel";
import { IngestionPanel } from "../ingestion/IngestionPanel";

interface WorkroomScreenProps {
  api: SuseoroApi;
  user: User;
}

const STAGE_TITLES = ["1. 후보 만들기", "2. 승인·발주", "3. 납품 검수"] as const;

function stageFor(status: string): number {
  if (["DRAFT", "ANALYZING", "CANDIDATE_REVIEW"].includes(status)) return 0;
  if (
    [
      "APPROVAL_PENDING",
      "CHANGES_REQUESTED",
      "APPROVED",
      "QUOTE_REVIEW",
      "ORDER_READY",
    ].includes(status)
  ) {
    return 1;
  }
  return 2;
}

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
}: {
  api: SuseoroApi;
  user: User;
  workspace: Workspace;
  stage: number;
}) {
  if (stage === 0) {
    return workspace.status === "CANDIDATE_REVIEW" ? (
      <CandidatePanel api={api} user={user} workspace={workspace} />
    ) : (
      <IngestionPanel api={api} workspace={workspace} />
    );
  }
  if (stage === 1) {
    return (
      <div className="calm-placeholder">
        <h3>승인과 발주 진행</h3>
        <p>현재 승인 상태와 발주 준비 내용을 이 자리에서 확인합니다.</p>
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
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    void api
      .getWorkspace(workspaceId)
      .then((result) => {
        if (active) setWorkspace(result.data);
      })
      .catch((reason: unknown) => {
        if (active) {
          setError(
            reason instanceof Error
              ? reason.message
              : "작업실을 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.",
          );
        }
      });
    return () => {
      active = false;
    };
  }, [api, workspaceId]);

  if (error) {
    return (
      <section className="page-shell">
        <h1>수서 작업실</h1>
        <p role="alert">{error}</p>
        <Link to="/workspaces">내 수서 업무로 돌아가기</Link>
      </section>
    );
  }

  if (!workspace) {
    return (
      <p className="loading-state" role="status">
        작업실을 불러오고 있습니다…
      </p>
    );
  }

  const currentStage = stageFor(workspace.status);

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
                <CurrentStage api={api} stage={index} user={user} workspace={workspace} />
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
