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
type DocumentRole = Schemas["DocumentRole"];

interface IngestionPanelProps {
  api: SuseoroApi;
  workspace: Workspace;
}

interface MappingState {
  sourceId: string;
  sourceVersion: number;
  previewText: string;
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

function progressPercent(job: Job | null): number {
  if (!job || job.progress_total <= 0) return 0;
  return Math.round((job.progress_current / job.progress_total) * 100);
}

export function IngestionPanel({ api, workspace }: IngestionPanelProps) {
  const [files, setFiles] = useState<File[]>([]);
  const [pastedText, setPastedText] = useState("");
  const [role, setRole] = useState<DocumentRole>("PURCHASE_REQUEST");
  const [uploadItems, setUploadItems] = useState<UploadItem[]>([]);
  const [job, setJob] = useState<Job | null>(null);
  const [uploading, setUploading] = useState(false);
  const [mapping, setMapping] = useState<MappingState | null>(null);
  const [announcement, setAnnouncement] = useState("");
  const beginButtonRef = useRef<HTMLButtonElement>(null);

  const canUpload = files.length > 0 || pastedText.trim().length > 0;

  useEffect(() => {
    if (!job || !ACTIVE_JOB_STATES.has(job.status)) return;
    let active = true;
    const timer = window.setTimeout(() => {
      void api.getJob(job.id).then((next) => {
        if (active) setJob(next);
      }).catch(() => {
        if (active) {
          setAnnouncement("진행 상태를 불러오지 못했습니다. 잠시 후 다시 확인합니다.");
        }
      });
    }, 1_500);
    return () => {
      active = false;
      window.clearTimeout(timer);
    };
  }, [api, job]);

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

  async function previewFor(filename: string): Promise<string> {
    if (filename === "붙여넣은-자료.txt") return pastedText;
    const sourceFile = files.find((file) => file.name === filename);
    return sourceFile ? sourceFile.text() : "";
  }

  async function inspectJob(nextJob: Job) {
    setJob(nextJob);
    const mappingItem = nextJob.items.find(
      (item) => item.status === "NEEDS_MAPPING" && item.source_document_id,
    );
    if (!mappingItem) return;
    const source = await api.getSource(mappingItem.source_document_id);
    setMapping({
      sourceId: source.data.id,
      sourceVersion: source.data.row_version,
      previewText: await previewFor(mappingItem.filename),
    });
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
      const response = await api.uploadSources(workspace.id, {
        files: inputs,
        role,
      });
      setUploadItems(response.items);
      const failed = response.items.filter((item) => item.status === "FAILED").length;
      setAnnouncement(
        failed > 0
          ? `${failed}개 파일은 확인이 필요합니다.`
          : `${response.items.length}개 파일을 읽고 있습니다.`,
      );
      if (response.job_id) {
        await inspectJob(await api.getJob(response.job_id));
      }
    } catch (error) {
      setAnnouncement(
        error instanceof Error
          ? error.message
          : "자료를 읽지 못했습니다. 잠시 후 다시 시도해 주세요.",
      );
    } finally {
      setUploading(false);
    }
  }

  async function cancel() {
    if (!job) return;
    const next = await api.cancelJob(job.id);
    setJob((current) => (current ? { ...current, ...next } : current));
    setAnnouncement("자료 읽기 취소를 요청했습니다.");
  }

  async function retry() {
    if (!job) {
      await beginUpload();
      return;
    }
    const next = await api.retryJob(job.id);
    setJob((current) => (current ? { ...current, ...next } : current));
    setAnnouncement("파일을 다시 읽기 시작했습니다.");
  }

  const jobItems = useMemo(
    () => new Map(job?.items.map((item) => [item.filename, item]) ?? []),
    [job],
  );
  const percent = progressPercent(job);

  function closeMapping() {
    setMapping(null);
    beginButtonRef.current?.focus();
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
          <select onChange={(event) => setRole(event.target.value as DocumentRole)} value={role}>
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
        <label className="file-picker">
          <span className="button button-secondary">파일 고르기</span>
          <input
            aria-label="추천자료 파일 선택"
            className="visually-hidden"
            multiple
            onChange={chooseFiles}
            type="file"
          />
        </label>
        <p>또는 이곳에 파일을 놓으세요.</p>
        <p className="support-list">
          표: XLS, XLSX, XLSB, ODS, CSV, TSV · 문서: DOCX, HWPX, HWP, MARC
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
        <ul aria-label="파일 처리 상태" className="file-status-list">
          {uploadItems.map((item) => {
            const detail = jobItems.get(item.filename);
            const failed = item.status === "FAILED";
            return (
              <li aria-label={`${item.filename} 처리 상태`} key={item.filename}>
                <div className="file-status-heading">
                  <strong>{item.filename}</strong>
                  <span className={`status-chip ${failed ? "status-danger" : ""}`}>
                    {failed
                      ? "읽지 못함"
                      : detail?.status === "NEEDS_MAPPING"
                        ? "열 연결 확인"
                        : ACTIVE_JOB_STATES.has(job?.status ?? "")
                          ? "분석 중"
                          : "읽기 완료"}
                  </span>
                </div>
                <p>{ROLE_LABELS[role]}</p>
                {failed ? (
                  <>
                    <p className="error-copy">{item.error?.message}</p>
                    <button
                      aria-label={`${item.filename} 다시 시도`}
                      className="button button-secondary"
                      onClick={() => void retry()}
                      type="button"
                    >
                      다시 시도
                    </button>
                  </>
                ) : (
                  <>
                    <div className="progress-copy">
                      <progress aria-label={`${item.filename} 진행률`} max={100} value={percent} />
                      <span>{percent}%</span>
                    </div>
                    <p>
                      {detail?.processed_rows ?? 0}권 읽음 · 확인 필요{" "}
                      {detail?.status === "NEEDS_MAPPING" ? detail.total_rows : 0}
                    </p>
                    {ACTIVE_JOB_STATES.has(job?.status ?? "") ? (
                      <button
                        aria-label={`${item.filename} 취소`}
                        className="button button-quiet"
                        onClick={() => void cancel()}
                        type="button"
                      >
                        취소
                      </button>
                    ) : null}
                  </>
                )}
              </li>
            );
          })}
        </ul>
      ) : null}

      <div aria-atomic="true" aria-live="polite" className="live-status" role="status">
        {announcement}
      </div>
      <div className="primary-action-row">
        <button
          className="button button-primary"
          disabled={!canUpload || uploading}
          onClick={() => void beginUpload()}
          ref={beginButtonRef}
          type="button"
        >
          {uploading ? "책을 찾는 중…" : "우리 도서관에 없는 책 찾기"}
        </button>
      </div>

      {mapping ? (
        <MappingDialog
          onApply={async (mappingFields, rememberTemplate) => {
            await api.updateSourceMapping(
              mapping.sourceId,
              {
                role,
                mapping: mappingFields,
                remember_template: rememberTemplate,
                vendor_scope: "*",
              },
              mapping.sourceVersion,
            );
            closeMapping();
            setAnnouncement("열 연결을 저장했습니다.");
          }}
          onClose={closeMapping}
          previewText={mapping.previewText}
        />
      ) : null}
    </div>
  );
}
