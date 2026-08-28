import { Fragment, useRef, useState } from "react";

import type { Candidate, SuseoroApi, User } from "../../api/client";
import { ModalDialog } from "../../components/ModalDialog";

interface CandidateRowProps {
  api: SuseoroApi;
  candidate: Candidate;
  user: User;
  workspaceId: string;
  announce: (message: string) => void;
  allowDecision?: boolean;
  onDecided?: (candidate: Candidate, outcome: "CANDIDATE" | "EXCLUDED") => void;
}

function shortTime(value: string | Date): string {
  const date = typeof value === "string" ? new Date(value) : value;
  return new Intl.DateTimeFormat("ko-KR", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

function isConflict(error: unknown): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    "status" in error &&
    error.status === 412
  );
}

export function CandidateRow({
  api,
  candidate,
  user,
  workspaceId,
  announce,
  allowDecision = false,
  onDecided,
}: CandidateRowProps) {
  const [quantity, setQuantity] = useState(candidate.quantity);
  const [storedQuantity, setStoredQuantity] = useState(candidate.quantity);
  const [version, setVersion] = useState(candidate.row_version);
  const [lockText, setLockText] = useState("");
  const [savedAt, setSavedAt] = useState("");
  const [conflict, setConflict] = useState(false);
  const quantityRef = useRef<HTMLInputElement>(null);
  const useCurrentRef = useRef<HTMLButtonElement>(null);

  async function claimLock() {
    try {
      const lock = await api.lockCandidate(candidate.id, workspaceId);
      setLockText(
        `${user.display_name}님이 편집 중 · ${shortTime(lock.expires_at)}까지`,
      );
    } catch {
      announce("이 책을 다른 분이 편집하고 있습니다. 잠시 후 다시 확인해 주세요.");
    }
  }

  async function commitQuantity() {
    if (quantity === storedQuantity) return;
    if (!Number.isInteger(quantity) || quantity < 1) {
      announce("수량은 1권 이상의 온전한 숫자로 입력해 주세요.");
      return;
    }
    if (navigator.onLine === false) {
      announce(
        "연결이 끊겨 변경하지 않았습니다. 입력한 내용은 그대로 보관합니다.",
      );
      return;
    }
    try {
      const updated = await api.updateCandidate(
        candidate.id,
        {
          workspace_id: workspaceId,
          changes: { quantity },
          reason: "후보 수량 수정",
        },
        version,
      );
      setStoredQuantity(updated.data.quantity);
      setQuantity(updated.data.quantity);
      setVersion(updated.data.row_version);
      setSavedAt(shortTime(new Date()));
    } catch (error) {
      if (isConflict(error)) {
        try {
          const pages = await Promise.all(
            ["NEEDS_REVIEW", "CANDIDATE", "EXCLUDED"].map((outcome) =>
              api.listCandidates(workspaceId, { outcome }),
            ),
          );
          const latest = pages.flatMap((page) => page.items).find(
            (item) => item.id === candidate.id,
          );
          if (latest) {
            setStoredQuantity(latest.quantity);
            setVersion(latest.row_version);
          }
        } catch {
          // The known stored value remains useful when the refresh is unavailable.
        }
        setConflict(true);
      } else {
        announce(
          error instanceof Error
            ? error.message
            : "변경 내용을 저장하지 못했습니다. 입력한 내용은 그대로 보관합니다.",
        );
      }
    }
  }

  function useCurrent() {
    setQuantity(storedQuantity);
    setConflict(false);
    quantityRef.current?.focus();
  }

  function keepMine() {
    setConflict(false);
    quantityRef.current?.focus();
  }

  async function decide(outcome: "CANDIDATE" | "EXCLUDED") {
    if (navigator.onLine === false) {
      announce(
        "연결이 끊겨 변경하지 않았습니다. 입력한 내용은 그대로 보관합니다.",
      );
      return;
    }
    try {
      const updated = await api.updateCandidate(
        candidate.id,
        {
          workspace_id: workspaceId,
          changes: { outcome },
          reason: outcome === "CANDIDATE" ? "판본 확인 완료" : "사서 확인 후 제외",
        },
        version,
      );
      setVersion(updated.data.row_version);
      onDecided?.(
        {
          ...candidate,
          outcome,
          reason: outcome === "EXCLUDED" ? "LIBRARIAN_DECISION" : candidate.reason,
          row_version: updated.data.row_version,
        },
        outcome,
      );
    } catch (error) {
      announce(
        error instanceof Error
          ? error.message
          : "판정을 저장하지 못했습니다. 잠시 후 다시 시도해 주세요.",
      );
    }
  }

  return (
    <Fragment>
      <tr>
        <th scope="row">
          <strong>{candidate.title}</strong>
          <span className="book-byline">{candidate.authors.join(", ")}</span>
        </th>
        <td>{candidate.isbn13 ?? "ISBN 없음"}</td>
        <td>
          <input
            aria-label={`${candidate.title} 수량`}
            inputMode="numeric"
            min={1}
            onBlur={() => void commitQuantity()}
            onChange={(event) => setQuantity(Number(event.target.value))}
            onFocus={() => void claimLock()}
            onKeyDown={(event) => {
              if (event.key === "Enter") event.currentTarget.blur();
            }}
            ref={quantityRef}
            type="number"
            value={quantity}
          />
          {lockText ? <span className="field-note">{lockText}</span> : null}
        </td>
        <td>
          {candidate.unit_price === null
            ? "가격 미정"
            : `${candidate.unit_price.toLocaleString("ko-KR")}원`}
        </td>
        <td>
          {savedAt ? <span className="saved-copy">저장됨 {savedAt}</span> : "자동 저장"}
        </td>
        {allowDecision ? (
          <td>
            <div className="decision-actions">
              <button
                aria-label={`${candidate.title} 수서 후보로 포함`}
                className="button button-secondary"
                onClick={() => void decide("CANDIDATE")}
                type="button"
              >
                수서 후보로 포함
              </button>
              <button
                aria-label={`${candidate.title} 제외`}
                className="button button-quiet"
                onClick={() => void decide("EXCLUDED")}
                type="button"
              >
                제외
              </button>
            </div>
          </td>
        ) : null}
      </tr>

      {conflict ? (
        <ModalDialog
          initialFocusRef={useCurrentRef}
          labelledBy="candidate-conflict-title"
          onClose={keepMine}
        >
          <div className="dialog-heading">
            <p className="eyebrow">서로의 수정을 잃지 않도록</p>
            <h2 id="candidate-conflict-title">수정 내용 충돌</h2>
            <p>다른 사용자가 먼저 저장했습니다. 두 내용을 확인해 주세요.</p>
          </div>
          <div className="conflict-comparison">
            <section aria-labelledby="current-value-title">
              <h3 id="current-value-title">현재 저장 내용</h3>
              <p>수량 {storedQuantity}권</p>
            </section>
            <section aria-labelledby="mine-value-title">
              <h3 id="mine-value-title">내 변경</h3>
              <p>수량 {quantity}권</p>
            </section>
          </div>
          <div className="dialog-actions">
            <button
              className="button button-quiet"
              onClick={keepMine}
              type="button"
            >
              내 변경 계속 보기
            </button>
            <button
              className="button button-primary"
              onClick={useCurrent}
              ref={useCurrentRef}
              type="button"
            >
              현재 내용 사용
            </button>
          </div>
        </ModalDialog>
      ) : null}
    </Fragment>
  );
}
