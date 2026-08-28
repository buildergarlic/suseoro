import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import type { SuseoroApi, User, Workspace } from "../../api/client";

const stateCopy: Record<string, string> = {
  DRAFT: "자료 준비 중",
  ANALYZING: "도서 비교 중",
  CANDIDATE_REVIEW: "후보 확인 중",
  APPROVAL_PENDING: "승인 기다리는 중",
  CHANGES_REQUESTED: "수정 요청됨",
  APPROVED: "승인됨",
  QUOTE_REVIEW: "견적·예산 조정 중",
  ORDER_READY: "발주파일 준비됨",
  ORDER_SENT: "납품 기다리는 중",
  RECEIVING: "납품 검수 중",
  COMPLETED: "완료",
};

const operatorPriority = new Map([
  ["CHANGES_REQUESTED", 0],
  ["CANDIDATE_REVIEW", 1],
  ["DRAFT", 2],
  ["APPROVED", 3],
  ["QUOTE_REVIEW", 4],
  ["ORDER_READY", 5],
  ["ORDER_SENT", 6],
  ["RECEIVING", 7],
  ["ANALYZING", 8],
  ["APPROVAL_PENDING", 9],
  ["COMPLETED", 10],
]);

function nextOwner(status: string): string {
  if (status === "APPROVAL_PENDING") return "검토자";
  if (status === "ANALYZING") return "자동 처리";
  if (status === "COMPLETED") return "완료";
  return "담당자";
}

function savedAt(value: string): string {
  return new Intl.DateTimeFormat("ko-KR", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(value));
}

interface WorklistScreenProps {
  api: SuseoroApi;
  user: User;
}

export function WorklistScreen({ api, user }: WorklistScreenProps) {
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const reviewer = user.roles.includes("REVIEWER") && !user.roles.includes("OPERATOR");

  useEffect(() => {
    document.title = "내 수서 업무 · 수서로";
    let active = true;
    void api
      .listWorkspaces()
      .then((page) => {
        if (active) setWorkspaces(page.items);
      })
      .catch(() => {
        if (active) setError("업무를 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.");
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [api]);

  const sorted = useMemo(() => {
    return [...workspaces].sort((left, right) => {
      const leftRank = reviewer
        ? left.status === "APPROVAL_PENDING"
          ? 0
          : left.status === "COMPLETED"
            ? 2
            : 1
        : (operatorPriority.get(left.status) ?? 20);
      const rightRank = reviewer
        ? right.status === "APPROVAL_PENDING"
          ? 0
          : right.status === "COMPLETED"
            ? 2
            : 1
        : (operatorPriority.get(right.status) ?? 20);
      if (leftRank !== rightRank) return leftRank - rightRank;
      return right.updated_at.localeCompare(left.updated_at);
    });
  }, [reviewer, workspaces]);

  const primary = reviewer
    ? sorted.find((item) => item.status === "APPROVAL_PENDING")
    : sorted.find((item) => !["APPROVAL_PENDING", "COMPLETED"].includes(item.status));

  if (loading) {
    return (
      <p className="loading-state" role="status">
        업무를 불러오고 있습니다…
      </p>
    );
  }

  return (
    <section className="page-shell" aria-labelledby="worklist-title">
      <div className="page-heading">
        <p className="eyebrow">오늘 이어갈 일</p>
        <h1 id="worklist-title">내 수서 업무</h1>
        <p>지금 내 차례인 일부터 위에 모아두었습니다.</p>
      </div>
      {primary ? (
        <aside className="next-action" aria-label="가장 먼저 할 일">
          <div>
            <span className="status-chip">{stateCopy[primary.status] ?? primary.status}</span>
            <p>{reviewer ? "승인을 기다리는 목록이 있습니다." : "이 작업부터 이어가면 됩니다."}</p>
          </div>
          <Link className="button button-primary" to={`/workspaces/${primary.id}`}>
            {reviewer ? "승인할 작업 보기" : "이어 하기"}
          </Link>
        </aside>
      ) : null}
      {error ? <p role="alert" className="message message-error">{error}</p> : null}
      <div className="table-scroll">
          <table aria-label="수서 업무 목록">
            <thead>
              <tr>
                <th scope="col">학교</th>
                <th scope="col">작업명</th>
                <th scope="col">상태</th>
                <th scope="col">다음 담당</th>
                <th scope="col">마지막 저장</th>
              </tr>
            </thead>
            <tbody>
              {sorted.map((item) => (
                <tr key={item.id}>
                  <td>우리 학교</td>
                  <th scope="row">
                    <Link to={`/workspaces/${item.id}`}>{item.name}</Link>
                  </th>
                  <td>{stateCopy[item.status] ?? item.status}</td>
                  <td>{nextOwner(item.status)}</td>
                  <td>{savedAt(item.updated_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
      </div>
    </section>
  );
}
