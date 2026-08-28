import { Fragment, useEffect, useRef, useState } from "react";

import type { Candidate, SuseoroApi, User } from "../../api/client";
import { ModalDialog } from "../../components/ModalDialog";

interface CandidateRowProps {
  api: SuseoroApi;
  candidate: Candidate;
  user: User;
  workspaceId: string;
  announce: (message: string) => void;
  editable: boolean;
  allowDecision?: boolean;
  onCandidateUpdated?: (candidate: Candidate, previous: Candidate) => void;
  onDecided?: (candidate: Candidate, outcome: "CANDIDATE" | "EXCLUDED") => void;
  onMutationStarted?: () => void;
  onMutationFinished?: (authoritative: boolean) => void;
}

interface ConflictState {
  server: Candidate;
  localQuantity: number;
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

function apiErrorCode(error: unknown): string | null {
  if (
    typeof error === "object" &&
    error !== null &&
    "detail" in error &&
    typeof error.detail === "object" &&
    error.detail !== null &&
    "code" in error.detail
  ) {
    return String(error.detail.code);
  }
  return null;
}

function apiErrorField(error: unknown, field: string): unknown {
  if (
    typeof error !== "object" ||
    error === null ||
    !("detail" in error) ||
    typeof error.detail !== "object" ||
    error.detail === null
  ) {
    return undefined;
  }
  const detail = error.detail as Record<string, unknown>;
  const fields = detail.fields;
  if (!Array.isArray(fields)) return undefined;
  const item: unknown = fields.find(
    (value: unknown) =>
      typeof value === "object" &&
      value !== null &&
      "field" in value &&
      value.field === field,
  );
  if (typeof item !== "object" || item === null || !("value" in item)) return undefined;
  const valueHolder = item as Record<string, unknown>;
  return valueHolder.value;
}

export function CandidateRow({
  api,
  candidate,
  user,
  workspaceId,
  announce,
  editable,
  allowDecision = false,
  onCandidateUpdated,
  onDecided,
  onMutationStarted,
  onMutationFinished,
}: CandidateRowProps) {
  const [quantity, setQuantityState] = useState(candidate.quantity);
  const [confirmedQuantity, setConfirmedQuantity] = useState(candidate.quantity);
  const [lockState, setLockState] = useState<"idle" | "acquiring" | "acquired" | "failed">(
    "idle",
  );
  const [lockText, setLockText] = useState("");
  const [savedAt, setSavedAt] = useState("");
  const [deciding, setDeciding] = useState(false);
  const [conflict, setConflict] = useState<ConflictState | null>(null);
  const quantityRef = useRef<HTMLInputElement>(null);
  const useCurrentRef = useRef<HTMLButtonElement>(null);
  const quantityDraftRef = useRef(candidate.quantity);
  const confirmedQuantityRef = useRef(candidate.quantity);
  const versionRef = useRef(candidate.row_version);
  const authoritativeRef = useRef(candidate);
  const saveInFlightRef = useRef(false);
  const queuedQuantityRef = useRef<number | null>(null);
  const saveDrainPromiseRef = useRef<Promise<void>>(Promise.resolve());
  const lockGenerationRef = useRef(0);
  const lockStateRef = useRef<"idle" | "acquiring" | "acquired" | "failed">("idle");
  const lockPromiseRef = useRef<Promise<boolean> | null>(null);
  const lockExpiryTimerRef = useRef<number | null>(null);

  useEffect(() => {
    const current = authoritativeRef.current;
    if (candidate.id !== current.id || candidate.row_version <= versionRef.current) return;
    const dirty =
      quantityDraftRef.current !== confirmedQuantityRef.current ||
      saveInFlightRef.current ||
      queuedQuantityRef.current !== null;
    authoritativeRef.current = candidate;
    versionRef.current = candidate.row_version;
    confirmedQuantityRef.current = candidate.quantity;
    setConfirmedQuantity(candidate.quantity);
    if (!dirty) setQuantity(candidate.quantity);
  }, [candidate]);

  useEffect(
    () => () => {
      if (lockExpiryTimerRef.current !== null) {
        window.clearTimeout(lockExpiryTimerRef.current);
      }
    },
    [],
  );

  function setQuantity(next: number) {
    quantityDraftRef.current = next;
    setQuantityState(next);
  }

  function changeLockState(next: "idle" | "acquiring" | "acquired" | "failed") {
    lockStateRef.current = next;
    setLockState(next);
  }

  async function claimLock(): Promise<boolean> {
    if (!editable) return false;
    if (lockStateRef.current === "acquired") return true;
    if (lockPromiseRef.current) return await lockPromiseRef.current;
    const generation = lockGenerationRef.current + 1;
    lockGenerationRef.current = generation;
    changeLockState("acquiring");
    const pending = (async () => {
      try {
        const lock = await api.lockCandidate(candidate.id, workspaceId);
        if (generation !== lockGenerationRef.current) return false;
        changeLockState("acquired");
        setLockText(
          `${lock.actor_id === user.id ? user.display_name : "다른 담당자"}님이 편집 중 · ${shortTime(lock.expires_at)}까지`,
        );
        if (lockExpiryTimerRef.current !== null) {
          window.clearTimeout(lockExpiryTimerRef.current);
        }
        lockExpiryTimerRef.current = window.setTimeout(
          () => {
            if (generation !== lockGenerationRef.current) return;
            changeLockState("idle");
            setLockText("편집 시간이 지나 다시 잠금 확인이 필요합니다.");
            quantityRef.current?.blur();
          },
          Math.max(0, Date.parse(lock.expires_at) - Date.now()),
        );
        return true;
      } catch (error) {
        if (generation !== lockGenerationRef.current) return false;
        changeLockState("failed");
        const expiresAt = apiErrorField(error, "expires_at");
        setLockText(
          typeof expiresAt === "string"
            ? `다른 담당자가 ${shortTime(expiresAt)}까지 편집 중입니다.`
            : "다른 담당자가 편집 중입니다.",
        );
        announce("이 책을 다른 분이 편집하고 있습니다. 잠시 후 다시 확인해 주세요.");
        return false;
      } finally {
        if (generation === lockGenerationRef.current) lockPromiseRef.current = null;
      }
    })();
    lockPromiseRef.current = pending;
    return await pending;
  }

  async function drainSaves() {
    if (saveInFlightRef.current) return;
    saveInFlightRef.current = true;
    try {
      while (queuedQuantityRef.current !== null) {
        const desired = queuedQuantityRef.current;
        queuedQuantityRef.current = null;
        if (desired === confirmedQuantityRef.current) continue;
        let authoritative = false;
        try {
          onMutationStarted?.();
          const updated = await api.updateCandidate(
            candidate.id,
            {
              workspace_id: workspaceId,
              changes: { quantity: desired },
              reason: "후보 수량 수정",
            },
            versionRef.current,
          );
          const previous = authoritativeRef.current;
          const merged: Candidate = { ...previous, ...updated.data };
          authoritativeRef.current = merged;
          versionRef.current = updated.data.row_version;
          confirmedQuantityRef.current = updated.data.quantity;
          setConfirmedQuantity(updated.data.quantity);
          if (quantityDraftRef.current === desired) setQuantity(updated.data.quantity);
          setSavedAt(shortTime(new Date()));
          onCandidateUpdated?.(merged, previous);
          authoritative = true;
        } catch (error) {
          if (isConflict(error)) {
            try {
              const latest = await api.getCandidate(candidate.id, workspaceId);
              const previous = authoritativeRef.current;
              authoritativeRef.current = latest.data;
              versionRef.current = latest.data.row_version;
              confirmedQuantityRef.current = latest.data.quantity;
              setConfirmedQuantity(latest.data.quantity);
              queuedQuantityRef.current = null;
              setConflict({ server: latest.data, localQuantity: quantityDraftRef.current });
              onCandidateUpdated?.(latest.data, previous);
              authoritative = true;
            } catch (refreshError) {
              announce(
                refreshError instanceof Error
                  ? refreshError.message
                  : "현재 저장 내용을 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.",
              );
            }
          } else {
            if (
              apiErrorCode(error) === "EDIT_LOCK_REQUIRED" ||
              apiErrorCode(error) === "EDIT_LOCKED"
            ) {
              changeLockState("failed");
            }
            announce(
              error instanceof Error
                ? error.message
                : "변경 내용을 저장하지 못했습니다. 입력한 내용은 그대로 보관합니다.",
            );
          }
          break;
        } finally {
          onMutationFinished?.(authoritative);
        }
      }
    } finally {
      saveInFlightRef.current = false;
    }
  }

  function commitQuantity(): Promise<void> {
    const draft = quantityDraftRef.current;
    if (draft === confirmedQuantityRef.current) return saveDrainPromiseRef.current;
    if (lockStateRef.current !== "acquired") {
      announce("편집 잠금을 확인한 뒤 수량을 저장할 수 있습니다.");
      return saveDrainPromiseRef.current;
    }
    if (!Number.isInteger(draft) || draft < 1) {
      announce("수량은 1권 이상의 온전한 숫자로 입력해 주세요.");
      return saveDrainPromiseRef.current;
    }
    if (navigator.onLine === false) {
      announce("연결이 끊겨 변경하지 않았습니다. 입력한 내용은 그대로 보관합니다.");
      return saveDrainPromiseRef.current;
    }
    queuedQuantityRef.current = draft;
    if (!saveInFlightRef.current) {
      saveDrainPromiseRef.current = drainSaves();
    }
    return saveDrainPromiseRef.current;
  }

  function useCurrent() {
    if (!conflict) return;
    setQuantity(conflict.server.quantity);
    setConflict(null);
    quantityRef.current?.focus();
  }

  function keepMine() {
    setConflict(null);
    quantityRef.current?.focus();
  }

  async function decide(outcome: "CANDIDATE" | "EXCLUDED") {
    if (navigator.onLine === false) {
      announce("연결이 끊겨 변경하지 않았습니다. 입력한 내용은 그대로 보관합니다.");
      return;
    }
    setDeciding(true);
    try {
      if (!(await claimLock())) return;
      await commitQuantity();
      await saveDrainPromiseRef.current;
      if (quantityDraftRef.current !== confirmedQuantityRef.current || conflict) {
        announce("수량 저장을 확인한 뒤 판정해 주세요.");
        return;
      }
      onMutationStarted?.();
      let authoritative = false;
      try {
        const updated = await api.updateCandidate(
            candidate.id,
            {
              workspace_id: workspaceId,
              changes: { outcome },
              reason:
                outcome === "CANDIDATE" ? "판본 확인 완료" : "사서 확인 후 제외",
            },
            versionRef.current,
          );
        const merged: Candidate = {
          ...authoritativeRef.current,
          ...updated.data,
          reason:
            outcome === "EXCLUDED"
              ? "LIBRARIAN_DECISION"
              : authoritativeRef.current.reason,
        };
        authoritativeRef.current = merged;
        versionRef.current = updated.data.row_version;
        onDecided?.(merged, outcome);
        authoritative = true;
      } finally {
        onMutationFinished?.(authoritative);
      }
    } catch (error) {
      announce(
        error instanceof Error
          ? error.message
          : "판정을 저장하지 못했습니다. 잠시 후 다시 시도해 주세요.",
      );
    } finally {
      setDeciding(false);
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
          {editable ? (
            <>
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
                readOnly={lockState !== "acquired"}
                ref={quantityRef}
                type="number"
                value={quantity}
              />
              {lockText ? <span className="field-note">{lockText}</span> : null}
              {lockState === "failed" ? (
                <button
                  className="button button-quiet"
                  onClick={() => void claimLock()}
                  type="button"
                >
                  편집 잠금 다시 확인
                </button>
              ) : null}
            </>
          ) : (
            <span>{confirmedQuantity}권</span>
          )}
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
                disabled={deciding}
                onClick={() => void decide("CANDIDATE")}
                type="button"
              >
                수서 후보로 포함
              </button>
              <button
                aria-label={`${candidate.title} 제외`}
                className="button button-quiet"
                disabled={deciding}
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
              <p>수량 {conflict.server.quantity}권</p>
            </section>
            <section aria-labelledby="mine-value-title">
              <h3 id="mine-value-title">내 변경</h3>
              <p>수량 {conflict.localQuantity}권</p>
            </section>
          </div>
          <div className="dialog-actions">
            <button className="button button-quiet" onClick={keepMine} type="button">
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
