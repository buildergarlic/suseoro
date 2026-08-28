import {
  type ChangeEvent,
  type DragEvent,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import type { SuseoroApi, Workspace } from "../../api/client";
import type { components } from "../../api/types";
import { MappingDialog } from "./MappingDialog";

type Schemas = components["schemas"];
type UploadItem = Schemas["UploadItem"];
type Job = Schemas["JobResponse"];
type JobItem = Schemas["JobFileResult"];
type DocumentRole = Schemas["DocumentRole"];
type MappingRequired = Schemas["MappingRequired"];

interface IngestionPanelProps {
  api: SuseoroApi;
  workspace: Workspace;
  onWorkspaceChange?: (workspace: Workspace) => void;
}

interface MappingState {
  sourceId: string;
  sourceVersion: number;
  mappingRequired: MappingRequired;
}

const ROLE_LABELS: Record<DocumentRole, string> = {
  UNKNOWN: "자료 종류 미정",
  PURCHASE_REQUEST: "추천목록",
  VENDOR_QUOTE: "견적서",
  INVENTORY: "보유 장서",
  CATALOG_FULL: "전체 장서",
  CATALOG_DELTA_REGISTRATION: "신규 등록 장서",
  CATALOG_DELTA_UPDATE: "수정 장서",
};

const ACTIVE_JOB_STATES = new Set(["QUEUED", "RUNNING", "CANCEL_REQUESTED"]);
const TERMINAL_JOB_STATES = new Set(["SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED"]);

function progressPercent(current: number, total: number): number {
  if (total <= 0) return 0;
  return Math.max(0, Math.min(100, Math.round((current / total) * 100)));
}

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback;
}

export function IngestionPanel({
  api,
  workspace,
  onWorkspaceChange = () => undefined,
}: IngestionPanelProps) {
  const [files, setFiles] = useState<File[]>([]);
  const [pastedText, setPastedText] = useState("");
  const [role, setRole] = useState<DocumentRole>("PURCHASE_REQUEST");
  const [uploadItems, setUploadItems] = useState<UploadItem[]>([]);
  const [job, setJob] = useState<Job | null>(null);
  const [uploading, setUploading] = useState(false);
  const [mappingQueue, setMappingQueue] = useState<MappingState[]>([]);
  const [mappingOpen, setMappingOpen] = useState(false);
  const [announcement, setAnnouncement] = useState("");
  const [comparisonRetryIds, setComparisonRetryIds] = useState<string[]>([]);
  const [comparisonJobId, setComparisonJobId] = useState<string | null>(null);
  const [workspaceRefreshPending, setWorkspaceRefreshPending] = useState(false);
  const [comparing, setComparing] = useState(false);
  const beginButtonRef = useRef<HTMLButtonElement>(null);
  const pickerButtonRef = useRef<HTMLButtonElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const replacementInputRefs = useRef(new Map<string, HTMLInputElement>());
  const mountedRef = useRef(true);
  const workspaceIdRef = useRef(workspace.id);
  const uploadItemsRef = useRef<UploadItem[]>([]);
  const sourceIdsRef = useRef<string[]>([]);
  const sourceResultsRef = useRef(new Map<string, JobItem>());

  const canUpload = files.length > 0 || pastedText.trim().length > 0;
  const mapping = mappingOpen ? (mappingQueue[0] ?? null) : null;

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  useEffect(() => {
    if (workspaceIdRef.current === workspace.id) return;
    workspaceIdRef.current = workspace.id;
    setFiles([]);
    setPastedText("");
    setRole("PURCHASE_REQUEST");
    setUploadItems([]);
    setJob(null);
    setUploading(false);
    setMappingQueue([]);
    setMappingOpen(false);
    setAnnouncement("");
    setComparisonRetryIds([]);
    setComparisonJobId(null);
    setWorkspaceRefreshPending(false);
    setComparing(false);
    uploadItemsRef.current = [];
    sourceIdsRef.current = [];
    sourceResultsRef.current.clear();
    if (fileInputRef.current) fileInputRef.current.value = "";
  }, [workspace.id]);

  function appendFiles(incoming: File[]) {
    setFiles((current) => {
      const known = new Set(current.map((file) => `${file.name}:${file.size}`));
      return [
        ...current,
        ...incoming.filter((file) => !known.has(`${file.name}:${file.size}`)),
      ];
    });
  }

  function chooseFiles(event: ChangeEvent<HTMLInputElement>) {
    appendFiles(Array.from(event.target.files ?? []));
  }

  function dropFiles(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    appendFiles(Array.from(event.dataTransfer.files));
  }

  async function waitForJob(jobId: string, showFileProgress = true): Promise<Job | null> {
    let pollErrors = 0;
    let delayMilliseconds = 500;
    while (mountedRef.current) {
      try {
        const next = await api.getJob(jobId);
        if (!mountedRef.current) return null;
        if (showFileProgress) {
          for (const item of next.items) {
            sourceResultsRef.current.set(item.source_document_id, item);
          }
          setJob({ ...next, items: Array.from(sourceResultsRef.current.values()) });
        }
        pollErrors = 0;
        delayMilliseconds = 500;
        if (TERMINAL_JOB_STATES.has(next.status)) return next;
      } catch (error) {
        if (!mountedRef.current) return null;
        pollErrors += 1;
        setAnnouncement("진행 상태를 불러오지 못했습니다. 잠시 후 다시 확인합니다.");
        if (pollErrors >= 5) {
          throw new Error(
            errorMessage(error, "진행 상태를 계속 불러오지 못했습니다."),
            { cause: error },
          );
        }
        delayMilliseconds = Math.min(500 * 2 ** pollErrors, 4_000);
      }
      await new Promise<void>((resolve) => {
        window.setTimeout(resolve, delayMilliseconds);
      });
    }
    return null;
  }

  async function refreshCandidateWorkspace() {
    setWorkspaceRefreshPending(true);
    try {
      const current = await api.getWorkspace(workspace.id);
      if (!mountedRef.current) return;
      onWorkspaceChange(current.data);
      setWorkspaceRefreshPending(false);
      setAnnouncement("비교를 마쳤습니다. 수서 후보를 확인해 주세요.");
    } catch {
      if (!mountedRef.current) return;
      setAnnouncement(
        "도서 비교는 마쳤지만 후보 화면을 불러오지 못했습니다. 화면만 다시 불러와 주세요.",
      );
    }
  }

  async function finishComparison(completed: Job | null) {
    if (!completed) return;
    if (completed.status !== "SUCCEEDED") {
      setAnnouncement("도서 비교를 마치지 못했습니다. 다시 비교해 주세요.");
      return;
    }
    setComparisonRetryIds([]);
    setComparisonJobId(null);
    await refreshCandidateWorkspace();
  }

  async function startComparison(sourceIds: string[]) {
    if (sourceIds.length === 0) {
      setAnnouncement("비교할 수 있는 자료가 없습니다. 실패한 파일을 다시 확인해 주세요.");
      return;
    }
    setComparisonRetryIds(sourceIds);
    setComparisonJobId(null);
    setComparing(true);
    setAnnouncement("우리 도서관 장서와 추천자료를 비교하고 있습니다.");
    try {
      const command = await api.createComparisonJob(
        workspace.id,
        sourceIds,
        workspace.row_version,
      );
      setComparisonJobId(command.job_id);
      await finishComparison(await waitForJob(command.job_id, false));
    } catch (error) {
      if (!mountedRef.current) return;
      setAnnouncement(errorMessage(error, "도서 비교를 마치지 못했습니다. 다시 시도해 주세요."));
    } finally {
      if (mountedRef.current) setComparing(false);
    }
  }

  async function retryComparison() {
    if (comparisonRetryIds.length === 0) return;
    if (!comparisonJobId) {
      await startComparison(comparisonRetryIds);
      return;
    }
    setComparing(true);
    setAnnouncement("도서 비교를 다시 시작했습니다.");
    try {
      const command = await api.retryJob(comparisonJobId);
      await finishComparison(await waitForJob(command.id, false));
    } catch (error) {
      setAnnouncement(errorMessage(error, "도서 비교를 다시 시작하지 못했습니다."));
    } finally {
      if (mountedRef.current) setComparing(false);
    }
  }

  async function finishIngest(completed: Job, sourceIds: string[]) {
    for (const item of completed.items) {
      sourceResultsRef.current.set(item.source_document_id, item);
    }
    const sourceResults = sourceIds
      .map((sourceId) => sourceResultsRef.current.get(sourceId))
      .filter((item): item is JobItem => item !== undefined);
    const mappingItems = sourceResults.filter(
      (item) => item.mapping_required !== null && item.source_document_id,
    );
    if (mappingItems.length > 0) {
      const states = await Promise.all(
        mappingItems.map(async (item) => {
          const source = await api.getSource(item.source_document_id);
          return {
            sourceId: source.data.id,
            sourceVersion: source.data.row_version,
            mappingRequired: item.mapping_required as MappingRequired,
          };
        }),
      );
      if (mountedRef.current) {
        setMappingQueue(states);
        setMappingOpen(true);
        setAnnouncement("파일의 제목과 저자 열을 한 번 확인해 주세요.");
      }
      return;
    }
    setMappingQueue([]);
    setMappingOpen(false);
    if (uploadItemsRef.current.some((item) => item.status === "FAILED")) {
      setAnnouncement(
        "읽지 못한 파일을 다시 올린 뒤 모든 자료를 함께 비교해 주세요.",
      );
      return;
    }
    if (sourceResults.length !== sourceIds.length) {
      setAnnouncement("일부 자료의 처리 결과를 확인하지 못했습니다. 자료 읽기를 다시 시도해 주세요.");
      return;
    }
    const readyIds = sourceResults
      .filter((item) => item.status === "SUCCESS" || item.status === "PARTIAL")
      .map((item) => item.source_document_id);
    if (readyIds.length !== sourceIds.length) {
      setAnnouncement("일부 자료를 읽지 못했습니다. 해당 자료를 다시 읽어 주세요.");
      return;
    }
    await startComparison(readyIds);
  }

  async function runUpload(
    inputs: File[],
    merge: boolean,
    replacedFilename?: string,
  ) {
    const response = await api.uploadSources(workspace.id, { files: inputs, role });
    const nextItems = (() => {
      if (!merge) return response.items;
      const replacement = new Map(response.items.map((item) => [item.filename, item]));
      return [
        ...uploadItemsRef.current.filter(
          (item) =>
            item.filename !== replacedFilename && !replacement.has(item.filename),
        ),
        ...response.items,
      ];
    })();
    uploadItemsRef.current = nextItems;
    setUploadItems(nextItems);
    const failed = response.items.filter((item) => item.status === "FAILED").length;
    setAnnouncement(
      failed > 0
        ? `${failed}개 파일은 확인이 필요합니다.`
        : `${response.items.length}개 파일을 읽고 있습니다.`,
    );
    const acceptedIds = response.items
      .map((item) => item.source_id)
      .filter((sourceId): sourceId is string => sourceId !== null);
    sourceIdsRef.current = merge
      ? Array.from(new Set([...sourceIdsRef.current, ...acceptedIds]))
      : acceptedIds;
    if (!response.job_id || acceptedIds.length === 0) return;
    const completed = await waitForJob(response.job_id);
    if (completed) await finishIngest(completed, sourceIdsRef.current);
  }

  async function beginUpload() {
    if (!canUpload) return;
    setUploading(true);
    setAnnouncement("자료를 안전하게 읽기 시작했습니다.");
    try {
      const inputs = [...files];
      if (pastedText.trim()) {
        inputs.push(
          new File([pastedText], "붙여넣은-자료.txt", {
            type: "text/plain;charset=utf-8",
          }),
        );
      }
      await runUpload(inputs, false);
    } catch (error) {
      if (mountedRef.current) {
        setAnnouncement(errorMessage(error, "자료를 읽지 못했습니다. 잠시 후 다시 시도해 주세요."));
      }
    } finally {
      if (mountedRef.current) setUploading(false);
    }
  }

  async function reupload(item: UploadItem, sourceFile: File) {
    try {
      await runUpload([sourceFile], true, item.filename);
    } catch (error) {
      setAnnouncement(errorMessage(error, "파일을 다시 올리지 못했습니다. 다시 시도해 주세요."));
    }
  }

  function chooseReplacement(
    event: ChangeEvent<HTMLInputElement>,
    item: UploadItem,
  ) {
    const replacement = event.target.files?.[0];
    event.target.value = "";
    if (!replacement) return;
    setFiles((current) => [
      ...current.filter(
        (file) => file.name !== item.filename && file.name !== replacement.name,
      ),
      replacement,
    ]);
    void reupload(item, replacement);
  }

  async function reparse(item: JobItem) {
    setAnnouncement(`${item.filename} 파일을 다시 읽고 있습니다.`);
    try {
      const command = await api.parseSource(item.source_document_id);
      const completed = await waitForJob(command.job_id);
      if (!completed) return;
      await finishIngest(completed, sourceIdsRef.current);
    } catch (error) {
      setAnnouncement(errorMessage(error, "파일을 다시 읽지 못했습니다. 다시 시도해 주세요."));
    }
  }

  async function cancelBatch() {
    if (!job) return;
    try {
      const next = await api.cancelJob(job.id);
      setJob((current) => (current ? { ...current, ...next } : current));
      setAnnouncement("자료 읽기 전체 취소를 요청했습니다.");
    } catch (error) {
      setAnnouncement(errorMessage(error, "자료 읽기 취소를 요청하지 못했습니다."));
    }
  }

  async function retryBatch() {
    if (!job) return;
    try {
      const next = await api.retryJob(job.id);
      setJob((current) => (current ? { ...current, ...next } : current));
      setAnnouncement("자료 읽기 전체를 다시 시작했습니다.");
      const completed = await waitForJob(next.id);
      if (completed) await finishIngest(completed, sourceIdsRef.current);
    } catch (error) {
      setAnnouncement(errorMessage(error, "자료 읽기를 다시 시작하지 못했습니다."));
    }
  }

  const jobItems = useMemo(
    () => new Map(job?.items.map((item) => [item.source_document_id, item]) ?? []),
    [job],
  );
  const overallPercent = job
    ? progressPercent(job.progress_current, job.progress_total)
    : 0;

  function closeMapping() {
    setMappingOpen(false);
    (beginButtonRef.current ?? pickerButtonRef.current)?.focus();
  }

  async function reopenMapping(
    sourceId: string,
    mappingRequired: MappingRequired,
  ) {
    const selected = mappingQueue.find((item) => item.sourceId === sourceId);
    if (selected) {
      setMappingQueue((current) => [
        selected,
        ...current.filter((item) => item.sourceId !== sourceId),
      ]);
      setMappingOpen(true);
      return;
    }
    try {
      const source = await api.getSource(sourceId);
      setMappingQueue((current) => [
        {
          sourceId,
          sourceVersion: source.data.row_version,
          mappingRequired,
        },
        ...current.filter((item) => item.sourceId !== sourceId),
      ]);
      setMappingOpen(true);
      setAnnouncement("파일의 제목과 저자 열을 다시 확인해 주세요.");
    } catch (error) {
      setAnnouncement(
        errorMessage(error, "열 연결 정보를 불러오지 못했습니다. 다시 시도해 주세요."),
      );
    }
  }

  async function applyMapping(
    mappingFields: Record<string, string>,
    rememberTemplate: boolean,
  ) {
    if (!mapping) return;
    try {
      const updated = await api.updateSourceMapping(
        mapping.sourceId,
        {
          role,
          mapping: mappingFields,
          remember_template: rememberTemplate,
          vendor_scope: "*",
        },
        mapping.sourceVersion,
      );
      setMappingQueue((current) =>
        current.map((item) =>
          item.sourceId === mapping.sourceId
            ? { ...item, sourceVersion: updated.data.row_version }
            : item,
        ),
      );
      setAnnouncement("열 연결을 저장했습니다. 자료를 다시 읽고 있습니다.");
      const command = await api.parseSource(mapping.sourceId);
      const completed = await waitForJob(command.job_id);
      if (!completed) return;
      await finishIngest(completed, sourceIdsRef.current);
    } catch (error) {
      if (!mountedRef.current) return;
      setAnnouncement(errorMessage(error, "열 연결을 저장하지 못했습니다. 다시 시도해 주세요."));
    }
  }

  return (
    <div className="ingestion-panel">
      <div className="section-intro">
        <div>
          <p className="eyebrow">자료를 한곳에 모아요</p>
          <h3>추천자료 가져오기</h3>
          <p>파일을 여러 개 골라도 되고, 받은 목록의 글을 그대로 붙여넣어도 됩니다.</p>
        </div>
        <label className="compact-field">
          자료 역할
          <select
            aria-label="자료 역할"
            onChange={(event) => setRole(event.target.value as DocumentRole)}
            value={role}
          >
            <option value="PURCHASE_REQUEST">추천목록</option>
            <option value="VENDOR_QUOTE">견적서</option>
            <option value="INVENTORY">보유 장서</option>
          </select>
        </label>
      </div>

      <div
        className="drop-zone"
        onDragOver={(event) => event.preventDefault()}
        onDrop={dropFiles}
      >
        <div className="file-picker">
          <button
            className="button button-secondary"
            onClick={() => fileInputRef.current?.click()}
            ref={pickerButtonRef}
            type="button"
          >
            파일 고르기
          </button>
          <input
            aria-label="추천자료 파일 선택"
            className="visually-hidden"
            multiple
            onChange={chooseFiles}
            ref={fileInputRef}
            tabIndex={-1}
            type="file"
          />
        </div>
        <p>또는 이곳에 파일을 놓으세요.</p>
        <p className="support-list">
          표: XLS, XLSX, XLSB, ODS, CSV, TSV · 문서: DOCX, HWPX, HWP, MARC, PDF, TXT
        </p>
      </div>

      <label className="text-paste-field">
        추천자료 글 붙여넣기
        <textarea
          onChange={(event) => setPastedText(event.target.value)}
          placeholder="메일이나 문서에서 받은 추천자료를 붙여넣으세요."
          rows={5}
          value={pastedText}
        />
      </label>

      {files.length > 0 && uploadItems.length === 0 ? (
        <ul aria-label="선택한 파일" className="selected-files">
          {files.map((file) => (
            <li key={`${file.name}:${file.size}`}>
              <span>{file.name}</span>
              <span>{ROLE_LABELS[role]}</span>
              <button
                aria-label={`${file.name} 선택 취소`}
                className="button button-quiet"
                onClick={() => setFiles((current) => current.filter((item) => item !== file))}
                type="button"
              >
                빼기
              </button>
            </li>
          ))}
        </ul>
      ) : null}

      {uploadItems.length > 0 ? (
        <>
          <ul aria-label="파일 처리 상태" className="file-status-list">
            {uploadItems.map((item) => {
              const detail = item.source_id ? jobItems.get(item.source_id) : undefined;
              const rejected = item.status === "FAILED";
              const parseFailed = detail?.status === "FAILED";
              const filePercent = detail
                ? detail.status === "SUCCESS"
                  ? 100
                  : progressPercent(detail.processed_rows, detail.total_rows)
                : 0;
              const needsReview = detail?.mapping_required
                ? Math.max(detail.total_rows - detail.processed_rows, 0)
                : detail?.status === "PARTIAL"
                  ? Math.max(detail.total_rows - detail.processed_rows, 0)
                  : 0;
              const statusCopy = rejected
                ? "읽지 못함"
                : detail?.mapping_required
                  ? "열 연결 확인"
                  : parseFailed
                    ? "읽기 실패"
                    : detail?.status === "SUCCESS"
                      ? "읽기 완료"
                      : "대기 중";
              return (
                <li aria-label={`${item.filename} 처리 상태`} key={item.filename}>
                  <div className="file-status-heading">
                    <strong>{item.filename}</strong>
                    <span className={`status-chip ${rejected || parseFailed ? "status-danger" : ""}`}>
                      {statusCopy}
                    </span>
                  </div>
                  <p>{ROLE_LABELS[role]}</p>
                  {rejected ? (
                    <>
                      <p className="error-copy">{item.error?.message}</p>
                      <button
                        aria-label={`${item.filename} 다시 올리기`}
                        className="button button-secondary"
                        onClick={() => replacementInputRefs.current.get(item.filename)?.click()}
                        type="button"
                      >
                        다시 올리기
                      </button>
                      <input
                        aria-label={`${item.filename} 수정한 파일 선택`}
                        className="visually-hidden"
                        onChange={(event) => chooseReplacement(event, item)}
                        ref={(node) => {
                          if (node) replacementInputRefs.current.set(item.filename, node);
                          else replacementInputRefs.current.delete(item.filename);
                        }}
                        tabIndex={-1}
                        type="file"
                      />
                    </>
                  ) : (
                    <>
                      <div className="progress-copy">
                        <progress aria-label={`${item.filename} 진행률`} max={100} value={filePercent} />
                        <span>{filePercent}%</span>
                      </div>
                      <p>
                        {detail?.processed_rows ?? 0}권 읽음 · 확인 필요 {needsReview}
                      </p>
                      {detail?.mapping_required && !mappingOpen ? (
                        <button
                          aria-label={`${item.filename} 열 연결 다시 확인`}
                          className="button button-secondary"
                          onClick={() =>
                            void reopenMapping(
                              detail.source_document_id,
                              detail.mapping_required as MappingRequired,
                            )
                          }
                          type="button"
                        >
                          열 연결 다시 확인
                        </button>
                      ) : null}
                      {parseFailed && detail ? (
                        <button
                          aria-label={`${item.filename} 다시 읽기`}
                          className="button button-secondary"
                          onClick={() => void reparse(detail)}
                          type="button"
                        >
                          다시 읽기
                        </button>
                      ) : null}
                    </>
                  )}
                </li>
              );
            })}
          </ul>
          {job ? (
            <div className="batch-progress">
              <div className="progress-copy">
                <progress aria-label="전체 자료 진행률" max={100} value={overallPercent} />
                <span>{overallPercent}%</span>
              </div>
              {ACTIVE_JOB_STATES.has(job.status) ? (
                <button
                  className="button button-quiet"
                  onClick={() => void cancelBatch()}
                  type="button"
                >
                  자료 읽기 모두 취소
                </button>
              ) : job.status === "FAILED" ||
                (job.status === "PARTIAL" && !job.items.some((item) => item.mapping_required)) ? (
                <button
                  className="button button-secondary"
                  onClick={() => void retryBatch()}
                  type="button"
                >
                  자료 읽기 전체 다시 시도
                </button>
              ) : null}
            </div>
          ) : null}
        </>
      ) : null}

      <div aria-atomic="true" aria-live="polite" className="live-status" role="status">
        {announcement}
      </div>
      <div className="primary-action-row">
        {uploadItems.length === 0 ? (
          <button
            className="button button-primary"
            disabled={!canUpload || uploading}
            onClick={() => void beginUpload()}
            ref={beginButtonRef}
            type="button"
          >
            {uploading ? "책을 찾는 중…" : "우리 도서관에 없는 책 찾기"}
          </button>
        ) : null}
        {comparisonRetryIds.length > 0 ? (
          <button
            className="button button-secondary"
            disabled={comparing}
            onClick={() => void retryComparison()}
            type="button"
          >
            {comparing ? "도서 비교 중…" : "도서 비교 다시 시도"}
          </button>
        ) : null}
        {workspaceRefreshPending ? (
          <button
            className="button button-secondary"
            disabled={comparing}
            onClick={() => void refreshCandidateWorkspace()}
            type="button"
          >
            후보 화면 다시 불러오기
          </button>
        ) : null}
      </div>

      {mapping ? (
        <MappingDialog
          key={mapping.sourceId}
          mappingRequired={mapping.mappingRequired}
          onApply={applyMapping}
          onClose={closeMapping}
        />
      ) : null}
    </div>
  );
}
