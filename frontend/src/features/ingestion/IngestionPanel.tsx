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
  comparisonJob?: Job | null;
}

interface MappingState {
  sourceId: string;
  sourceVersion: number;
  mappingRequired: MappingRequired;
  role: DocumentRole;
}

interface RecheckState {
  jobId: string;
  purpose: "INGEST" | "COMPARE";
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
const RETRYABLE_TERMINAL_JOB_STATES = new Set(["PARTIAL", "FAILED", "CANCELLED"]);

function progressPercent(current: number, total: number): number {
  if (total <= 0) return 0;
  return Math.max(0, Math.min(100, Math.round((current / total) * 100)));
}

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback;
}

function errorCode(error: unknown): string | null {
  if (
    typeof error === "object" &&
    error !== null &&
    "detail" in error &&
    typeof error.detail === "object" &&
    error.detail !== null &&
    "code" in error.detail &&
    typeof error.detail.code === "string"
  ) {
    return error.detail.code;
  }
  return null;
}

export function IngestionPanel({
  api,
  workspace,
  onWorkspaceChange = () => undefined,
  comparisonJob = null,
}: IngestionPanelProps) {
  const initialComparisonJob =
    comparisonJob &&
    comparisonJob.workspace_id === workspace.id &&
    RETRYABLE_TERMINAL_JOB_STATES.has(comparisonJob.status)
      ? comparisonJob
      : null;
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
  const [comparisonJobId, setComparisonJobId] = useState<string | null>(
    () => initialComparisonJob?.id ?? null,
  );
  const [workspaceRefreshPending, setWorkspaceRefreshPending] = useState(false);
  const [comparing, setComparing] = useState(false);
  const [comparisonRetryable, setComparisonRetryable] = useState(
    () => initialComparisonJob !== null,
  );
  const [comparisonError, setComparisonError] = useState(
    () =>
      initialComparisonJob?.error?.message ??
      (initialComparisonJob
        ? "도서 비교를 마치지 못했습니다. 자료를 확인한 뒤 다시 시도해 주세요."
        : ""),
  );
  const [parsedReplacementPending, setParsedReplacementPending] = useState<Set<string>>(
    () => new Set(),
  );
  const [recheckJobs, setRecheckJobs] = useState<RecheckState[]>([]);
  const [displayedSourceRoles, setDisplayedSourceRoles] = useState(
    () => new Map<string, DocumentRole>(),
  );
  const [displayedRepairRoles, setDisplayedRepairRoles] = useState(
    () => new Map<string, DocumentRole>(),
  );
  const beginButtonRef = useRef<HTMLButtonElement>(null);
  const pickerButtonRef = useRef<HTMLButtonElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const replacementInputRefs = useRef(new Map<string, HTMLInputElement>());
  const mountedRef = useRef(true);
  const workspaceIdRef = useRef(workspace.id);
  const renderedWorkspaceIdRef = useRef(workspace.id);
  const workspaceVersionRef = useRef(workspace.row_version);
  const workspaceStatusRef = useRef(workspace.status);
  const uploadItemsRef = useRef<UploadItem[]>([]);
  const sourceIdsRef = useRef<string[]>([]);
  const sourceResultsRef = useRef(new Map<string, JobItem>());
  const sourceRolesRef = useRef(new Map<string, DocumentRole>());
  const sourceJobsRef = useRef(
    new Map<string, Array<{ id: string; filename: string }>>(),
  );
  const hydrationGenerationRef = useRef(0);
  const repairGenerationsRef = useRef(new Map<string, number>());
  const repairSourcesRef = useRef(new Map<string, string>());
  const repairRolesRef = useRef(new Map<string, DocumentRole>());
  const comparisonStartedRef = useRef(false);
  const parsedReplacementInFlightRef = useRef(new Set<string>());
  const parsedReplacementRolesRef = useRef(new Map<string, DocumentRole>());

  const canUpload = files.length > 0 || pastedText.trim().length > 0;
  const mapping = mappingOpen ? (mappingQueue[0] ?? null) : null;

  function operationIsCurrent(expectedWorkspaceId: string, generation: number) {
    return (
      mountedRef.current &&
      renderedWorkspaceIdRef.current === expectedWorkspaceId &&
      hydrationGenerationRef.current === generation
    );
  }

  function mergeJobResults(current: Job) {
    for (const item of current.items) {
      sourceResultsRef.current.set(item.source_document_id, item);
    }
    if (!RETRYABLE_TERMINAL_JOB_STATES.has(current.status)) return;
    for (const source of sourceJobsRef.current.get(current.id) ?? []) {
      if (sourceResultsRef.current.has(source.id)) continue;
      sourceResultsRef.current.set(source.id, {
        source_document_id: source.id,
        filename: source.filename,
        status: "FAILED",
        total_rows: 0,
        processed_rows: 0,
        error:
          current.error ??
          {
            code: "JOB_FAILED",
            message: "파일 내용을 읽지 못했습니다. 다시 시도해 주세요.",
            type: null,
          },
        mapping_required: null,
      });
    }
  }

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  useEffect(() => {
    renderedWorkspaceIdRef.current = workspace.id;
    workspaceVersionRef.current = workspace.row_version;
    workspaceStatusRef.current = workspace.status;
  }, [workspace.id, workspace.row_version, workspace.status]);

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
    setComparisonRetryable(false);
    setComparisonError("");
    setRecheckJobs([]);
    uploadItemsRef.current = [];
    sourceIdsRef.current = [];
    sourceResultsRef.current.clear();
    sourceRolesRef.current.clear();
    sourceJobsRef.current.clear();
    repairGenerationsRef.current.clear();
    repairSourcesRef.current.clear();
    repairRolesRef.current.clear();
    setDisplayedSourceRoles(new Map());
    setDisplayedRepairRoles(new Map());
    comparisonStartedRef.current = false;
    parsedReplacementInFlightRef.current.clear();
    parsedReplacementRolesRef.current.clear();
    setParsedReplacementPending(new Set());
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

  function queueRecheck(pending: RecheckState) {
    setRecheckJobs((current) =>
      current.some((item) => item.jobId === pending.jobId)
        ? current
        : [...current, pending],
    );
  }

  async function waitForJob(
    jobId: string,
    showFileProgress = true,
    isCurrent: () => boolean = () => true,
  ): Promise<Job | null> {
    let pollErrors = 0;
    let delayMilliseconds = 250;
    while (mountedRef.current) {
      try {
        const next = await api.getJob(jobId);
        if (!mountedRef.current || !isCurrent()) return null;
        if (showFileProgress) {
          for (const item of next.items) {
            sourceResultsRef.current.set(item.source_document_id, item);
          }
          setJob({ ...next, items: Array.from(sourceResultsRef.current.values()) });
        }
        pollErrors = 0;
        delayMilliseconds = 250;
        if (TERMINAL_JOB_STATES.has(next.status)) return next;
      } catch (error) {
        if (!mountedRef.current) return null;
        pollErrors += 1;
        setAnnouncement("진행 상태를 불러오지 못했습니다. 잠시 후 다시 확인합니다.");
        if (pollErrors >= 5) {
          queueRecheck({
            jobId,
            purpose: showFileProgress ? "INGEST" : "COMPARE",
          });
          if (!showFileProgress) setComparisonRetryable(false);
          setAnnouncement(
            errorMessage(error, "진행 상태를 계속 불러오지 못했습니다. 상태를 다시 확인해 주세요."),
          );
          return null;
        }
        delayMilliseconds = Math.min(250 * 2 ** pollErrors, 2_000);
      }
      await new Promise<void>((resolve) => {
        window.setTimeout(resolve, delayMilliseconds);
      });
    }
    return null;
  }

  useEffect(() => {
    const generation = ++hydrationGenerationRef.current;
    let active = true;
    void Promise.all([
      api.listSources(workspace.id),
      api.listUploadRepairs(workspace.id),
      api.listWorkspaceJobs(workspace.id),
    ])
      .then(async ([sourcePage, repairPage, jobPage]) => {
        if (!active || hydrationGenerationRef.current !== generation) return;
        const discoveredItems: UploadItem[] = [
          ...sourcePage.items.map((source) => ({
            filename: source.filename,
            status: "ACCEPTED",
            source_id: source.id,
            error: null,
            repair_obligation_id: null,
            repair_generation: null,
          })),
          ...repairPage.items.map((repair) => ({
            filename: repair.filename,
            status: "FAILED",
            source_id: null,
            error: repair.error,
            repair_obligation_id: repair.id,
            repair_generation: repair.generation,
          })),
        ];
        uploadItemsRef.current = discoveredItems;
        setUploadItems(discoveredItems);
        sourceIdsRef.current = sourcePage.items
          .filter((source) => source.role === "PURCHASE_REQUEST")
          .map((source) => source.id);
        const mappingStates: MappingState[] = [];
        for (const repair of repairPage.items) {
          repairGenerationsRef.current.set(repair.id, repair.generation);
          repairRolesRef.current.set(repair.id, repair.role as DocumentRole);
          if (repair.resolved_source_id) {
            repairSourcesRef.current.set(repair.id, repair.resolved_source_id);
          }
        }
        for (const source of sourcePage.items) {
          sourceRolesRef.current.set(source.id, source.role as DocumentRole);
          if (source.latest_result) {
            sourceResultsRef.current.set(source.id, source.latest_result);
          }
          if (source.latest_result?.mapping_required) {
            mappingStates.push({
              sourceId: source.id,
              sourceVersion: source.row_version,
              mappingRequired: source.latest_result.mapping_required,
              role: source.role as DocumentRole,
            });
          }
          if (
            source.role === "PURCHASE_REQUEST" &&
            source.latest_job_id !== null
          ) {
            const linked = sourceJobsRef.current.get(source.latest_job_id) ?? [];
            linked.push({ id: source.id, filename: source.filename });
            sourceJobsRef.current.set(source.latest_job_id, linked);
          }
        }
        setDisplayedSourceRoles(new Map(sourceRolesRef.current));
        setDisplayedRepairRoles(new Map(repairRolesRef.current));
        const discoveredComparison = jobPage.items.find(
          (candidate) => candidate.type === "COMPARE",
        );
        if (
          discoveredComparison &&
          repairPage.items.length === 0 &&
          RETRYABLE_TERMINAL_JOB_STATES.has(discoveredComparison.status)
        ) {
          setComparisonJobId(discoveredComparison.id);
          setComparisonRetryIds(sourceIdsRef.current);
          setComparisonRetryable(true);
          setComparisonError(
            discoveredComparison.error?.message ??
              "도서 비교를 마치지 못했습니다. 자료를 확인한 뒤 다시 시도해 주세요.",
          );
        }
        if (repairPage.items.length > 0) {
          setComparisonRetryable(false);
          setComparisonError("");
        }
        const latestProcessingJobIds = new Set(
          sourcePage.items
            .filter((source) => source.role === "PURCHASE_REQUEST")
            .map((source) => source.latest_job_id)
            .filter((jobId): jobId is string => jobId !== null),
        );
        const discoveredJobs = new Map(
          jobPage.items
            .filter(
              (candidate) =>
                ["INGEST", "PARSE"].includes(candidate.type) &&
                (latestProcessingJobIds.size === 0 ||
                  latestProcessingJobIds.has(candidate.id)),
            )
            .map((candidate) => [candidate.id, candidate]),
        );
        const missingJobIds = sourcePage.items
          .filter(
            (source) =>
              source.role === "PURCHASE_REQUEST" &&
              source.latest_result === null &&
              source.latest_job_id !== null &&
              !discoveredJobs.has(source.latest_job_id),
          )
          .map((source) => source.latest_job_id as string);
        const missingJobs = await Promise.all(
          Array.from(new Set(missingJobIds)).map((jobId) => api.getJob(jobId)),
        );
        if (!active || hydrationGenerationRef.current !== generation) return;
        for (const candidate of missingJobs) {
          if (["INGEST", "PARSE"].includes(candidate.type)) {
            discoveredJobs.set(candidate.id, candidate);
          }
        }
        const relevantJobs = Array.from(discoveredJobs.values());
        for (const relevant of relevantJobs) mergeJobResults(relevant);
        const displayJob =
          relevantJobs.find((candidate) => ACTIVE_JOB_STATES.has(candidate.status)) ??
          relevantJobs.find((candidate) =>
            RETRYABLE_TERMINAL_JOB_STATES.has(candidate.status),
          ) ??
          relevantJobs[0];
        if (displayJob) {
          setJob({ ...displayJob, items: Array.from(sourceResultsRef.current.values()) });
        }
        if (mappingStates.length > 0) {
          setMappingQueue(mappingStates);
          setMappingOpen(true);
          setAnnouncement("저장된 열 연결 확인부터 이어갑니다.");
          return;
        }
        if (relevantJobs.length > 0) {
          const isCurrent = () =>
            active && hydrationGenerationRef.current === generation;
          const completedJobs = await Promise.all(
            relevantJobs.map((relevant) =>
              ACTIVE_JOB_STATES.has(relevant.status)
                ? waitForJob(relevant.id, true, isCurrent)
                : Promise.resolve(relevant),
            ),
          );
          if (
            completedJobs.some((completed) => completed === null) ||
            !isCurrent()
          ) {
            return;
          }
          for (const completed of completedJobs) {
            if (!completed) continue;
            mergeJobResults(completed);
          }
          const terminalJobs = completedJobs.filter(
            (completed): completed is Job => completed !== null,
          );
          const aggregateBase =
            terminalJobs.find((completed) =>
              RETRYABLE_TERMINAL_JOB_STATES.has(completed.status),
            ) ?? terminalJobs[0];
          const aggregate = {
            ...aggregateBase,
            items: Array.from(sourceResultsRef.current.values()),
          };
          setJob(aggregate);
          // Hydration deliberately resumes the orchestration function declared below.
          // eslint-disable-next-line react-hooks/immutability
          await finishIngest(aggregate, sourceIdsRef.current);
        }
      })
      .catch((error: unknown) => {
        if (active && hydrationGenerationRef.current === generation) {
          setAnnouncement(errorMessage(error, "저장된 자료 상태를 불러오지 못했습니다."));
        }
      });
    return () => {
      active = false;
    };
    // Workspace identity is the recovery boundary; API implementations are stable per app.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspace.id]);

  async function recheckCurrentJobs() {
    if (recheckJobs.length === 0) return;
    const expectedWorkspaceId = workspace.id;
    const generation = hydrationGenerationRef.current;
    const isCurrent = () => operationIsCurrent(expectedWorkspaceId, generation);
    const purpose = recheckJobs.some((item) => item.purpose === "COMPARE")
      ? "COMPARE"
      : "INGEST";
    const pending = recheckJobs.filter((item) => item.purpose === purpose);
    const pendingIds = new Set(pending.map((item) => item.jobId));
    setRecheckJobs((current) =>
      current.filter((item) => !pendingIds.has(item.jobId)),
    );
    setAnnouncement(
      purpose === "COMPARE"
        ? "현재 도서 비교 상태를 다시 확인하고 있습니다."
        : "현재 자료 읽기 상태를 다시 확인하고 있습니다.",
    );
    if (purpose === "COMPARE") {
      const comparison = pending[0];
      try {
        const current = await api.getJob(comparison.jobId);
        if (!isCurrent()) return;
        setComparisonJobId(current.id);
        if (ACTIVE_JOB_STATES.has(current.status)) {
          const completed = await waitForJob(current.id, false, isCurrent);
          if (isCurrent()) await finishComparison(completed);
        } else if (TERMINAL_JOB_STATES.has(current.status)) {
          await finishComparison(current);
        }
      } catch (error) {
        if (!isCurrent()) return;
        queueRecheck(comparison);
        setAnnouncement(
          errorMessage(error, "현재 도서 비교 상태를 확인하지 못했습니다."),
        );
      }
      return;
    }

    const completedJobs = await Promise.all(
      pending.map(async (item) => {
        try {
          const current = await api.getJob(item.jobId);
          if (!isCurrent()) return null;
          mergeJobResults(current);
          if (ACTIVE_JOB_STATES.has(current.status)) {
            return await waitForJob(current.id, true, isCurrent);
          }
          return TERMINAL_JOB_STATES.has(current.status) ? current : null;
        } catch (error) {
          if (!isCurrent()) return null;
          queueRecheck(item);
          setAnnouncement(
            errorMessage(error, "현재 자료 읽기 상태를 확인하지 못했습니다."),
          );
          return null;
        }
      }),
    );
    if (!isCurrent()) return;
    if (completedJobs.some((completed) => completed === null)) return;
    for (const completed of completedJobs) {
      if (completed) mergeJobResults(completed);
    }
    const aggregate =
      completedJobs.find(
        (completed) =>
          completed !== null &&
          RETRYABLE_TERMINAL_JOB_STATES.has(completed.status),
      ) ?? completedJobs[0];
    if (aggregate) {
      setJob({ ...aggregate, items: Array.from(sourceResultsRef.current.values()) });
      await finishIngest(
        { items: Array.from(sourceResultsRef.current.values()) },
        sourceIdsRef.current,
      );
    } else {
      setAnnouncement(
        "현재 자료 읽기 상태를 확인하지 못했습니다.",
      );
    }
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
      setComparisonRetryable(
        ["FAILED", "PARTIAL", "CANCELLED"].includes(completed.status),
      );
      setAnnouncement("도서 비교를 마치지 못했습니다. 다시 비교해 주세요.");
      setComparisonError(
        completed.error?.message ?? "도서 비교를 마치지 못했습니다. 다시 시도해 주세요.",
      );
      return;
    }
    setComparisonRetryable(false);
    setComparisonError("");
    setComparisonRetryIds([]);
    setComparisonJobId(null);
    await refreshCandidateWorkspace();
  }

  async function reconcileComparisonSources(sourceSetRefreshes: number) {
    const expectedWorkspaceId = workspace.id;
    const generation = hydrationGenerationRef.current;
    const isCurrent = () => operationIsCurrent(expectedWorkspaceId, generation);
    const [sourcePage, repairPage] = await Promise.all([
      api.listSources(expectedWorkspaceId),
      api.listUploadRepairs(expectedWorkspaceId),
    ]);
    if (!isCurrent()) return;
    const purchaseSources = sourcePage.items.filter(
      (source) => source.role === "PURCHASE_REQUEST",
    );
    sourceIdsRef.current = purchaseSources.map((source) => source.id);
    sourceResultsRef.current.clear();
    sourceRolesRef.current.clear();
    sourceJobsRef.current.clear();
    repairGenerationsRef.current.clear();
    repairSourcesRef.current.clear();
    repairRolesRef.current.clear();
    for (const source of sourcePage.items) {
      sourceRolesRef.current.set(source.id, source.role as DocumentRole);
      if (source.latest_result) {
        sourceResultsRef.current.set(source.id, source.latest_result);
      }
      if (source.role === "PURCHASE_REQUEST" && source.latest_job_id !== null) {
        const linked = sourceJobsRef.current.get(source.latest_job_id) ?? [];
        linked.push({ id: source.id, filename: source.filename });
        sourceJobsRef.current.set(source.latest_job_id, linked);
      }
    }
    for (const repair of repairPage.items) {
      repairGenerationsRef.current.set(repair.id, repair.generation);
      repairRolesRef.current.set(repair.id, repair.role as DocumentRole);
      if (repair.resolved_source_id) {
        repairSourcesRef.current.set(repair.id, repair.resolved_source_id);
      }
    }
    setDisplayedSourceRoles(new Map(sourceRolesRef.current));
    setDisplayedRepairRoles(new Map(repairRolesRef.current));
    const discoveredItems: UploadItem[] = [
      ...sourcePage.items.map((source) => ({
        filename: source.filename,
        status: "ACCEPTED" as const,
        source_id: source.id,
        error: null,
        repair_obligation_id: null,
        repair_generation: null,
      })),
      ...repairPage.items.map((repair) => ({
        filename: repair.filename,
        status: "FAILED" as const,
        source_id: null,
        error: repair.error,
        repair_obligation_id: repair.id,
        repair_generation: repair.generation,
      })),
    ];
    uploadItemsRef.current = discoveredItems;
    setUploadItems(discoveredItems);
    if (repairPage.items.length > 0) {
      setComparisonRetryable(false);
      setAnnouncement("읽지 못한 파일을 다시 올린 뒤 모든 자료를 함께 비교해 주세요.");
      return;
    }

    const missingJobIds = purchaseSources
      .filter((source) => source.latest_result === null && source.latest_job_id !== null)
      .map((source) => source.latest_job_id as string);
    const recoveredJobs = await Promise.all(
      Array.from(new Set(missingJobIds)).map(async (jobId) => {
        const current = await api.getJob(jobId);
        if (!isCurrent()) return null;
        return ACTIVE_JOB_STATES.has(current.status)
          ? await waitForJob(current.id, true, isCurrent)
          : current;
      }),
    );
    if (!isCurrent()) return;
    if (recoveredJobs.some((current) => current === null)) return;
    for (const current of recoveredJobs) {
      for (const item of current?.items ?? []) {
        sourceResultsRef.current.set(item.source_document_id, item);
      }
    }
    await finishIngest(
      { items: Array.from(sourceResultsRef.current.values()) },
      sourceIdsRef.current,
      sourceSetRefreshes,
    );
  }

  async function startComparison(sourceIds: string[], sourceSetRefreshes = 0) {
    if (!mountedRef.current || renderedWorkspaceIdRef.current !== workspace.id) return;
    if (sourceIds.length === 0) {
      setAnnouncement("비교할 수 있는 자료가 없습니다. 실패한 파일을 다시 확인해 주세요.");
      return;
    }
    if (comparisonStartedRef.current) return;
    comparisonStartedRef.current = true;
    setComparisonRetryable(false);
    setComparisonRetryIds(sourceIds);
    setComparisonJobId(null);
    setComparing(true);
    setAnnouncement("우리 도서관 장서와 추천자료를 비교하고 있습니다.");
    try {
      const command = await api.createComparisonJob(
        workspace.id,
        sourceIds,
        workspaceVersionRef.current,
      );
      setComparisonJobId(command.job_id);
      await finishComparison(await waitForJob(command.job_id, false));
    } catch (error) {
      comparisonStartedRef.current = false;
      if (
        ["COMPARISON_SOURCE_SET_CHANGED", "UPLOAD_REPAIR_REQUIRED"].includes(
          errorCode(error) ?? "",
        ) &&
        sourceSetRefreshes < 1
      ) {
        setAnnouncement("최신 추천자료를 다시 확인한 뒤 비교를 이어갑니다.");
        try {
          await reconcileComparisonSources(sourceSetRefreshes + 1);
          return;
        } catch (refreshError) {
          if (!mountedRef.current) return;
          setComparisonRetryable(true);
          setAnnouncement(
            errorMessage(
              refreshError,
              "최신 추천자료를 불러오지 못했습니다. 다시 시도해 주세요.",
            ),
          );
          return;
        }
      }
      setComparisonRetryable(true);
      if (!mountedRef.current) return;
      setAnnouncement(errorMessage(error, "도서 비교를 마치지 못했습니다. 다시 시도해 주세요."));
    } finally {
      if (mountedRef.current) setComparing(false);
    }
  }

  async function retryComparison() {
    if (!comparisonJobId) {
      if (comparisonRetryIds.length === 0) return;
      await startComparison(comparisonRetryIds);
      return;
    }
    setComparing(true);
    setComparisonRetryable(false);
    setAnnouncement("도서 비교를 다시 시작했습니다.");
    try {
      const current = await api.getJob(comparisonJobId);
      if (ACTIVE_JOB_STATES.has(current.status)) {
        await finishComparison(await waitForJob(current.id, false));
        return;
      }
      if (current.status === "SUCCEEDED") {
        await finishComparison(current);
        return;
      }
      if (!["FAILED", "PARTIAL", "CANCELLED"].includes(current.status)) {
        setAnnouncement("현재 상태에서는 도서 비교를 다시 시작할 수 없습니다.");
        return;
      }
      const command = await api.retryJob(comparisonJobId);
      await finishComparison(await waitForJob(command.id, false));
    } catch (error) {
      setComparisonRetryable(true);
      setAnnouncement(errorMessage(error, "도서 비교를 다시 시작하지 못했습니다."));
    } finally {
      if (mountedRef.current) setComparing(false);
    }
  }

  async function finishIngest(
    completed: Pick<Job, "items">,
    sourceIds: string[],
    sourceSetRefreshes = 0,
  ) {
    for (const item of completed.items) {
      sourceResultsRef.current.set(item.source_document_id, item);
    }
    const sourceResults = sourceIds
      .map((sourceId) => sourceResultsRef.current.get(sourceId))
      .filter((item): item is JobItem => item !== undefined);
    const mappingItems = Array.from(sourceResultsRef.current.values()).filter(
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
            role: source.data.role as DocumentRole,
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
    if (workspaceStatusRef.current === "ANALYZING") {
      setAnnouncement(
        "읽지 못한 내용이 있으면 수정한 파일로 교체한 뒤 다시 비교해 주세요.",
      );
      return;
    }
    await startComparison(readyIds, sourceSetRefreshes);
  }

  async function runUpload(
    inputs: File[],
    merge: boolean,
    replacedFilename?: string,
    repair?: { obligationId: string; generation: number },
    replacedSourceId?: string,
    forcedRole?: DocumentRole,
  ) {
    const expectedWorkspaceId = workspace.id;
    const generation = hydrationGenerationRef.current;
    const isCurrent = () =>
      operationIsCurrent(expectedWorkspaceId, generation) &&
      (!repair ||
        repairGenerationsRef.current.get(repair.obligationId) === repair.generation);
    const rememberedRepairRole = repair
      ? repairRolesRef.current.get(repair.obligationId)
      : undefined;
    const uploadRole = forcedRole ??
      (repair && rememberedRepairRole && rememberedRepairRole !== "UNKNOWN"
        ? rememberedRepairRole
        : role);
    const response = await api.uploadSources(workspace.id, {
      files: inputs,
      role: uploadRole,
      repairObligationId: repair?.obligationId,
      repairGeneration: repair?.generation,
      replacementSourceId: replacedSourceId,
    });
    if (!isCurrent()) return;
    if (workspaceStatusRef.current === "ANALYZING") {
      const reopened = await api.getWorkspace(workspace.id);
      if (!isCurrent()) return;
      workspaceVersionRef.current = reopened.data.row_version;
      workspaceStatusRef.current = reopened.data.status;
      onWorkspaceChange(reopened.data);
    }
    for (const responseItem of response.items) {
      if (
        responseItem.repair_obligation_id &&
        typeof responseItem.repair_generation === "number"
      ) {
        repairGenerationsRef.current.set(
          responseItem.repair_obligation_id,
          responseItem.repair_generation,
        );
        if (
          !repairRolesRef.current.has(responseItem.repair_obligation_id) ||
          repairRolesRef.current.get(responseItem.repair_obligation_id) === "UNKNOWN"
        ) {
          repairRolesRef.current.set(responseItem.repair_obligation_id, uploadRole);
        }
      }
    }
    setDisplayedRepairRoles(new Map(repairRolesRef.current));
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
    for (const sourceId of acceptedIds) {
      sourceRolesRef.current.set(sourceId, uploadRole);
    }
    setDisplayedSourceRoles(new Map(sourceRolesRef.current));
    const comparisonAcceptedIds =
      uploadRole === "PURCHASE_REQUEST" ? acceptedIds : [];
    if (repair) {
      const previousSource = repairSourcesRef.current.get(repair.obligationId);
      const withoutPrevious = previousSource
        ? sourceIdsRef.current.filter((sourceId) => sourceId !== previousSource)
        : sourceIdsRef.current;
      sourceIdsRef.current = Array.from(
        new Set([...withoutPrevious, ...comparisonAcceptedIds]),
      );
      if (acceptedIds[0]) repairSourcesRef.current.set(repair.obligationId, acceptedIds[0]);
    } else {
      const retainedSourceIds = replacedSourceId
        ? sourceIdsRef.current.filter((sourceId) => sourceId !== replacedSourceId)
        : sourceIdsRef.current;
      sourceIdsRef.current = Array.from(
        new Set([...retainedSourceIds, ...comparisonAcceptedIds]),
      );
    }
    for (const responseItem of response.items) {
      if (responseItem.repair_obligation_id && responseItem.source_id) {
        repairSourcesRef.current.set(responseItem.repair_obligation_id, responseItem.source_id);
      }
    }
    if (!response.job_id || acceptedIds.length === 0) return;
    sourceJobsRef.current.set(
      response.job_id,
      response.items
        .filter(
          (item): item is typeof item & { source_id: string } =>
            item.source_id !== null,
        )
        .map((item) => ({ id: item.source_id, filename: item.filename })),
    );
    const completed = await waitForJob(response.job_id, true, isCurrent);
    if (completed && isCurrent()) await finishIngest(completed, sourceIdsRef.current);
  }

  async function beginUpload() {
    if (!canUpload) return;
    hydrationGenerationRef.current += 1;
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

  async function reupload(
    item: UploadItem,
    sourceFile: File,
    repair: { obligationId: string; generation: number },
  ) {
    try {
      await runUpload([sourceFile], true, item.filename, repair);
    } catch (error) {
      if (repairGenerationsRef.current.get(repair.obligationId) !== repair.generation) return;
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
    if (!item.repair_obligation_id) {
      setAnnouncement("교체할 파일 정보를 다시 불러온 뒤 시도해 주세요.");
      return;
    }
    const generation =
      (repairGenerationsRef.current.get(item.repair_obligation_id) ??
        item.repair_generation ??
        0) + 1;
    repairGenerationsRef.current.set(item.repair_obligation_id, generation);
    setFiles((current) => [
      ...current.filter(
        (file) => file.name !== item.filename && file.name !== replacement.name,
      ),
      replacement,
    ]);
    void reupload(item, replacement, {
      obligationId: item.repair_obligation_id,
      generation,
    });
  }

  async function replaceParsedSource(item: JobItem, replacement: File) {
    const expectedWorkspaceId = workspace.id;
    const generation = hydrationGenerationRef.current;
    const isCurrent = () => operationIsCurrent(expectedWorkspaceId, generation);
    setAnnouncement(`${item.filename} 대신 수정한 파일을 준비하고 있습니다.`);
    try {
      const originalRole =
        parsedReplacementRolesRef.current.get(item.source_document_id) ??
        sourceRolesRef.current.get(item.source_document_id);
      if (!originalRole || originalRole === "UNKNOWN") {
        setAnnouncement("이미 교체된 자료입니다. 최신 자료 상태를 다시 확인해 주세요.");
        return;
      }
      parsedReplacementRolesRef.current.set(item.source_document_id, originalRole);
      await runUpload(
        [replacement],
        true,
        item.filename,
        undefined,
        item.source_document_id,
        originalRole,
      );
    } catch (error) {
      if (!isCurrent()) return;
      setAnnouncement(
        errorMessage(error, "수정한 파일로 교체하지 못했습니다. 다시 시도해 주세요."),
      );
    }
  }

  function chooseParsedReplacement(
    event: ChangeEvent<HTMLInputElement>,
    item: JobItem,
  ) {
    const replacement = event.target.files?.[0];
    event.target.value = "";
    if (
      !replacement ||
      parsedReplacementInFlightRef.current.has(item.source_document_id)
    ) {
      return;
    }
    parsedReplacementInFlightRef.current.add(item.source_document_id);
    setParsedReplacementPending(
      new Set(parsedReplacementInFlightRef.current),
    );
    void replaceParsedSource(item, replacement).finally(() => {
      parsedReplacementInFlightRef.current.delete(item.source_document_id);
      if (mountedRef.current) {
        setParsedReplacementPending(
          new Set(parsedReplacementInFlightRef.current),
        );
      }
    });
  }

  async function reparse(item: JobItem) {
    setAnnouncement(`${item.filename} 파일을 다시 읽고 있습니다.`);
    try {
      const command = await api.parseSource(item.source_document_id);
      sourceJobsRef.current.set(command.job_id, [
        { id: item.source_document_id, filename: item.filename },
      ]);
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
          role: source.data.role as DocumentRole,
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
          role: mapping.role,
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
            {uploadItems.map((item, itemIndex) => {
              const inputKey = item.source_id
                ? `source:${item.source_id}`
                : item.repair_obligation_id
                  ? `repair:${item.repair_obligation_id}`
                  : `file:${itemIndex}:${item.filename}`;
              const detail = item.source_id ? jobItems.get(item.source_id) : undefined;
              const rejected = item.status === "FAILED";
              const parseFailed = detail?.status === "FAILED";
              const readCount = parseFailed ? 0 : (detail?.processed_rows ?? 0);
              const filePercent = detail
                ? TERMINAL_JOB_STATES.has(detail.status) ||
                  ["SUCCESS", "PARTIAL", "FAILED"].includes(detail.status)
                  ? 100
                  : progressPercent(detail.processed_rows, detail.total_rows)
                : 0;
              const needsReview = detail?.mapping_required
                ? Math.max(detail.total_rows - detail.processed_rows, 0)
                  : detail?.status === "PARTIAL"
                  ? Math.max(detail.total_rows - readCount, 0)
                  : 0;
              const statusCopy = rejected
                ? "읽지 못함"
                : detail?.mapping_required
                  ? "열 연결 확인"
                  : parseFailed
                    ? "읽기 실패"
                    : detail?.status === "SUCCESS"
                      ? "읽기 완료"
                      : detail?.status === "PARTIAL"
                        ? "일부 읽기 완료"
                      : "대기 중";
              return (
                <li aria-label={`${item.filename} 처리 상태`} key={inputKey}>
                  <div className="file-status-heading">
                    <strong>{item.filename}</strong>
                    <span className={`status-chip ${rejected || parseFailed ? "status-danger" : ""}`}>
                      {statusCopy}
                    </span>
                  </div>
                  <p>
                    {ROLE_LABELS[
                      (item.source_id
                        ? displayedSourceRoles.get(item.source_id)
                        : item.repair_obligation_id
                          ? displayedRepairRoles.get(item.repair_obligation_id)
                          : undefined) ?? role
                    ]}
                  </p>
                  {rejected ? (
                    <>
                      <p className="error-copy">{item.error?.message}</p>
                      <button
                        aria-label={`${item.filename} 다시 올리기`}
                        className="button button-secondary"
                        onClick={() => replacementInputRefs.current.get(inputKey)?.click()}
                        type="button"
                      >
                        다시 올리기
                      </button>
                      <input
                        aria-label={`${item.filename} 수정한 파일 선택`}
                        className="visually-hidden"
                        onChange={(event) => chooseReplacement(event, item)}
                        ref={(node) => {
                          if (node) replacementInputRefs.current.set(inputKey, node);
                          else replacementInputRefs.current.delete(inputKey);
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
                        {readCount}권 읽음 · 확인 필요 {needsReview}
                      </p>
                      {parseFailed && detail?.error?.message ? (
                        <p className="error-copy">{detail.error.message}</p>
                      ) : null}
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
                      {detail &&
                      ["FAILED", "PARTIAL"].includes(detail.status) &&
                      !detail.mapping_required ? (
                        <>
                          <button
                            aria-label={`${item.filename} 수정한 파일로 교체`}
                            className="button button-secondary"
                            disabled={parsedReplacementPending.has(
                              detail.source_document_id,
                            )}
                            onClick={() =>
                              replacementInputRefs.current.get(inputKey)?.click()
                            }
                            type="button"
                          >
                            {parsedReplacementPending.has(detail.source_document_id)
                              ? "교체 중…"
                              : "수정한 파일로 교체"}
                          </button>
                          <input
                            aria-label={`${item.filename} 수정한 파일 선택`}
                            className="visually-hidden"
                            onChange={(event) => chooseParsedReplacement(event, detail)}
                            ref={(node) => {
                              if (node) replacementInputRefs.current.set(inputKey, node);
                              else replacementInputRefs.current.delete(inputKey);
                            }}
                            tabIndex={-1}
                            type="file"
                          />
                        </>
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
                job.status === "CANCELLED" ||
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
      {comparisonError ? <p role="alert">{comparisonError}</p> : null}
      <div className="primary-action-row">
        {recheckJobs.length > 0 ? (
          <button
            className="button button-secondary"
            onClick={() => void recheckCurrentJobs()}
            type="button"
          >
            {recheckJobs.some((item) => item.purpose === "COMPARE")
              ? "도서 비교 상태 다시 확인"
              : "자료 읽기 상태 다시 확인"}
          </button>
        ) : null}
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
        {comparisonRetryable && (comparisonJobId !== null || comparisonRetryIds.length > 0) ? (
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
