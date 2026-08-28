import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import type { SuseoroApi, User, Workspace } from "../../api/client";
import {
  isReviewerOnly,
  primaryPriority,
  workflowPolicy,
} from "./workflowPolicy";

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
  const reviewer = isReviewerOnly(user);

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
      const leftRank = primaryPriority(user, left.status) ?? 20;
      const rightRank = primaryPriority(user, right.status) ?? 20;
      if (leftRank !== rightRank) return leftRank - rightRank;
      return right.updated_at.localeCompare(left.updated_at);
    });
  }, [user, workspaces]);

  const primary = sorted.find(
    (item) => primaryPriority(user, item.status) !== null,
  );

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
            <span className="status-chip">{workflowPolicy(primary.status).label}</span>
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
                  <td>{workflowPolicy(item.status).label}</td>
                  <td>{workflowPolicy(item.status).owner}</td>
                  <td>{savedAt(item.updated_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
      </div>
    </section>
  );
}
