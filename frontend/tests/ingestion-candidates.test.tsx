import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode } from "react";
import { Link, MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, test, vi } from "vitest";

import { App } from "../src/app/App";
import { IngestionPanel } from "../src/features/ingestion/IngestionPanel";
import {
  candidate,
  createFixtureApi,
  FixtureApiError,
  idleJob,
  operator,
  sourceFixture,
  workspace,
} from "../src/test/fixtures";

const originalOnline = Object.getOwnPropertyDescriptor(
  window.navigator,
  "onLine",
);

afterEach(() => {
  if (originalOnline) {
    Object.defineProperty(window.navigator, "onLine", originalOnline);
  } else {
    Object.defineProperty(window.navigator, "onLine", {
      configurable: true,
      value: true,
    });
  }
});

function renderWorkroom(
  api: ReturnType<typeof createFixtureApi>,
  workspaceId: string,
) {
  return render(
    <MemoryRouter initialEntries={[`/workspaces/${workspaceId}`]}>
      <App api={api} />
    </MemoryRouter>,
  );
}

describe("후보 만들기 자료 입력", () => {
  test("새로 연 작업실도 서버의 열 연결 상태와 자료 역할을 찾아 그대로 이어간다", async () => {
    const user = userEvent.setup();
    const draft = workspace("workspace-reload-map", "다시 연 열 연결", "DRAFT");
    const mappingRequired = {
      headers: ["서점 제목", "쓴 이"],
      preview_rows: [["다시 연 책", "글쓴이"]],
      suggested_mapping: { "서점 제목": "title", "쓴 이": "author" },
      required_fields: ["title"],
      confidence: 0,
      questions: ["제목 열을 확인해 주세요."],
    };
    const source = {
      ...sourceFixture,
      id: "source-reload-map",
      filename: "vendor.xlsx",
      role: "VENDOR_QUOTE" as const,
      vendor_scope: "bookstore-a",
      latest_job_id: "ingest-reload-map",
      latest_result: {
        source_document_id: "source-reload-map",
        filename: "vendor.xlsx",
        status: "PARTIAL",
        total_rows: 1,
        processed_rows: 0,
        row_error_count: 0,
        error: { code: "MAPPING_REQUIRED", message: "열 연결 확인", type: null },
        mapping_required: mappingRequired,
      },
    };
    const mappings: Array<{ role: string; vendorScope: string; version: number }> = [];
    const api = createFixtureApi({
      getWorkspace: async () => ({ data: draft, etag: '"1"' }),
      getSource: async () => ({ data: source, etag: '"1"' }),
      listSources: async () => ({ items: [source], next_cursor: null }),
      listWorkspaceJobs: async () => ({
        items: [{ ...idleJob, id: "ingest-reload-map", status: "PARTIAL", items: [source.latest_result] }],
        next_cursor: null,
      }),
      updateSourceMapping: async (_sourceId, input, version) => {
        mappings.push({ role: input.role, vendorScope: input.vendor_scope, version });
        return { data: { ...source, role: input.role, row_version: 2 }, etag: '"2"' };
      },
      parseSource: async () => ({ job_id: "parse-reload-map", status: "QUEUED" }),
      getJob: async () => ({
        ...idleJob,
        id: "parse-reload-map",
        type: "PARSE",
        items: [{ ...source.latest_result, status: "SUCCESS", processed_rows: 1, row_error_count: 0, error: null, mapping_required: null }],
      }),
    });
    renderWorkroom(api, draft.id);

    const dialog = await screen.findByRole("dialog", { name: "열 연결 확인" });
    expect(within(dialog).getByText("다시 연 책")).toBeVisible();
    await user.click(within(dialog).getByRole("button", { name: "열 연결 적용" }));
    await waitFor(() =>
      expect(mappings).toEqual([
        { role: "VENDOR_QUOTE", vendorScope: "bookstore-a", version: 1 },
      ]),
    );
  });

  test("열 연결이 필요한 자료가 있어도 함께 진행 중인 다른 자료를 계속 확인한다", async () => {
    const draft = workspace("workspace-mixed-hydration", "열 연결과 진행 작업", "DRAFT");
    const mappingRequired = {
      headers: ["책 열"],
      preview_rows: [["확인할 책"]],
      suggested_mapping: { "책 열": "title" },
      required_fields: ["title"],
      confidence: 0,
      questions: ["제목 열을 확인해 주세요."],
    };
    const mappingResult = {
      source_document_id: "source-needs-mapping",
      filename: "mapping.xlsx",
      status: "PARTIAL",
      total_rows: 1,
      processed_rows: 0,
      row_error_count: 0,
      error: { code: "MAPPING_REQUIRED", message: "열 연결 확인", type: null },
      mapping_required: mappingRequired,
    };
    const runningResult = {
      source_document_id: "source-still-running",
      filename: "running.csv",
      status: "SUCCESS",
      total_rows: 1,
      processed_rows: 1,
      row_error_count: 0,
      error: null,
      mapping_required: null,
    };
    const jobReads: string[] = [];
    const api = createFixtureApi({
      listSources: async () => ({
        items: [
          {
            ...sourceFixture,
            id: "source-needs-mapping",
            filename: "mapping.xlsx",
            latest_job_id: "job-needs-mapping",
            latest_result: mappingResult,
          },
          {
            ...sourceFixture,
            id: "source-still-running",
            filename: "running.csv",
            latest_job_id: "job-still-running",
            latest_result: null,
          },
        ],
        next_cursor: null,
      }),
      listWorkspaceJobs: async () => ({
        items: [
          {
            ...idleJob,
            id: "job-needs-mapping",
            status: "PARTIAL",
            items: [mappingResult],
          },
          {
            ...idleJob,
            id: "job-still-running",
            status: "RUNNING",
            items: [],
          },
        ],
        next_cursor: null,
      }),
      getJob: async (jobId) => {
        jobReads.push(jobId);
        return {
          ...idleJob,
          id: jobId,
          status: "SUCCEEDED",
          items: [runningResult],
        };
      },
    });

    render(<IngestionPanel api={api} workspace={draft} />);

    expect(await screen.findByRole("dialog", { name: "열 연결 확인" })).toBeVisible();
    await waitFor(() => expect(jobReads).toEqual(["job-still-running"]));
  });

  test("새로 연 작업실의 종료된 여러 자료 결과를 합쳐 비교를 이어간다", async () => {
    const draft = workspace("workspace-reload-ready", "다시 연 자료", "DRAFT");
    const reviewed = { ...draft, status: "CANDIDATE_REVIEW", row_version: 3 };
    const result = (sourceId: string, filename: string) => ({
      source_document_id: sourceId,
      filename,
      status: "SUCCESS",
      total_rows: 1,
      processed_rows: 1,
      row_error_count: 0,
      error: null,
      mapping_required: null,
    });
    const firstResult = result("source-reload-first", "first.csv");
    const secondResult = result("source-reload-second", "second.csv");
    const comparisons: string[][] = [];
    const changed: string[] = [];
    const api = createFixtureApi({
      listSources: async () => ({
        items: [
          {
            ...sourceFixture,
            id: "source-reload-first",
            filename: "first.csv",
            latest_job_id: "ingest-reload-ready",
            latest_result: firstResult,
          },
          {
            ...sourceFixture,
            id: "source-reload-second",
            filename: "second.csv",
            latest_job_id: "parse-reload-second",
            latest_result: secondResult,
          },
        ],
        next_cursor: null,
      }),
      listWorkspaceJobs: async () => ({
        items: [
          {
            ...idleJob,
            id: "ingest-reload-ready",
            status: "SUCCEEDED",
            items: [firstResult],
          },
        ],
        next_cursor: null,
      }),
      createComparisonJob: async (_workspaceId, sourceIds) => {
        comparisons.push(sourceIds);
        return {
          job_id: "compare-reload-ready",
          status: "QUEUED",
          workspace_status: "ANALYZING",
          row_version: 2,
        };
      },
      getJob: async (jobId) => ({
        ...idleJob,
        id: jobId,
        type: "COMPARE",
        status: "SUCCEEDED",
        items: [],
      }),
      getWorkspace: async () => ({ data: reviewed, etag: '"3"' }),
    });

    render(
      <IngestionPanel
        api={api}
        onWorkspaceChange={(next) => changed.push(next.status)}
        workspace={draft}
      />,
    );

    await waitFor(() =>
      expect(comparisons).toEqual([
        ["source-reload-first", "source-reload-second"],
      ]),
    );
    expect(changed).toEqual(["CANDIDATE_REVIEW"]);
    expect(screen.getByRole("listitem", { name: "second.csv 처리 상태" })).toHaveTextContent(
      "읽기 완료",
    );
  });

  test("비교 직전 자료 구성이 바뀌면 서버의 최신 추천자료를 다시 찾아 모두 비교한다", async () => {
    const draft = workspace("workspace-source-set-refresh", "자료 구성 다시 확인", "DRAFT");
    const reviewed = { ...draft, status: "CANDIDATE_REVIEW", row_version: 3 };
    const result = (sourceId: string, filename: string) => ({
      source_document_id: sourceId,
      filename,
      status: "SUCCESS" as const,
      total_rows: 1,
      processed_rows: 1,
      row_error_count: 0,
      error: null,
      mapping_required: null,
    });
    const olderResult = result("source-set-older", "older.csv");
    const replacementResult = result("source-set-replacement", "replacement.csv");
    let sourceReads = 0;
    const comparisons: string[][] = [];
    const changed: string[] = [];
    const source = (id: string, filename: string, latestResult: typeof olderResult) => ({
      ...sourceFixture,
      id,
      filename,
      status: "SUCCESS" as const,
      latest_job_id: "ingest-source-set",
      latest_result: latestResult,
    });
    const api = createFixtureApi({
      listSources: async () => {
        sourceReads += 1;
        return {
          items:
            sourceReads === 1
              ? [source("source-set-older", "older.csv", olderResult)]
              : [
                  source("source-set-older", "older.csv", olderResult),
                  source("source-set-replacement", "replacement.csv", replacementResult),
                ],
          next_cursor: null,
        };
      },
      listWorkspaceJobs: async () => ({
        items: [
          {
            ...idleJob,
            id: "ingest-source-set",
            status: "SUCCEEDED",
            items: [olderResult],
          },
        ],
        next_cursor: null,
      }),
      createComparisonJob: async (_workspaceId, sourceIds) => {
        comparisons.push(sourceIds);
        if (comparisons.length === 1) {
          throw new FixtureApiError(
            409,
            "COMPARISON_SOURCE_SET_CHANGED",
            "추천자료 구성이 바뀌었습니다.",
          );
        }
        return {
          job_id: "compare-source-set-refreshed",
          status: "QUEUED",
          workspace_status: "ANALYZING",
          row_version: 2,
        };
      },
      getJob: async (jobId) => ({
        ...idleJob,
        id: jobId,
        type: "COMPARE",
        status: "SUCCEEDED",
        items: [],
      }),
      getWorkspace: async () => ({ data: reviewed, etag: '"3"' }),
    });

    render(
      <IngestionPanel
        api={api}
        onWorkspaceChange={(next) => changed.push(next.status)}
        workspace={draft}
      />,
    );

    await waitFor(() =>
      expect(comparisons).toEqual([
        ["source-set-older"],
        ["source-set-older", "source-set-replacement"],
      ]),
    );
    expect(changed).toEqual(["CANDIDATE_REVIEW"]);
  });

  test("최신 자료를 다시 찾는 동안 화면을 떠나면 이전 작업의 비교를 시작하지 않는다", async () => {
    const draft = workspace("workspace-source-refresh-unmount", "떠난 자료 구성", "DRAFT");
    const readyResult = {
      source_document_id: "source-before-unmount",
      filename: "before.csv",
      status: "SUCCESS" as const,
      total_rows: 1,
      processed_rows: 1,
      row_error_count: 0,
      error: null,
      mapping_required: null,
    };
    type SourcePage = Awaited<
      ReturnType<ReturnType<typeof createFixtureApi>["listSources"]>
    >;
    let resolveRefresh!: (page: SourcePage) => void;
    const refreshedSources = new Promise<SourcePage>((resolve) => {
      resolveRefresh = resolve;
    });
    let sourceReads = 0;
    const comparisons: string[][] = [];
    const initialSource = {
      ...sourceFixture,
      id: "source-before-unmount",
      filename: "before.csv",
      status: "SUCCESS" as const,
      latest_job_id: "ingest-before-unmount",
      latest_result: readyResult,
    };
    const api = createFixtureApi({
      listSources: async () => {
        sourceReads += 1;
        if (sourceReads === 1) {
          return { items: [initialSource], next_cursor: null };
        }
        return await refreshedSources;
      },
      listWorkspaceJobs: async () => ({
        items: [
          {
            ...idleJob,
            id: "ingest-before-unmount",
            status: "SUCCEEDED",
            items: [readyResult],
          },
        ],
        next_cursor: null,
      }),
      createComparisonJob: async (_workspaceId, sourceIds) => {
        comparisons.push(sourceIds);
        if (comparisons.length === 1) {
          throw new FixtureApiError(
            409,
            "COMPARISON_SOURCE_SET_CHANGED",
            "추천자료 구성이 바뀌었습니다.",
          );
        }
        return {
          job_id: "compare-after-unmount",
          status: "QUEUED",
          workspace_status: "ANALYZING",
          row_version: 2,
        };
      },
    });
    const view = render(<IngestionPanel api={api} workspace={draft} />);
    await waitFor(() => expect(sourceReads).toBe(2));
    expect(comparisons).toEqual([["source-before-unmount"]]);

    view.unmount();
    await act(async () => {
      resolveRefresh({ items: [initialSource], next_cursor: null });
      await refreshedSources;
    });

    expect(comparisons).toEqual([["source-before-unmount"]]);
  });

  test("비교 직전에 다른 창이 만든 실패 파일을 다시 찾아 교체 행동을 보여준다", async () => {
    const draft = workspace("workspace-concurrent-repair", "새 실패 파일 발견", "DRAFT");
    const readyResult = {
      source_document_id: "source-before-repair",
      filename: "ready.csv",
      status: "SUCCESS" as const,
      total_rows: 1,
      processed_rows: 1,
      row_error_count: 0,
      error: null,
      mapping_required: null,
    };
    let repairReads = 0;
    const comparisons: string[][] = [];
    const readySource = {
      ...sourceFixture,
      id: "source-before-repair",
      filename: "ready.csv",
      status: "SUCCESS" as const,
      latest_job_id: "ingest-before-repair",
      latest_result: readyResult,
    };
    const api = createFixtureApi({
      listSources: async () => ({ items: [readySource], next_cursor: null }),
      listUploadRepairs: async () => {
        repairReads += 1;
        return {
          items:
            repairReads === 1
              ? []
              : [
                  {
                    id: "repair-from-other-window",
                    filename: "other-window.exe",
                    error: {
                      code: "UNSUPPORTED_FILE_TYPE",
                      message: "지원하지 않는 파일 형식입니다.",
                    },
                    status: "UNRESOLVED" as const,
                    generation: 0,
                    role: "PURCHASE_REQUEST" as const,
                    vendor_scope: "*",
                    requested_start_local_date: null,
                    requested_through_local_date: null,
                    configuration_confirmation_required: false,
                    resolved_source_id: null,
                    created_at: "2026-08-29T00:00:00Z",
                    updated_at: "2026-08-29T00:00:00Z",
                  },
                ],
          next_cursor: null,
        };
      },
      listWorkspaceJobs: async () => ({
        items: [
          {
            ...idleJob,
            id: "ingest-before-repair",
            status: "SUCCEEDED",
            items: [readyResult],
          },
        ],
        next_cursor: null,
      }),
      createComparisonJob: async (_workspaceId, sourceIds) => {
        comparisons.push(sourceIds);
        throw new FixtureApiError(
          409,
          "UPLOAD_REPAIR_REQUIRED",
          "읽지 못한 파일을 다시 올린 뒤 계속해 주세요.",
        );
      },
    });

    render(<IngestionPanel api={api} workspace={draft} />);

    const failed = await screen.findByRole("listitem", {
      name: "other-window.exe 처리 상태",
    });
    expect(failed).toHaveTextContent("지원하지 않는 파일 형식입니다.");
    expect(screen.getByLabelText("other-window.exe 수정한 파일 선택")).toBeInTheDocument();
    expect(repairReads).toBe(2);
    expect(comparisons).toEqual([["source-before-repair"]]);
  });

  test("새로 연 작업실의 실행 중인 여러 자료 작업을 모두 기다린 뒤 한 번만 비교한다", async () => {
    const draft = workspace("workspace-reload-running", "진행 중인 여러 자료", "DRAFT");
    const result = (sourceId: string, filename: string) => ({
      source_document_id: sourceId,
      filename,
      status: "SUCCESS",
      total_rows: 1,
      processed_rows: 1,
      row_error_count: 0,
      error: null,
      mapping_required: null,
    });
    const firstResult = result("source-running-first", "first.csv");
    const secondResult = result("source-running-second", "second.csv");
    const comparisons: string[][] = [];
    const api = createFixtureApi({
      listSources: async () => ({
        items: [
          {
            ...sourceFixture,
            id: "source-running-first",
            filename: "first.csv",
            latest_job_id: "parse-running-first",
            latest_result: null,
          },
          {
            ...sourceFixture,
            id: "source-running-second",
            filename: "second.csv",
            latest_job_id: "parse-running-second",
            latest_result: null,
          },
        ],
        next_cursor: null,
      }),
      listWorkspaceJobs: async () => ({
        items: [
          {
            ...idleJob,
            id: "parse-running-first",
            type: "PARSE",
            status: "RUNNING",
            items: [],
          },
          {
            ...idleJob,
            id: "parse-running-second",
            type: "PARSE",
            status: "RUNNING",
            items: [],
          },
        ],
        next_cursor: null,
      }),
      getJob: async (jobId) => {
        if (jobId === "parse-running-first") {
          return {
            ...idleJob,
            id: jobId,
            type: "PARSE",
            status: "SUCCEEDED",
            items: [firstResult],
          };
        }
        if (jobId === "parse-running-second") {
          return {
            ...idleJob,
            id: jobId,
            type: "PARSE",
            status: "SUCCEEDED",
            items: [secondResult],
          };
        }
        return { ...idleJob, id: jobId, type: "COMPARE", status: "SUCCEEDED", items: [] };
      },
      createComparisonJob: async (_workspaceId, sourceIds) => {
        comparisons.push(sourceIds);
        return {
          job_id: "compare-after-running",
          status: "QUEUED",
          workspace_status: "ANALYZING",
          row_version: 2,
        };
      },
    });

    render(<IngestionPanel api={api} workspace={draft} />);

    await waitFor(() =>
      expect(comparisons).toEqual([
        ["source-running-first", "source-running-second"],
      ]),
    );
  });

  test("여러 자료 작업 중 결과 없는 실패 작업도 파일 실패와 올바른 재시도로 복원한다", async () => {
    const user = userEvent.setup();
    const draft = workspace("workspace-empty-failed-job", "결과 없는 실패 복구", "DRAFT");
    const successResult = {
      source_document_id: "source-job-success",
      filename: "success.csv",
      status: "SUCCESS" as const,
      total_rows: 1,
      processed_rows: 1,
      row_error_count: 0,
      error: null,
      mapping_required: null,
    };
    const recoveredResult = {
      source_document_id: "source-job-failed",
      filename: "failed.csv",
      status: "SUCCESS" as const,
      total_rows: 1,
      processed_rows: 1,
      row_error_count: 0,
      error: null,
      mapping_required: null,
    };
    const failedJob = {
      ...idleJob,
      id: "parse-empty-failed",
      type: "PARSE" as const,
      status: "FAILED" as const,
      error: {
        code: "JOB_FAILED",
        message: "작업을 처리하지 못했습니다. 다시 시도해 주세요.",
        type: null,
      },
      items: [],
    };
    const retries: string[] = [];
    const comparisons: string[][] = [];
    const api = createFixtureApi({
      listSources: async () => ({
        items: [
          {
            ...sourceFixture,
            id: "source-job-success",
            filename: "success.csv",
            status: "SUCCESS",
            latest_job_id: "parse-job-success",
            latest_result: successResult,
          },
          {
            ...sourceFixture,
            id: "source-job-failed",
            filename: "failed.csv",
            status: "FAILED",
            latest_job_id: failedJob.id,
            latest_result: null,
          },
        ],
        next_cursor: null,
      }),
      listWorkspaceJobs: async () => ({
        items: [
          {
            ...idleJob,
            id: "parse-job-success",
            type: "PARSE",
            status: "SUCCEEDED",
            items: [successResult],
          },
          failedJob,
        ],
        next_cursor: null,
      }),
      retryJob: async (jobId) => {
        retries.push(jobId);
        return { ...failedJob, id: jobId, status: "QUEUED", error: null };
      },
      getJob: async (jobId) => {
        if (jobId === failedJob.id) {
          return {
            ...failedJob,
            status: "SUCCEEDED",
            error: null,
            items: [recoveredResult],
          };
        }
        return { ...idleJob, id: jobId, type: "COMPARE", status: "SUCCEEDED" };
      },
      createComparisonJob: async (_workspaceId, sourceIds) => {
        comparisons.push(sourceIds);
        return {
          job_id: "compare-after-empty-failure",
          status: "QUEUED",
          workspace_status: "ANALYZING",
          row_version: 2,
        };
      },
    });

    render(<IngestionPanel api={api} workspace={draft} />);

    const failedFile = await screen.findByRole("listitem", {
      name: "failed.csv 처리 상태",
    });
    expect(failedFile).toHaveTextContent("읽기 실패");
    expect(failedFile).toHaveTextContent("0권 읽음");
    await user.click(screen.getByRole("button", { name: "자료 읽기 전체 다시 시도" }));
    await waitFor(() => expect(retries).toEqual([failedJob.id]));
    await waitFor(() =>
      expect(comparisons).toEqual([
        ["source-job-success", "source-job-failed"],
      ]),
    );
  });

  test("새로 연 작업실도 다시 올릴 파일을 찾아 올바른 복구 세대로 이어간다", async () => {
    const user = userEvent.setup();
    const draft = workspace("workspace-repair-reload", "다시 올릴 파일", "DRAFT");
    const repairInputs: Array<{
      obligation?: string;
      generation?: number;
      role: string;
      confirmed?: boolean;
    }> = [];
    const comparisons: string[][] = [];
    const api = createFixtureApi({
      listUploadRepairs: async () => ({
        items: [
          {
            id: "repair-reloaded",
            filename: "broken.exe",
            error: {
              code: "UNSUPPORTED_FILE_TYPE",
              message: "지원하지 않는 파일 형식입니다.",
            },
            status: "UNRESOLVED",
            generation: 0,
            role: "UNKNOWN",
            vendor_scope: "*",
            requested_start_local_date: null,
            requested_through_local_date: null,
            configuration_confirmation_required: true,
            resolved_source_id: null,
            created_at: "2026-08-29T00:00:00Z",
            updated_at: "2026-08-29T00:00:00Z",
          },
        ],
        next_cursor: null,
      }),
      uploadSources: async (_workspaceId, input) => {
        repairInputs.push({
          obligation: input.repairObligationId,
          generation: input.repairGeneration,
          role: input.role,
          confirmed: input.confirmRepairConfiguration,
        });
        return {
          job_id: "parse-repaired-reload",
          items: [
            {
              filename: "fixed.csv",
              status: "ACCEPTED",
              source_id: "source-repaired-reload",
              error: null,
              repair_obligation_id: "repair-reloaded",
              repair_generation: 1,
            },
          ],
        };
      },
      getJob: async (jobId) => ({
        ...idleJob,
        id: jobId,
        status: "SUCCEEDED",
        items: [
          {
            source_document_id: "source-repaired-reload",
            filename: "fixed.csv",
            status: "SUCCESS",
            total_rows: 1,
            processed_rows: 1,
            row_error_count: 0,
            error: null,
            mapping_required: null,
          },
        ],
      }),
      createComparisonJob: async (_workspaceId, sourceIds) => {
        comparisons.push(sourceIds);
        return {
          job_id: "compare-repaired-reload",
          status: "QUEUED",
          workspace_status: "ANALYZING",
          row_version: 2,
        };
      },
    });
    render(<IngestionPanel api={api} workspace={draft} />);

    const failed = await screen.findByRole("listitem", { name: "broken.exe 처리 상태" });
    expect(failed).toHaveTextContent("지원하지 않는 파일 형식입니다.");
    await user.selectOptions(
      screen.getByRole("combobox", { name: "broken.exe 자료 종류 확인" }),
      "PURCHASE_REQUEST",
    );
    await user.upload(
      screen.getByLabelText("broken.exe 수정한 파일 선택"),
      new File(["title\nfixed\n"], "fixed.csv"),
    );

    await waitFor(() =>
      expect(repairInputs).toEqual([
        {
          obligation: "repair-reloaded",
          generation: 1,
          role: "PURCHASE_REQUEST",
          confirmed: true,
        },
      ]),
    );
    expect(comparisons).toEqual([["source-repaired-reload"]]);
  });

  test("견적서 교체 파일은 추천자료 역할로 바꾸지 않고 비교 대상에도 넣지 않는다", async () => {
    const user = userEvent.setup();
    const draft = workspace("workspace-vendor-repair", "견적서 교체", "DRAFT");
    const purchaseResult = {
      source_document_id: "source-purchase-ready",
      filename: "request.csv",
      status: "SUCCESS" as const,
      total_rows: 1,
      processed_rows: 1,
      row_error_count: 0,
      error: null,
      mapping_required: null,
    };
    const uploadedRoles: string[] = [];
    const mappingScopes: string[] = [];
    const comparisons: string[][] = [];
    const mappingRequired = {
      headers: ["서점 제목"],
      preview_rows: [["견적 책"]],
      suggested_mapping: { "서점 제목": "title" },
      required_fields: ["title"],
      confidence: 0,
      questions: ["제목 열을 확인해 주세요."],
    };
    const vendorMappingResult = {
      source_document_id: "source-vendor-repaired",
      filename: "quote-fixed.xlsx",
      status: "PARTIAL",
      total_rows: 1,
      processed_rows: 0,
      row_error_count: 0,
      error: { code: "MAPPING_REQUIRED", message: "열 연결 확인", type: null },
      mapping_required: mappingRequired,
    };
    const repairedVendorSource = {
      ...sourceFixture,
      id: "source-vendor-repaired",
      filename: "quote-fixed.xlsx",
      role: "VENDOR_QUOTE" as const,
      vendor_scope: "bookstore-a",
      latest_job_id: "parse-vendor-repair",
      latest_result: vendorMappingResult,
    };
    const api = createFixtureApi({
      listSources: async () => ({
        items: [
          {
            ...sourceFixture,
            id: "source-purchase-ready",
            filename: "request.csv",
            status: "SUCCESS",
            latest_job_id: null,
            latest_result: purchaseResult,
          },
        ],
        next_cursor: null,
      }),
      listUploadRepairs: async () => ({
        items: [
          {
            id: "repair-vendor",
            filename: "quote-broken.xlsx",
            error: { code: "PARSER_FAILURE", message: "파일 내용을 읽지 못했습니다." },
            status: "UNRESOLVED",
            generation: 0,
            role: "VENDOR_QUOTE",
            vendor_scope: "bookstore-a",
            requested_start_local_date: null,
            requested_through_local_date: null,
            configuration_confirmation_required: false,
            resolved_source_id: null,
            created_at: "2026-08-29T00:00:00Z",
            updated_at: "2026-08-29T00:00:00Z",
          },
        ],
        next_cursor: null,
      }),
      uploadSources: async (_workspaceId, input) => {
        uploadedRoles.push(input.role);
        return {
          job_id: "parse-vendor-repair",
          items: [
            {
              filename: "quote-fixed.xlsx",
              status: "ACCEPTED",
              source_id: "source-vendor-repaired",
              error: null,
              repair_obligation_id: "repair-vendor",
              repair_generation: 1,
            },
          ],
        };
      },
      getSource: async () => ({ data: repairedVendorSource, etag: '"1"' }),
      updateSourceMapping: async (_sourceId, input, version) => {
        mappingScopes.push(input.vendor_scope);
        return {
          data: {
            ...repairedVendorSource,
            mapping: input.mapping ?? {},
            row_version: version + 1,
          },
          etag: '"2"',
        };
      },
      parseSource: async () => ({
        job_id: "parse-vendor-remapped",
        status: "QUEUED",
      }),
      getJob: async (jobId) => ({
        ...idleJob,
        id: jobId,
        type: jobId.startsWith("parse-") ? "PARSE" : "COMPARE",
        status: jobId === "parse-vendor-repair" ? "PARTIAL" : "SUCCEEDED",
        items:
          jobId === "parse-vendor-repair"
            ? [vendorMappingResult]
            : jobId === "parse-vendor-remapped"
              ? [
                  {
                    ...vendorMappingResult,
                    status: "SUCCESS",
                    processed_rows: 1,
                    error: null,
                    mapping_required: null,
                  },
                ]
              : [],
      }),
      createComparisonJob: async (_workspaceId, sourceIds) => {
        comparisons.push(sourceIds);
        return {
          job_id: "compare-after-vendor-repair",
          status: "QUEUED",
          workspace_status: "ANALYZING",
          row_version: 2,
        };
      },
    });
    render(<IngestionPanel api={api} workspace={draft} />);

    await user.upload(
      await screen.findByLabelText("quote-broken.xlsx 수정한 파일 선택"),
      new File(["binary"], "quote-fixed.xlsx"),
    );

    const dialog = await screen.findByRole("dialog", { name: "열 연결 확인" });
    await user.click(within(dialog).getByRole("button", { name: "열 연결 적용" }));

    await waitFor(() => expect(uploadedRoles).toEqual(["VENDOR_QUOTE"]));
    await waitFor(() => expect(mappingScopes).toEqual(["bookstore-a"]));
    await waitFor(() => expect(comparisons).toEqual([["source-purchase-ready"]]));
  });

  test("다시 연 두 자료의 폴링이 모두 멈춰도 한 번의 상태 확인으로 둘 다 이어간다", async () => {
    const user = userEvent.setup();
    const draft = workspace("workspace-multi-recheck", "여러 자료 상태 재확인", "DRAFT");
    const reads = new Map<string, number>();
    const comparisons: string[][] = [];
    const result = (sourceId: string, filename: string) => ({
      source_document_id: sourceId,
      filename,
      status: "SUCCESS" as const,
      total_rows: 1,
      processed_rows: 1,
      row_error_count: 0,
      error: null,
      mapping_required: null,
    });
    const sources = [
      {
        ...sourceFixture,
        id: "source-recheck-first",
        filename: "first.csv",
        latest_job_id: "parse-recheck-first",
        latest_result: null,
      },
      {
        ...sourceFixture,
        id: "source-recheck-second",
        filename: "second.csv",
        latest_job_id: "parse-recheck-second",
        latest_result: null,
      },
    ];
    const runningJobs = sources.map((source) => ({
      ...idleJob,
      id: source.latest_job_id,
      type: "PARSE" as const,
      status: "RUNNING" as const,
      items: [],
    }));
    const api = createFixtureApi({
      listSources: async () => ({ items: sources, next_cursor: null }),
      listWorkspaceJobs: async () => ({ items: runningJobs, next_cursor: null }),
      getJob: async (jobId) => {
        const count = (reads.get(jobId) ?? 0) + 1;
        reads.set(jobId, count);
        if (count <= 5) throw new TypeError(`temporary ${jobId} failure`);
        const first = jobId.endsWith("first");
        return {
          ...idleJob,
          id: jobId,
          type: "PARSE",
          status: "SUCCEEDED",
          items: [
            result(
              first ? "source-recheck-first" : "source-recheck-second",
              first ? "first.csv" : "second.csv",
            ),
          ],
        };
      },
      createComparisonJob: async (_workspaceId, sourceIds) => {
        comparisons.push(sourceIds);
        return {
          job_id: "compare-after-multi-recheck",
          status: "QUEUED",
          workspace_status: "ANALYZING",
          row_version: 2,
        };
      },
    });
    render(<IngestionPanel api={api} workspace={draft} />);

    const recheck = await screen.findByRole(
      "button",
      { name: "자료 읽기 상태 다시 확인" },
      { timeout: 10_000 },
    );
    await waitFor(() => {
      expect(reads.get("parse-recheck-first")).toBe(5);
      expect(reads.get("parse-recheck-second")).toBe(5);
    });
    await user.click(recheck);

    await waitFor(() =>
      expect(comparisons).toEqual([
        ["source-recheck-first", "source-recheck-second"],
      ]),
    );
    expect(reads.get("parse-recheck-first")).toBe(6);
    expect(reads.get("parse-recheck-second")).toBe(6);
  }, 12_000);

  test("여러 자료 재확인에서 결과가 비어 있는 실패 작업도 해당 파일에 남겨 다시 읽는다", async () => {
    const user = userEvent.setup();
    const draft = workspace("workspace-mixed-recheck", "혼합 자료 상태 재확인", "DRAFT");
    const reads = new Map<string, number>();
    const retries: string[] = [];
    const comparisons: string[][] = [];
    const successResult = (sourceId: string, filename: string) => ({
      source_document_id: sourceId,
      filename,
      status: "SUCCESS" as const,
      total_rows: 1,
      processed_rows: 1,
      row_error_count: 0,
      error: null,
      mapping_required: null,
    });
    const sources = [
      {
        ...sourceFixture,
        id: "source-mixed-first",
        filename: "first.csv",
        latest_job_id: "parse-mixed-first",
        latest_result: null,
      },
      {
        ...sourceFixture,
        id: "source-mixed-second",
        filename: "second.csv",
        latest_job_id: "parse-mixed-second",
        latest_result: null,
      },
    ];
    const runningJobs = sources.map((source) => ({
      ...idleJob,
      id: source.latest_job_id,
      type: "PARSE" as const,
      status: "RUNNING" as const,
      items: [],
    }));
    const api = createFixtureApi({
      listSources: async () => ({ items: sources, next_cursor: null }),
      listWorkspaceJobs: async () => ({ items: runningJobs, next_cursor: null }),
      getJob: async (jobId) => {
        const count = (reads.get(jobId) ?? 0) + 1;
        reads.set(jobId, count);
        if (count <= 5) throw new TypeError(`temporary ${jobId} failure`);
        if (jobId === "parse-mixed-first") {
          return {
            ...idleJob,
            id: jobId,
            type: "PARSE",
            status: "SUCCEEDED",
            items: [successResult("source-mixed-first", "first.csv")],
          };
        }
        if (count === 6) {
          return {
            ...idleJob,
            id: jobId,
            type: "PARSE",
            status: "FAILED",
            stage: "FAILED",
            error: {
              code: "JOB_FAILED",
              message: "second.csv 파일 내용을 읽지 못했습니다.",
              type: null,
            },
            items: [],
          };
        }
        return {
          ...idleJob,
          id: jobId,
          type: "PARSE",
          status: "SUCCEEDED",
          items: [successResult("source-mixed-second", "second.csv")],
        };
      },
      retryJob: async (jobId) => {
        retries.push(jobId);
        return {
          ...idleJob,
          id: jobId,
          type: "PARSE",
          status: "QUEUED",
          items: [],
        };
      },
      createComparisonJob: async (_workspaceId, sourceIds) => {
        comparisons.push(sourceIds);
        return {
          job_id: "compare-after-mixed-recheck",
          status: "QUEUED",
          workspace_status: "ANALYZING",
          row_version: 2,
        };
      },
    });
    render(<IngestionPanel api={api} workspace={draft} />);

    const recheck = await screen.findByRole(
      "button",
      { name: "자료 읽기 상태 다시 확인" },
      { timeout: 10_000 },
    );
    await user.click(recheck);

    const failedRow = await screen.findByRole("listitem", {
      name: "second.csv 처리 상태",
    });
    expect(failedRow).toHaveTextContent("읽기 실패");
    expect(failedRow).toHaveTextContent("0권 읽음");
    expect(failedRow).toHaveTextContent("second.csv 파일 내용을 읽지 못했습니다.");
    expect(comparisons).toEqual([]);
    await user.click(
      screen.getByRole("button", { name: "자료 읽기 전체 다시 시도" }),
    );
    await waitFor(() =>
      expect(comparisons).toEqual([
        ["source-mixed-first", "source-mixed-second"],
      ]),
    );
    expect(retries).toEqual(["parse-mixed-second"]);
  }, 15_000);

  test("자료 상태를 다시 확인하는 동안 화면을 떠나면 이전 작업의 비교를 시작하지 않는다", async () => {
    const user = userEvent.setup();
    const draft = workspace("workspace-recheck-unmount", "떠난 상태 확인", "DRAFT");
    const result = {
      source_document_id: "source-recheck-unmount",
      filename: "leaving.csv",
      status: "SUCCESS" as const,
      total_rows: 1,
      processed_rows: 1,
      row_error_count: 0,
      error: null,
      mapping_required: null,
    };
    type JobResponse = Awaited<
      ReturnType<ReturnType<typeof createFixtureApi>["getJob"]>
    >;
    let resolveRecheck!: (job: JobResponse) => void;
    const recheckedJob = new Promise<JobResponse>((resolve) => {
      resolveRecheck = resolve;
    });
    let reads = 0;
    const comparisons: string[][] = [];
    const running = {
      ...idleJob,
      id: "parse-recheck-unmount",
      type: "PARSE" as const,
      status: "RUNNING" as const,
      items: [],
    };
    const api = createFixtureApi({
      listSources: async () => ({
        items: [
          {
            ...sourceFixture,
            id: "source-recheck-unmount",
            filename: "leaving.csv",
            latest_job_id: running.id,
            latest_result: null,
          },
        ],
        next_cursor: null,
      }),
      listWorkspaceJobs: async () => ({ items: [running], next_cursor: null }),
      getJob: async () => {
        reads += 1;
        if (reads <= 5) throw new TypeError("temporary poll failure");
        return await recheckedJob;
      },
      createComparisonJob: async (_workspaceId, sourceIds) => {
        comparisons.push(sourceIds);
        return {
          job_id: "compare-recheck-after-unmount",
          status: "QUEUED",
          workspace_status: "ANALYZING",
          row_version: 2,
        };
      },
    });
    const view = render(<IngestionPanel api={api} workspace={draft} />);
    const recheck = await screen.findByRole(
      "button",
      { name: "자료 읽기 상태 다시 확인" },
      { timeout: 10_000 },
    );
    await user.click(recheck);
    await waitFor(() => expect(reads).toBe(6));

    view.unmount();
    await act(async () => {
      resolveRecheck({ ...running, status: "SUCCEEDED", items: [result] });
      await recheckedJob;
    });

    expect(comparisons).toEqual([]);
  }, 12_000);

  test("느린 초기 발견 응답은 사용자가 시작한 업로드를 덮어쓰지 않는다", async () => {
    const user = userEvent.setup();
    const draft = workspace("workspace-hydration-race", "초기 발견 경쟁", "DRAFT");
    type SourcePage = Awaited<ReturnType<ReturnType<typeof createFixtureApi>["listSources"]>>;
    type JobPage = Awaited<ReturnType<ReturnType<typeof createFixtureApi>["listWorkspaceJobs"]>>;
    type JobResponse = Awaited<ReturnType<ReturnType<typeof createFixtureApi>["getJob"]>>;
    let resolveSources!: (page: SourcePage) => void;
    let resolveJobs!: (page: JobPage) => void;
    let resolveIngest!: (job: JobResponse) => void;
    const comparisons: string[][] = [];
    const sourcePage = new Promise<SourcePage>((resolve) => {
      resolveSources = resolve;
    });
    const jobPage = new Promise<JobPage>((resolve) => {
      resolveJobs = resolve;
    });
    const ingest = new Promise<JobResponse>((resolve) => {
      resolveIngest = resolve;
    });
    const api = createFixtureApi({
      listSources: async () => await sourcePage,
      listWorkspaceJobs: async () => await jobPage,
      uploadSources: async () => ({
        job_id: "ingest-hydration-race",
        items: [
          {
            filename: "new.csv",
            status: "ACCEPTED",
            source_id: "source-new",
            error: null,
            repair_obligation_id: null,
            repair_generation: null,
          },
        ],
      }),
      getJob: async (jobId: string) =>
        jobId === "ingest-hydration-race"
          ? await ingest
          : { ...idleJob, id: jobId, type: "COMPARE", status: "FAILED", items: [] },
      createComparisonJob: async (_workspaceId, sourceIds) => {
        comparisons.push(sourceIds);
        return {
          job_id: "compare-hydration-race",
          status: "QUEUED",
          workspace_status: "ANALYZING",
          row_version: 2,
        };
      },
    });
    render(<IngestionPanel api={api} workspace={draft} />);

    await user.upload(
      screen.getByLabelText("추천자료 파일 선택"),
      new File(["title\nnew\n"], "new.csv"),
    );
    await user.click(screen.getByRole("button", { name: "우리 도서관에 없는 책 찾기" }));
    await waitFor(() => expect(screen.getByText("new.csv")).toBeVisible());
    resolveSources({ items: [], next_cursor: null });
    resolveJobs({ items: [], next_cursor: null });
    await act(async () => undefined);
    resolveIngest({
      ...idleJob,
      id: "ingest-hydration-race",
      status: "SUCCEEDED",
      items: [
        {
          source_document_id: "source-new",
          filename: "new.csv",
          status: "SUCCESS",
          total_rows: 1,
          processed_rows: 1,
          row_error_count: 0,
          error: null,
          mapping_required: null,
        },
      ],
    });

    await waitFor(() => expect(comparisons).toEqual([["source-new"]]));
  });

  test("폴링이 멈춘 뒤에는 현재 작업부터 다시 확인하고 실행 중 작업을 재시도하지 않는다", async () => {
    const user = userEvent.setup();
    const draft = workspace("workspace-recheck", "상태 재확인", "DRAFT");
    let jobReads = 0;
    let retries = 0;
    const running = { ...idleJob, id: "ingest-running", status: "RUNNING", stage: "PARSING" };
    const api = createFixtureApi({
      listSources: async () => ({
        items: [
          {
            ...sourceFixture,
            id: "source-running-recheck",
            filename: "running.csv",
            latest_job_id: running.id,
            latest_result: null,
          },
        ],
        next_cursor: null,
      }),
      listWorkspaceJobs: async () => ({ items: [running], next_cursor: null }),
      getJob: async () => {
        jobReads += 1;
        if (jobReads <= 5) throw new TypeError("temporary poll failure");
        return { ...running, status: "SUCCEEDED", stage: "COMPLETED" };
      },
      retryJob: async () => {
        retries += 1;
        return running;
      },
    });
    render(<IngestionPanel api={api} workspace={draft} />);

    const recheck = await screen.findByRole(
      "button",
      { name: "자료 읽기 상태 다시 확인" },
      { timeout: 10_000 },
    );
    expect(screen.queryByRole("button", { name: /전체 다시 시도/ })).not.toBeInTheDocument();
    await user.click(recheck);
    await waitFor(() => expect(jobReads).toBeGreaterThanOrEqual(6));
    expect(retries).toBe(0);
  }, 12_000);

  test("비교 폴링이 멈추면 같은 비교 작업을 먼저 확인하고 실행 중에는 재시도하지 않는다", async () => {
    const user = userEvent.setup();
    const draft = workspace("workspace-compare-recheck", "비교 상태 재확인", "DRAFT");
    const reviewed = { ...draft, status: "CANDIDATE_REVIEW", row_version: 3 };
    let compareReads = 0;
    let retries = 0;
    const changes: string[] = [];
    const api = createFixtureApi({
      uploadSources: async () => ({
        job_id: "ingest-for-compare-recheck",
        items: [
          {
            filename: "ready.csv",
            status: "ACCEPTED",
            source_id: "source-ready-recheck",
            error: null,
            repair_obligation_id: null,
            repair_generation: null,
          },
        ],
      }),
      getJob: async (jobId) => {
        if (jobId === "ingest-for-compare-recheck") {
          return {
            ...idleJob,
            id: jobId,
            status: "SUCCEEDED",
            items: [
              {
                source_document_id: "source-ready-recheck",
                filename: "ready.csv",
                status: "SUCCESS",
                total_rows: 1,
                processed_rows: 1,
                row_error_count: 0,
                error: null,
                mapping_required: null,
              },
            ],
          };
        }
        compareReads += 1;
        if (compareReads <= 5) throw new TypeError("temporary compare poll failure");
        return {
          ...idleJob,
          id: jobId,
          type: "COMPARE",
          status: compareReads === 6 ? "RUNNING" : "SUCCEEDED",
          items: [],
        };
      },
      createComparisonJob: async () => ({
        job_id: "compare-recheck",
        status: "QUEUED",
        workspace_status: "ANALYZING",
        row_version: 2,
      }),
      retryJob: async () => {
        retries += 1;
        return { ...idleJob, id: "compare-recheck", type: "COMPARE" };
      },
      getWorkspace: async () => ({ data: reviewed, etag: '"3"' }),
    });
    render(
      <IngestionPanel
        api={api}
        onWorkspaceChange={(next) => changes.push(next.status)}
        workspace={draft}
      />,
    );
    await user.upload(
      screen.getByLabelText("추천자료 파일 선택"),
      new File(["title\nready\n"], "ready.csv"),
    );
    await user.click(screen.getByRole("button", { name: "우리 도서관에 없는 책 찾기" }));

    const recheck = await screen.findByRole(
      "button",
      { name: "도서 비교 상태 다시 확인" },
      { timeout: 10_000 },
    );
    expect(screen.queryByRole("button", { name: "도서 비교 다시 시도" })).not.toBeInTheDocument();
    await user.click(recheck);
    await waitFor(() => expect(changes).toEqual(["CANDIDATE_REVIEW"]));
    expect(compareReads).toBe(7);
    expect(retries).toBe(0);
  }, 12_000);

  test("종료된 파일 결과는 실패 행을 읽은 권수에 넣지 않고 부분 완료를 분명히 알린다", async () => {
    const draft = workspace("workspace-truthful-files", "파일 결과", "DRAFT");
    const failedResult = {
      source_document_id: "source-failed",
      filename: "failed.csv",
      status: "FAILED",
      total_rows: 1,
      processed_rows: 1,
      row_error_count: 0,
      error: { code: "PARSER_FAILURE", message: "파일 내용을 읽지 못했습니다.", type: null },
      mapping_required: null,
    };
    const partialResult = {
      source_document_id: "source-partial",
      filename: "partial.csv",
      status: "PARTIAL",
      total_rows: 3,
      processed_rows: 2,
      row_error_count: 1,
      error: { code: "ROW_ERRORS", message: "한 행을 확인해 주세요.", type: null },
      mapping_required: null,
    };
    const source = (id: string, filename: string, latestResult: typeof failedResult) => ({
      ...sourceFixture,
      id,
      filename,
      latest_job_id: "ingest-truth",
      latest_result: latestResult,
    });
    const api = createFixtureApi({
      listSources: async () => ({
        items: [
          source("source-failed", "failed.csv", failedResult),
          source("source-partial", "partial.csv", partialResult),
        ],
        next_cursor: null,
      }),
      listWorkspaceJobs: async () => ({
        items: [{ ...idleJob, id: "ingest-truth", status: "PARTIAL", items: [failedResult, partialResult] }],
        next_cursor: null,
      }),
    });
    render(<IngestionPanel api={api} workspace={draft} />);

    const failed = await screen.findByRole("listitem", { name: "failed.csv 처리 상태" });
    expect(within(failed).getByText("읽기 실패")).toBeVisible();
    expect(within(failed).getByText(/0권 읽음/)).toBeVisible();
    expect(within(failed).queryByText(/1권 읽음/)).not.toBeInTheDocument();
    expect(within(failed).getByText("파일 내용을 읽지 못했습니다.")).toBeVisible();
    const partial = screen.getByRole("listitem", { name: "partial.csv 처리 상태" });
    expect(within(partial).getByText("일부 읽기 완료")).toBeVisible();
    expect(within(partial).getByText("2권 읽음 · 확인 필요 1")).toBeVisible();
    expect(within(partial).queryByText("대기 중")).not.toBeInTheDocument();
  });

  test("교체 파일을 연달아 고르면 최신 세대만 남기고 비교는 한 번만 시작한다", async () => {
    const user = userEvent.setup();
    const draft = workspace("workspace-repair-race", "교체 세대", "DRAFT");
    type UploadResponse = Awaited<ReturnType<ReturnType<typeof createFixtureApi>["uploadSources"]>>;
    const pending = new Map<number, (value: UploadResponse) => void>();
    const generations: number[] = [];
    const comparisons: string[][] = [];
    let initial = true;
    const api = createFixtureApi({
      uploadSources: async (_workspaceId, input) => {
        if (initial) {
          initial = false;
          return {
            job_id: "ingest-ready",
            items: [
              { filename: "ready.csv", status: "ACCEPTED", source_id: "source-ready", error: null, repair_obligation_id: null, repair_generation: null },
              { filename: "broken.exe", status: "FAILED", source_id: null, error: { code: "UNSUPPORTED_FILE_TYPE", message: "지원하지 않는 파일 형식입니다." }, repair_obligation_id: "repair-o", repair_generation: 0 },
            ],
          };
        }
        const generation = input.repairGeneration ?? -1;
        generations.push(generation);
        return await new Promise<UploadResponse>((resolve) => pending.set(generation, resolve));
      },
      getJob: async (jobId) => ({
        ...idleJob,
        id: jobId,
        items: jobId === "ingest-ready"
          ? [{ source_document_id: "source-ready", filename: "ready.csv", status: "SUCCESS", total_rows: 1, processed_rows: 1, row_error_count: 0, error: null, mapping_required: null }]
          : [{ source_document_id: "source-b", filename: "replacement-b.csv", status: "SUCCESS", total_rows: 1, processed_rows: 1, row_error_count: 0, error: null, mapping_required: null }],
      }),
      createComparisonJob: async (_workspaceId, sourceIds, version) => {
        comparisons.push(sourceIds);
        return { job_id: "compare-repair", status: "QUEUED", workspace_status: "ANALYZING", row_version: version + 1 };
      },
    });
    render(<IngestionPanel api={api} workspace={draft} />);
    await user.upload(
      screen.getByLabelText("추천자료 파일 선택"),
      [new File(["제목\n준비\n"], "ready.csv"), new File(["bad"], "broken.exe")],
    );
    await user.click(screen.getByRole("button", { name: "우리 도서관에 없는 책 찾기" }));
    const replacement = await screen.findByLabelText("broken.exe 수정한 파일 선택");
    await user.upload(replacement, new File(["제목\nA\n"], "replacement-a.csv"));
    await user.upload(replacement, new File(["제목\nB\n"], "replacement-b.csv"));
    expect(generations).toEqual([1, 2]);

    act(() => pending.get(2)?.({
      job_id: "ingest-b",
      items: [{ filename: "replacement-b.csv", status: "ACCEPTED", source_id: "source-b", error: null, repair_obligation_id: "repair-o", repair_generation: 2 }],
    }));
    await waitFor(() => expect(comparisons).toEqual([["source-ready", "source-b"]]));
    act(() => pending.get(1)?.({
      job_id: "ingest-a",
      items: [{ filename: "replacement-a.csv", status: "ACCEPTED", source_id: "source-a", error: null, repair_obligation_id: "repair-o", repair_generation: 1 }],
    }));
    await act(async () => undefined);
    expect(comparisons).toEqual([["source-ready", "source-b"]]);
    expect(screen.getByText("replacement-b.csv")).toBeVisible();
    expect(screen.queryByText("replacement-a.csv")).not.toBeInTheDocument();
  });
  test("파일 고르기는 키보드 초점이 보이는 실제 버튼이다", async () => {
    const user = userEvent.setup();
    const draft = workspace("workspace-focus", "키보드 자료 선택", "DRAFT");
    render(<IngestionPanel api={createFixtureApi()} workspace={draft} />);

    await user.tab();
    expect(screen.getByLabelText("자료 역할")).toHaveFocus();
    await user.tab();
    const picker = screen.getByRole("button", { name: "파일 고르기" });
    expect(picker).toHaveFocus();
    expect(picker).toBeVisible();
  });

  test("개발 StrictMode에서도 자료 읽기와 비교가 끝까지 이어진다", async () => {
    const user = userEvent.setup();
    const draft = workspace("workspace-strict", "StrictMode 자료", "DRAFT");
    const reviewed = { ...draft, status: "CANDIDATE_REVIEW", row_version: 3 };
    const changed: string[] = [];
    const api = createFixtureApi({
      uploadSources: async () => ({
        job_id: "ingest-strict",
        items: [
          {
            filename: "strict.csv",
            status: "ACCEPTED",
            source_id: "source-strict",
            error: null,
            repair_obligation_id: null,
            repair_generation: null,
          },
        ],
      }),
      getJob: async (jobId) => ({
        ...idleJob,
        id: jobId,
        type: jobId === "compare-strict" ? "COMPARE" : "INGEST",
        items:
          jobId === "ingest-strict"
            ? [
                {
                  source_document_id: "source-strict",
                  filename: "strict.csv",
                  status: "SUCCESS",
                  total_rows: 1,
                  processed_rows: 1,
                  row_error_count: 0,
                  error: null,
                  mapping_required: null,
                },
              ]
            : [],
      }),
      createComparisonJob: async () => ({
        job_id: "compare-strict",
        status: "QUEUED",
        workspace_status: "ANALYZING",
        row_version: 2,
      }),
      getWorkspace: async () => ({ data: reviewed, etag: '"3"' }),
    });

    render(
      <StrictMode>
        <IngestionPanel
          api={api}
          onWorkspaceChange={(next) => changed.push(next.status)}
          workspace={draft}
        />
      </StrictMode>,
    );
    await user.upload(
      screen.getByLabelText("추천자료 파일 선택"),
      new File(["제목,저자\n책,저자\n"], "strict.csv", { type: "text/csv" }),
    );
    await user.click(
      screen.getByRole("button", { name: "우리 도서관에 없는 책 찾기" }),
    );

    await waitFor(() => expect(changed).toEqual(["CANDIDATE_REVIEW"]));
    expect(screen.getByRole("status")).toHaveTextContent("비교를 마쳤습니다");
  });

  test("현재 과정만 펼치고 여러 파일의 진행·부분 실패·재시도·취소를 글로 알린다", async () => {
    const user = userEvent.setup();
    const draft = workspace("workspace-draft", "여름방학 추천도서", "DRAFT");
    const uploadedBatches: string[][] = [];
    const api = createFixtureApi({
      getWorkspace: async () => ({ data: draft, etag: '"1"' }),
      uploadSources: async (_workspaceId, input) => {
        uploadedBatches.push(input.files.map((file) => file.name));
        return {
          job_id: "job-partial",
          items: input.files.map((file) =>
            file.name === "broken.exe"
              ? {
                  filename: file.name,
                  status: "FAILED",
                  source_id: null,
                  error: {
                    code: "UNSUPPORTED_FILE_TYPE",
                    message: "지원하지 않는 파일 형식입니다.",
                  },
                  repair_obligation_id: "repair-broken",
                  repair_generation: 0,
                }
              : {
                  filename: file.name,
                  status: "ACCEPTED",
                  source_id: "source-books",
                  error: null,
                  repair_obligation_id: input.repairObligationId ?? null,
                  repair_generation: input.repairGeneration ?? null,
                },
          ),
        };
      },
      getJob: async () => ({
        ...idleJob,
        id: "job-partial",
        status: "RUNNING",
        stage: "PARSING",
        progress_current: 1,
        progress_total: 2,
        items: [
          {
            source_document_id: "source-books",
            filename: "books.csv",
            status: "SUCCESS",
            total_rows: 2,
            processed_rows: 2,
            row_error_count: 0,
            error: null,
            mapping_required: null,
          },
        ],
      }),
    });
    renderWorkroom(api, draft.id);

    expect(
      await screen.findByRole("heading", { level: 1, name: "수서 작업실" }),
    ).toBeVisible();
    const current = screen.getByRole("region", { name: "1. 후보 만들기" });
    expect(current).toHaveAttribute("aria-current", "step");
    expect(
      screen.getByRole("region", { name: "2. 승인·발주" }),
    ).toHaveAttribute("aria-disabled", "true");
    expect(
      screen.getByRole("region", { name: "3. 납품 검수" }),
    ).toHaveAttribute("aria-disabled", "true");
    expect(screen.getByText(/XLS, XLSX, XLSB, ODS/)).toBeVisible();
    expect(screen.getByText(/DOCX, HWPX, HWP, MARC/)).toBeVisible();

    const input = screen.getByLabelText("추천자료 파일 선택");
    await user.upload(input, [
      new File(["title,author\nA,B\nC,D\n"], "books.csv", {
        type: "text/csv",
      }),
      new File(["MZ-invalid"], "broken.exe", {
        type: "application/octet-stream",
      }),
    ]);
    await user.click(
      screen.getByRole("button", { name: "우리 도서관에 없는 책 찾기" }),
    );

    const books = await screen.findByRole("listitem", { name: "books.csv 처리 상태" });
    expect(books).toHaveTextContent("추천목록");
    expect(books).toHaveTextContent("읽기 완료");
    expect(books).toHaveTextContent("100%");
    expect(books).toHaveTextContent("2권 읽음");
    expect(books).toHaveTextContent("확인 필요 0");
    expect(
      within(books).queryByRole("button", { name: /취소/u }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "전체 자료 진행률" })).toHaveValue(50);
    expect(
      screen.getByRole("button", { name: "자료 읽기 모두 취소" }),
    ).toBeVisible();

    const broken = screen.getByRole("listitem", { name: "broken.exe 처리 상태" });
    expect(broken).toHaveTextContent("읽지 못함");
    expect(broken).toHaveTextContent("지원하지 않는 파일 형식입니다.");
    expect(
      within(broken).getByRole("button", { name: "broken.exe 다시 올리기" }),
    ).toBeVisible();
    expect(screen.getByRole("status")).toHaveTextContent("1개 파일은 확인이 필요합니다");
    await user.click(
      within(broken).getByRole("button", { name: "broken.exe 다시 올리기" }),
    );
    await user.upload(
      within(broken).getByLabelText("broken.exe 수정한 파일 선택"),
      new File(["title,author\nFixed,Writer\n"], "broken-fixed.csv", {
        type: "text/csv",
      }),
    );
    expect(uploadedBatches).toEqual([
      ["books.csv", "broken.exe"],
      ["broken-fixed.csv"],
    ]);
  });

  test("서버 미리보기의 열 연결 뒤 해당 자료를 다시 읽고 비교하여 후보까지 연다", async () => {
    const user = userEvent.setup();
    const draft = workspace("workspace-draft", "낯선 추천양식", "DRAFT");
    const reviewed = {
      ...draft,
      status: "CANDIDATE_REVIEW",
      row_version: 3,
    };
    const calls: string[] = [];
    let workspaceReads = 0;
    const previewRows = Array.from({ length: 20 }, (_, index) => [
      `책 ${index + 1}`,
      `저자 ${index + 1}`,
    ]);
    const textSpy = vi
      .spyOn(File.prototype, "text")
      .mockRejectedValue(new Error("binary files must not be parsed in the browser"));
    const api = createFixtureApi({
      getWorkspace: async () => {
        workspaceReads += 1;
        calls.push(workspaceReads === 1 ? "workspace:draft" : "workspace:candidates");
        return workspaceReads === 1
          ? { data: draft, etag: '"1"' }
          : { data: reviewed, etag: '"3"' };
      },
      uploadSources: async () => ({
        job_id: "ingest-mapping",
        items: [
          {
            filename: "처음보는양식.xlsx",
            status: "ACCEPTED",
            source_id: sourceFixture.id,
            error: null,
            repair_obligation_id: null,
            repair_generation: null,
          },
        ],
      }),
      getJob: async (jobId) => {
        calls.push(`job:${jobId}`);
        if (jobId === "ingest-mapping") {
          return {
            ...idleJob,
            id: jobId,
            status: "PARTIAL",
            stage: "PARSING",
            items: [
              {
                source_document_id: sourceFixture.id,
                filename: "처음보는양식.xlsx",
                status: "PARTIAL",
                total_rows: 25,
                processed_rows: 0,
                row_error_count: 0,
                error: {
                  code: "MAPPING_REQUIRED",
                  message: "열 이름과 자료 내용을 확인해 연결해 주세요.",
                  type: null,
                },
                mapping_required: {
                  headers: ["내부 열", "쓴 사람"],
                  preview_rows: previewRows,
                  suggested_mapping: { "내부 열": null, "쓴 사람": null },
                  required_fields: ["title"],
                  confidence: 0,
                  questions: ["제목 열을 확인해 주세요."],
                },
              },
            ],
          };
        }
        return {
          ...idleJob,
          id: jobId,
          type: jobId === "compare-1" ? "COMPARE" : "PARSE",
          status: "SUCCEEDED",
          items:
            jobId === "parse-1"
              ? [
                  {
                    source_document_id: sourceFixture.id,
                    filename: "처음보는양식.xlsx",
                    status: "SUCCESS",
                    total_rows: 25,
                    processed_rows: 25,
                    row_error_count: 0,
                    error: null,
                    mapping_required: null,
                  },
                ]
              : [],
        };
      },
      getSource: async () => ({ data: sourceFixture, etag: '"1"' }),
      updateSourceMapping: async (_sourceId, input, version) => {
        calls.push("mapping:save");
        return {
          data: { ...sourceFixture, mapping: input.mapping ?? {}, row_version: version + 1 },
          etag: '"2"',
        };
      },
      parseSource: async (sourceId) => {
        calls.push(`parse:${sourceId}`);
        return { job_id: "parse-1", status: "QUEUED" };
      },
      createComparisonJob: async (_workspaceId, sourceIds, version) => {
        calls.push(`compare:${sourceIds.join(",")}:${version}`);
        return {
          job_id: "compare-1",
          status: "QUEUED",
          workspace_status: "ANALYZING",
          row_version: 2,
        };
      },
      listCandidates: async (_workspaceId, filters) => ({
        items:
          filters.outcome === "CANDIDATE"
            ? [candidate("candidate-from-flow", "비교해서 찾은 책", "CANDIDATE")]
            : [],
        next_cursor: null,
        total_count: filters.outcome === "CANDIDATE" ? 1 : 0,
        summary: {
          total_count: 1,
          candidate_count: 1,
          needs_review_count: 0,
          excluded_count: 0,
          unresolved_count: 0,
          expected_total_won: 12_000,
        },
      }),
    });
    renderWorkroom(api, draft.id);

    const input = await screen.findByLabelText("추천자료 파일 선택");
    await user.upload(
      input,
      new File([new Uint8Array([80, 75, 3, 4])], "처음보는양식.xlsx", {
        type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      }),
    );
    await user.click(
      screen.getByRole("button", { name: "우리 도서관에 없는 책 찾기" }),
    );

    const dialog = await screen.findByRole("dialog", { name: "열 연결 확인" });
    expect(within(dialog).getAllByRole("row")).toHaveLength(21);
    expect(within(dialog).getByLabelText("이 양식 기억")).toBeChecked();
    await user.selectOptions(within(dialog).getByLabelText("제목 열"), "내부 열");
    await user.selectOptions(within(dialog).getByLabelText("저자 열"), "쓴 사람");
    await user.click(within(dialog).getByRole("button", { name: "열 연결 적용" }));

    await waitFor(() => expect(dialog).not.toBeInTheDocument());
    expect(await screen.findByRole("heading", { name: "후보 확인" })).toBeVisible();
    expect(screen.getByRole("tab", { name: "수서 후보 1" })).toBeVisible();
    expect(calls).toEqual([
      "workspace:draft",
      "job:ingest-mapping",
      "mapping:save",
      `parse:${sourceFixture.id}`,
      "job:parse-1",
      `compare:${sourceFixture.id}:1`,
      "job:compare-1",
      "workspace:candidates",
    ]);
    expect(textSpy).not.toHaveBeenCalled();
  });

  test("열 연결을 닫거나 다시 읽기가 실패해도 같은 자료에서 다시 열어 이어간다", async () => {
    const user = userEvent.setup();
    const draft = workspace("workspace-mapping-recovery", "열 연결 복구", "DRAFT");
    const mappingRequired = {
      headers: ["책 열", "사람 열"],
      preview_rows: [["복구할 책", "글쓴이"]],
      suggested_mapping: { "책 열": "title", "사람 열": "author" },
      required_fields: ["title"],
      confidence: 0,
      questions: ["제목 열을 확인해 주세요."],
    };
    const mappingItem = {
      source_document_id: "source-recovery",
      filename: "recovery.xlsx",
      status: "PARTIAL",
      total_rows: 1,
      processed_rows: 0,
      row_error_count: 0,
      error: { code: "MAPPING_REQUIRED", message: "열 연결 확인", type: null },
      mapping_required: mappingRequired,
    };
    const mappingVersions: number[] = [];
    let parseAttempts = 0;
    let sourceReads = 0;
    const api = createFixtureApi({
      uploadSources: async () => ({
        job_id: "ingest-recovery",
        items: [
          {
            filename: "recovery.xlsx",
            status: "ACCEPTED",
            source_id: "source-recovery",
            error: null,
            repair_obligation_id: null,
            repair_generation: null,
          },
        ],
      }),
      getJob: async (jobId) =>
        jobId === "ingest-recovery"
          ? { ...idleJob, id: jobId, status: "PARTIAL", items: [mappingItem] }
          : {
              ...idleJob,
              id: jobId,
              type: "PARSE",
              items: [
                {
                  ...mappingItem,
                  status: "SUCCESS",
                  processed_rows: 1,
                  row_error_count: 0,
                  error: null,
                  mapping_required: null,
                },
              ],
            },
      getSource: async () => {
        sourceReads += 1;
        if (sourceReads === 1) throw new TypeError("source detail interrupted");
        return {
          data: { ...sourceFixture, id: "source-recovery", row_version: 1 },
          etag: '"1"',
        };
      },
      updateSourceMapping: async (_sourceId, input, version) => {
        mappingVersions.push(version);
        return {
          data: {
            ...sourceFixture,
            id: "source-recovery",
            mapping: input.mapping ?? {},
            row_version: version + 1,
          },
          etag: `"${version + 1}"`,
        };
      },
      parseSource: async () => {
        parseAttempts += 1;
        if (parseAttempts === 1) throw new TypeError("network interrupted");
        return { job_id: "parse-recovery", status: "QUEUED" };
      },
      createComparisonJob: async () => ({
        job_id: "compare-recovery",
        status: "QUEUED",
        workspace_status: "ANALYZING",
        row_version: 2,
      }),
    });
    render(<IngestionPanel api={api} workspace={draft} />);
    await user.upload(
      screen.getByLabelText("추천자료 파일 선택"),
      new File(["binary"], "recovery.xlsx"),
    );
    await user.click(screen.getByRole("button", { name: "우리 도서관에 없는 책 찾기" }));

    await user.click(
      await screen.findByRole("button", { name: "recovery.xlsx 열 연결 다시 확인" }),
    );
    let dialog = await screen.findByRole("dialog", { name: "열 연결 확인" });
    await user.click(within(dialog).getByRole("button", { name: "돌아가기" }));
    expect(dialog).not.toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: "recovery.xlsx 열 연결 다시 확인" }),
    );
    dialog = await screen.findByRole("dialog", { name: "열 연결 확인" });
    await user.click(within(dialog).getByRole("button", { name: "열 연결 적용" }));
    expect(await screen.findByRole("status")).toHaveTextContent("network interrupted");
    expect(screen.getByRole("dialog", { name: "열 연결 확인" })).toBeVisible();

    await user.click(
      within(screen.getByRole("dialog", { name: "열 연결 확인" })).getByRole("button", {
        name: "열 연결 적용",
      }),
    );
    await waitFor(() => expect(parseAttempts).toBe(2));
    expect(mappingVersions).toEqual([1, 2]);
  });

  test("부분 접수는 실패 파일을 고치기 전까지 성공 파일만으로 비교하지 않는다", async () => {
    const user = userEvent.setup();
    const draft = workspace("workspace-partial-repair", "부분 접수 복구", "DRAFT");
    let uploads = 0;
    const compared: string[][] = [];
    const api = createFixtureApi({
      uploadSources: async (_workspaceId, input) => {
        uploads += 1;
        return uploads === 1
          ? {
              job_id: "ingest-first",
              items: [
                {
                  filename: "ready.csv",
                  status: "ACCEPTED",
                  source_id: "source-ready",
                  error: null,
                  repair_obligation_id: null,
                  repair_generation: null,
                },
                {
                  filename: "repair.exe",
                  status: "FAILED",
                  source_id: null,
                  error: { code: "FILE_TOO_LARGE", message: "파일을 확인해 주세요." },
                  repair_obligation_id: "repair-obligation",
                  repair_generation: 0,
                },
              ],
            }
          : {
              job_id: "ingest-repair",
              items: [
                {
                  filename: input.files[0]?.name ?? "repair-fixed.csv",
                  status: "ACCEPTED",
                  source_id: "source-repair",
                  error: null,
                  repair_obligation_id: "repair-obligation",
                  repair_generation: 1,
                },
              ],
            };
      },
      getJob: async (jobId) => ({
        ...idleJob,
        id: jobId,
        items: [
          {
            source_document_id:
              jobId === "ingest-first" ? "source-ready" : "source-repair",
            filename: jobId === "ingest-first" ? "ready.csv" : "repair-fixed.csv",
            status: "SUCCESS",
            total_rows: 1,
            processed_rows: 1,
            row_error_count: 0,
            error: null,
            mapping_required: null,
          },
        ],
      }),
      createComparisonJob: async (_workspaceId, sourceIds) => {
        compared.push(sourceIds);
        return {
          job_id: "compare-repaired",
          status: "QUEUED",
          workspace_status: "ANALYZING",
          row_version: 2,
        };
      },
    });
    render(<IngestionPanel api={api} workspace={draft} />);
    await user.upload(screen.getByLabelText("추천자료 파일 선택"), [
      new File(["title\nReady\n"], "ready.csv", { type: "text/csv" }),
      new File(["MZ-broken"], "repair.exe", { type: "application/octet-stream" }),
    ]);
    await user.click(screen.getByRole("button", { name: "우리 도서관에 없는 책 찾기" }));

    await screen.findByRole("listitem", { name: "repair.exe 처리 상태" });
    expect(compared).toEqual([]);
    await user.upload(
      screen.getByLabelText("repair.exe 수정한 파일 선택"),
      new File(["title\nRepaired\n"], "repair-fixed.csv", { type: "text/csv" }),
    );
    await waitFor(() => expect(compared).toEqual([["source-ready", "source-repair"]]));
  });

  test("다른 작업으로 이동하면 이전 작업의 선택 파일과 처리 상태를 비운다", async () => {
    const user = userEvent.setup();
    const first = workspace("workspace-first-draft", "첫 작업", "DRAFT");
    const second = workspace("workspace-second-draft", "둘째 작업", "DRAFT");
    const api = createFixtureApi();
    const view = render(<IngestionPanel api={api} workspace={first} />);
    await user.upload(
      screen.getByLabelText("추천자료 파일 선택"),
      new File(["title\nFirst\n"], "first.csv", { type: "text/csv" }),
    );
    expect(screen.getByText("first.csv")).toBeVisible();

    view.rerender(<IngestionPanel api={api} workspace={second} />);

    await waitFor(() => expect(screen.queryByText("first.csv")).not.toBeInTheDocument());
    expect(
      screen.getByRole("button", { name: "우리 도서관에 없는 책 찾기" }),
    ).toBeDisabled();
  });

  test("서로 다른 두 파일의 열 연결은 앞 파일 선택값을 다음 파일에 가져가지 않는다", async () => {
    const user = userEvent.setup();
    const draft = workspace("workspace-two-mappings", "서로 다른 양식", "DRAFT");
    const reviewed = { ...draft, status: "CANDIDATE_REVIEW", row_version: 3 };
    const changed: string[] = [];
    const mappingFor = (headers: string[], suggested_mapping: Record<string, string>) => ({
      headers,
      preview_rows: [["첫 값", "둘째 값"]],
      suggested_mapping,
      required_fields: ["title"],
      confidence: 0,
      questions: ["제목 열을 확인해 주세요."],
    });
    const firstMapping = mappingFor(["A열", "B열"], { A열: "title", B열: "author" });
    const secondMapping = mappingFor(["X열", "Y열"], { X열: "author", Y열: "title" });
    const mappingItem = (sourceId: string, filename: string, mappingRequired: typeof firstMapping) => ({
      source_document_id: sourceId,
      filename,
      status: "PARTIAL",
      total_rows: 1,
      processed_rows: 0,
      row_error_count: 0,
      error: { code: "MAPPING_REQUIRED", message: "열 연결 확인", type: null },
      mapping_required: mappingRequired,
    });
    const api = createFixtureApi({
      uploadSources: async () => ({
        job_id: "ingest-two-mappings",
        items: [
          {
            filename: "first.xlsx",
            status: "ACCEPTED",
            source_id: "source-first",
            error: null,
            repair_obligation_id: null,
            repair_generation: null,
          },
          {
            filename: "second.xlsx",
            status: "ACCEPTED",
            source_id: "source-second",
            error: null,
            repair_obligation_id: null,
            repair_generation: null,
          },
        ],
      }),
      getJob: async (jobId) => {
        if (jobId === "ingest-two-mappings") {
          return {
            ...idleJob,
            id: jobId,
            status: "PARTIAL",
            items: [
              mappingItem("source-first", "first.xlsx", firstMapping),
              mappingItem("source-second", "second.xlsx", secondMapping),
            ],
          };
        }
        if (jobId === "parse-source-first" || jobId === "parse-source-second") {
          const first = jobId.endsWith("first");
          return {
            ...idleJob,
            id: jobId,
            type: "PARSE",
            status: first ? "PARTIAL" : "SUCCEEDED",
            items: [
              {
                source_document_id: first ? "source-first" : "source-second",
                filename: first ? "first.xlsx" : "second.xlsx",
                status: first ? "PARTIAL" : "SUCCESS",
                total_rows: 1,
                processed_rows: 1,
                row_error_count: 0,
                error: null,
                mapping_required: null,
              },
            ],
          };
        }
        return { ...idleJob, id: jobId, type: "COMPARE", items: [] };
      },
      getSource: async (sourceId) => ({
        data: { ...sourceFixture, id: sourceId, row_version: 1 },
        etag: '"1"',
      }),
      parseSource: async (sourceId) => ({
        job_id: `parse-${sourceId}`,
        status: "QUEUED",
      }),
      createComparisonJob: async () => ({
        job_id: "compare-two-mappings",
        status: "QUEUED",
        workspace_status: "ANALYZING",
        row_version: 2,
      }),
      getWorkspace: async () => ({ data: reviewed, etag: '"3"' }),
    });
    render(
      <IngestionPanel
        api={api}
        onWorkspaceChange={(next) => changed.push(next.status)}
        workspace={draft}
      />,
    );
    await user.upload(screen.getByLabelText("추천자료 파일 선택"), [
      new File(["first"], "first.xlsx"),
      new File(["second"], "second.xlsx"),
    ]);
    await user.click(screen.getByRole("button", { name: "우리 도서관에 없는 책 찾기" }));

    const firstDialog = await screen.findByRole("dialog", { name: "열 연결 확인" });
    expect(within(firstDialog).getByLabelText("제목 열")).toHaveValue("A열");
    expect(within(firstDialog).getByLabelText("저자 열")).toHaveValue("B열");
    await user.click(within(firstDialog).getByRole("button", { name: "열 연결 적용" }));
    await waitFor(() => expect(firstDialog).not.toBeInTheDocument());

    const secondDialog = await screen.findByRole("dialog", { name: "열 연결 확인" });
    expect(within(secondDialog).getByLabelText("제목 열")).toHaveValue("Y열");
    expect(within(secondDialog).getByLabelText("저자 열")).toHaveValue("X열");
    await user.click(within(secondDialog).getByRole("button", { name: "열 연결 적용" }));

    await waitFor(() => expect(changed).toEqual(["CANDIDATE_REVIEW"]));
  });

  test("접수 뒤 분석 실패는 완료로 말하지 않고 해당 자료만 다시 읽는다", async () => {
    const user = userEvent.setup();
    const draft = workspace("workspace-draft", "분석 다시 읽기", "DRAFT");
    const parsedSources: string[] = [];
    const api = createFixtureApi({
      getWorkspace: async () => ({ data: draft, etag: '"1"' }),
      uploadSources: async () => ({
        job_id: "ingest-failed",
        items: [
          {
            filename: "damaged.xlsx",
            status: "ACCEPTED",
            source_id: "source-damaged",
            error: null,
            repair_obligation_id: null,
            repair_generation: null,
          },
        ],
      }),
      getJob: async () => ({
        ...idleJob,
        id: "ingest-failed",
        status: "FAILED",
        items: [
          {
            source_document_id: "source-damaged",
            filename: "damaged.xlsx",
            status: "FAILED",
            total_rows: 1,
            processed_rows: 1,
            row_error_count: 0,
            error: {
              code: "PARSER_FAILURE",
              message: "파일 내용을 읽지 못했습니다.",
              type: null,
            },
            mapping_required: null,
          },
        ],
      }),
      parseSource: async (sourceId) => {
        parsedSources.push(sourceId);
        return await new Promise<never>(() => undefined);
      },
    });
    renderWorkroom(api, draft.id);
    await user.upload(
      await screen.findByLabelText("추천자료 파일 선택"),
      new File(["damaged"], "damaged.xlsx"),
    );
    await user.click(
      screen.getByRole("button", { name: "우리 도서관에 없는 책 찾기" }),
    );

    const item = await screen.findByRole("listitem", {
      name: "damaged.xlsx 처리 상태",
    });
    expect(item).toHaveTextContent("읽기 실패");
    expect(item).not.toHaveTextContent("읽기 완료");
    await user.click(
      within(item).getByRole("button", { name: "damaged.xlsx 다시 읽기" }),
    );
    expect(parsedSources).toEqual(["source-damaged"]);
  });

  test("도서 비교 실패는 새 비교를 중복 생성하지 않고 같은 작업을 다시 시도한다", async () => {
    const user = userEvent.setup();
    const draft = workspace("workspace-compare-retry", "비교 다시 시도", "DRAFT");
    const reviewed = { ...draft, status: "CANDIDATE_REVIEW", row_version: 3 };
    let workspaceReads = 0;
    let comparisonCreates = 0;
    let comparisonRetries = 0;
    let retried = false;
    const api = createFixtureApi({
      getWorkspace: async () => ({
        data: workspaceReads++ === 0 ? draft : reviewed,
        etag: workspaceReads === 1 ? '"1"' : '"3"',
      }),
      uploadSources: async () => ({
        job_id: "ingest-ready",
        items: [
          {
            filename: "ready.csv",
            status: "ACCEPTED",
            source_id: "source-ready",
            error: null,
            repair_obligation_id: null,
            repair_generation: null,
          },
        ],
      }),
      getJob: async (jobId) => {
        if (jobId === "ingest-ready") {
          return {
            ...idleJob,
            id: jobId,
            items: [
              {
                source_document_id: "source-ready",
                filename: "ready.csv",
                status: "SUCCESS",
                total_rows: 1,
                processed_rows: 1,
                row_error_count: 0,
                error: null,
                mapping_required: null,
              },
            ],
          };
        }
        return {
          ...idleJob,
          id: "compare-failed",
          type: "COMPARE",
          status: retried ? "SUCCEEDED" : "FAILED",
        };
      },
      createComparisonJob: async () => {
        comparisonCreates += 1;
        return {
          job_id: "compare-failed",
          status: "QUEUED",
          workspace_status: "ANALYZING",
          row_version: 2,
        };
      },
      retryJob: async () => {
        comparisonRetries += 1;
        retried = true;
        return {
          ...idleJob,
          id: "compare-failed",
          type: "COMPARE",
          status: "QUEUED",
          stage: "QUEUED",
        };
      },
    });
    renderWorkroom(api, draft.id);
    await user.upload(
      await screen.findByLabelText("추천자료 파일 선택"),
      new File(["제목,저자\n준비된 책,저자\n"], "ready.csv", { type: "text/csv" }),
    );
    await user.click(
      screen.getByRole("button", { name: "우리 도서관에 없는 책 찾기" }),
    );

    const retry = await screen.findByRole("button", { name: "도서 비교 다시 시도" });
    expect(comparisonCreates).toBe(1);
    await user.click(retry);

    expect(await screen.findByRole("heading", { name: "후보 확인" })).toBeVisible();
    expect(comparisonCreates).toBe(1);
    expect(comparisonRetries).toBe(1);
  });

  test("비교 작업은 끝났지만 화면 새로고침이 실패하면 작업 재시도 없이 화면만 다시 읽는다", async () => {
    const user = userEvent.setup();
    const draft = workspace("workspace-refresh-retry", "후보 화면 다시 읽기", "DRAFT");
    const reviewed = { ...draft, status: "CANDIDATE_REVIEW", row_version: 3 };
    let workspaceReads = 0;
    let jobRetries = 0;
    const changed: string[] = [];
    const api = createFixtureApi({
      uploadSources: async () => ({
        job_id: "ingest-refresh",
        items: [
          {
            filename: "refresh.csv",
            status: "ACCEPTED",
            source_id: "source-refresh",
            error: null,
            repair_obligation_id: null,
            repair_generation: null,
          },
        ],
      }),
      getJob: async (jobId) => ({
        ...idleJob,
        id: jobId,
        type: jobId === "compare-refresh" ? "COMPARE" : "INGEST",
        items:
          jobId === "ingest-refresh"
            ? [
                {
                  source_document_id: "source-refresh",
                  filename: "refresh.csv",
                  status: "SUCCESS",
                  total_rows: 1,
                  processed_rows: 1,
                  row_error_count: 0,
                  error: null,
                  mapping_required: null,
                },
              ]
            : [],
      }),
      createComparisonJob: async () => ({
        job_id: "compare-refresh",
        status: "QUEUED",
        workspace_status: "ANALYZING",
        row_version: 2,
      }),
      getWorkspace: async () => {
        workspaceReads += 1;
        if (workspaceReads === 1) throw new TypeError("response lost");
        return { data: reviewed, etag: '"3"' };
      },
      retryJob: async (jobId) => {
        jobRetries += 1;
        return { ...idleJob, id: jobId, status: "QUEUED", stage: "QUEUED" };
      },
    });
    render(
      <IngestionPanel
        api={api}
        onWorkspaceChange={(next) => changed.push(next.status)}
        workspace={draft}
      />,
    );
    await user.upload(
      screen.getByLabelText("추천자료 파일 선택"),
      new File(["제목,저자\n책,저자\n"], "refresh.csv", { type: "text/csv" }),
    );
    await user.click(
      screen.getByRole("button", { name: "우리 도서관에 없는 책 찾기" }),
    );

    const refresh = await screen.findByRole("button", { name: "후보 화면 다시 불러오기" });
    expect(screen.queryByRole("button", { name: "도서 비교 다시 시도" })).not.toBeInTheDocument();
    await user.click(refresh);

    await waitFor(() => expect(changed).toEqual(["CANDIDATE_REVIEW"]));
    expect(jobRetries).toBe(0);
    expect(workspaceReads).toBe(2);
  });
});

describe("후보 확인과 자동 저장", () => {
  const candidateWorkspace = workspace(
    "workspace-candidates",
    "가을 신간 후보",
    "CANDIDATE_REVIEW",
  );
  const candidates = {
    CANDIDATE: [candidate("candidate-ok", "새로 살 책", "CANDIDATE")],
    NEEDS_REVIEW: [
      candidate("candidate-review", "판본을 볼 책", "NEEDS_REVIEW", "판 정보가 비슷합니다."),
    ],
    EXCLUDED: [
      candidate("candidate-excluded", "이미 있는 책", "EXCLUDED", "EXACT_ISBN_MATCH"),
    ],
  };

  function candidateApi(overrides = {}) {
    const summary = {
      total_count: 3,
      candidate_count: 1,
      needs_review_count: 1,
      excluded_count: 1,
      unresolved_count: 1,
      expected_total_won: 12_000,
    };
    return createFixtureApi({
      getCurrentUser: async () => operator,
      getWorkspace: async () => ({ data: candidateWorkspace, etag: '"1"' }),
      listCandidates: async (_workspaceId, filters) => ({
        items: candidates[filters.outcome as keyof typeof candidates] ?? [],
        next_cursor: null,
        total_count:
          candidates[filters.outcome as keyof typeof candidates]?.length ?? 0,
        summary,
      }),
      ...overrides,
    });
  }

  test("수정 요청은 후보 교정에 머물고 분석 중에는 새 업로드를 숨긴다", async () => {
    const changes = workspace("workspace-changes", "수정할 후보", "CHANGES_REQUESTED");
    const analyzing = workspace("workspace-analyzing", "비교 중인 목록", "ANALYZING");
    const changesApi = candidateApi({
      getWorkspace: async () => ({ data: changes, etag: '"1"' }),
    });
    const first = renderWorkroom(changesApi, changes.id);
    expect(await screen.findByRole("heading", { name: "후보 확인" })).toBeVisible();
    expect(screen.queryByText("승인과 발주 진행")).not.toBeInTheDocument();
    first.unmount();

    renderWorkroom(
      createFixtureApi({
        getWorkspace: async () => ({ data: analyzing, etag: '"1"' }),
      }),
      analyzing.id,
    );
    expect(
      await screen.findByText("도서관 장서와 추천자료를 비교하고 있습니다."),
    ).toBeVisible();
    expect(screen.queryByRole("button", { name: "파일 고르기" })).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "우리 도서관에 없는 책 찾기" }),
    ).not.toBeInTheDocument();
  });

  test("분석 상태를 연속으로 읽지 못하면 멈춘 사실과 다시 확인 행동을 보여준다", async () => {
    const user = userEvent.setup();
    const analyzing = workspace("workspace-poll-failure", "비교 조회 실패", "ANALYZING");
    const reviewed = { ...analyzing, status: "CANDIDATE_REVIEW", row_version: 2 };
    let reads = 0;
    const api = candidateApi({
      getWorkspace: async () => {
        reads += 1;
        if (reads === 1) return { data: analyzing, etag: '"1"' };
        if (reads <= 6) throw new TypeError("temporary connection failure");
        return { data: reviewed, etag: '"2"' };
      },
    });
    renderWorkroom(api, analyzing.id);

    expect(
      await screen.findByText("도서관 장서와 추천자료를 비교하고 있습니다."),
    ).toBeVisible();
    const retry = await screen.findByRole(
      "button",
      { name: "비교 상태 다시 확인" },
      { timeout: 10_000 },
    );
    expect(screen.getByRole("alert")).toHaveTextContent(
      "진행 상태를 계속 불러오지 못했습니다",
    );
    await user.click(retry);

    expect(await screen.findByRole("heading", { name: "후보 확인" })).toBeVisible();
    expect(reads).toBe(7);
  }, 12_000);

  test("발견한 비교 작업의 폴링이 멈춰도 다시 확인하면 같은 작업부터 이어간다", async () => {
    const user = userEvent.setup();
    const analyzing = workspace("workspace-known-job-recheck", "발견한 비교 재확인", "ANALYZING");
    const reviewed = { ...analyzing, status: "CANDIDATE_REVIEW", row_version: 2 };
    const running = {
      ...idleJob,
      id: "compare-known-running",
      workspace_id: analyzing.id,
      type: "COMPARE",
      status: "RUNNING",
      stage: "COMPARING",
    };
    let jobReads = 0;
    let workspaceReads = 0;
    const api = candidateApi({
      getWorkspace: async () => {
        workspaceReads += 1;
        return {
          data: jobReads > 5 ? reviewed : analyzing,
          etag: jobReads > 5 ? '"2"' : '"1"',
        };
      },
      listWorkspaceJobs: async () => ({ items: [running], next_cursor: null }),
      getJob: async () => {
        jobReads += 1;
        if (jobReads <= 5) throw new TypeError("temporary job connection failure");
        return { ...running, status: "SUCCEEDED", stage: "COMPLETED" };
      },
    });
    renderWorkroom(api, analyzing.id);

    const recheck = await screen.findByRole(
      "button",
      { name: "비교 상태 다시 확인" },
      { timeout: 10_000 },
    );
    await user.click(recheck);

    expect(await screen.findByRole("heading", { name: "후보 확인" })).toBeVisible();
    expect(jobReads).toBe(6);
    expect(workspaceReads).toBeGreaterThan(1);
  }, 12_000);

  test("분석 중 화면에서 이미 끝난 비교를 발견하면 작업실을 다시 읽어 후보를 연다", async () => {
    const analyzing = workspace("workspace-discovered-success", "끝난 비교 발견", "ANALYZING");
    const reviewed = { ...analyzing, status: "CANDIDATE_REVIEW", row_version: 2 };
    const succeeded = {
      ...idleJob,
      id: "compare-discovered-success",
      workspace_id: analyzing.id,
      type: "COMPARE",
      status: "SUCCEEDED",
      stage: "COMPLETED",
    };
    let workspaceReads = 0;
    const api = candidateApi({
      getWorkspace: async () => {
        workspaceReads += 1;
        return {
          data: workspaceReads > 2 ? reviewed : analyzing,
          etag: workspaceReads > 2 ? '"2"' : '"1"',
        };
      },
      listWorkspaceJobs: async () => ({ items: [succeeded], next_cursor: null }),
    });

    renderWorkroom(api, analyzing.id);

    expect(await screen.findByRole("heading", { name: "후보 확인" })).toBeVisible();
    expect(workspaceReads).toBe(3);
  });

  test("다시 연 분석 화면은 실패한 비교 작업을 찾아 같은 작업만 안전하게 재시도한다", async () => {
    const user = userEvent.setup();
    const analyzing = workspace("workspace-failed-compare", "실패한 비교", "ANALYZING");
    const reviewed = { ...analyzing, status: "CANDIDATE_REVIEW", row_version: 3 };
    const failed = {
      ...idleJob,
      id: "compare-failed-reload",
      workspace_id: analyzing.id,
      type: "COMPARE",
      status: "FAILED",
      stage: "FAILED",
      error: { code: "JOB_FAILED", message: "도서 비교를 마치지 못했습니다.", type: null },
    };
    let retried = 0;
    let created = 0;
    let workspaceReads = 0;
    let jobReads = 0;
    const discoveredTypes: Array<string | undefined> = [];
    const api = candidateApi({
      getWorkspace: async () => {
        workspaceReads += 1;
        return { data: retried > 0 ? reviewed : analyzing, etag: retried > 0 ? '"3"' : '"1"' };
      },
      listWorkspaceJobs: async (
        _workspaceId: string,
        filters?: { type?: string },
      ) => {
        discoveredTypes.push(filters?.type);
        return { items: [failed], next_cursor: null };
      },
      retryJob: async (jobId: string) => {
        expect(jobId).toBe("compare-failed-reload");
        retried += 1;
        return { ...failed, status: "QUEUED", stage: "QUEUED", error: null, retry_count: 1 };
      },
      getJob: async () => {
        jobReads += 1;
        return jobReads === 1
          ? failed
          : { ...failed, status: "SUCCEEDED", stage: "COMPLETED", error: null };
      },
      createComparisonJob: async () => {
        created += 1;
        throw new Error("must not create a second comparison");
      },
    });
    renderWorkroom(api, analyzing.id);

    expect(await screen.findByRole("alert")).toHaveTextContent("도서 비교를 마치지 못했습니다");
    expect(discoveredTypes).toContain("COMPARE");
    await user.click(screen.getByRole("button", { name: "도서 비교 다시 시도" }));
    expect(await screen.findByRole("heading", { name: "후보 확인" })).toBeVisible();
    expect(retried).toBe(1);
    expect(created).toBe(0);
    expect(workspaceReads).toBeGreaterThan(1);
  });

  test("이전 버전의 실패한 비교에 교체 의무가 남으면 파일 복구부터 다시 열어 최신 버전으로 비교한다", async () => {
    const user = userEvent.setup();
    const analyzing = workspace(
      "workspace-legacy-repair-ui",
      "이전 비교 복구",
      "ANALYZING",
    );
    const reopened = { ...analyzing, status: "DRAFT", row_version: 3 };
    const reviewed = { ...analyzing, status: "CANDIDATE_REVIEW", row_version: 5 };
    const failedCompare = {
      ...idleJob,
      id: "compare-legacy-repair-failed",
      workspace_id: analyzing.id,
      type: "COMPARE",
      status: "FAILED",
      stage: "FAILED",
      error: { code: "JOB_FAILED", message: "이전 비교가 중단되었습니다.", type: null },
    };
    const readyResult = {
      source_document_id: "source-legacy-ready",
      filename: "ready.csv",
      status: "SUCCESS" as const,
      total_rows: 1,
      processed_rows: 1,
      row_error_count: 0,
      error: null,
      mapping_required: null,
    };
    let repaired = false;
    let compared = false;
    const comparisonVersions: number[] = [];
    const api = candidateApi({
      getWorkspace: async () => ({
        data: compared ? reviewed : repaired ? reopened : analyzing,
        etag: compared ? '"5"' : repaired ? '"3"' : '"2"',
      }),
      listWorkspaceJobs: async (
        _workspaceId: string,
        filters?: { type?: string },
      ) => ({
        items: filters?.type === "COMPARE" ? [failedCompare] : [],
        next_cursor: null,
      }),
      listSources: async () => ({
        items: [
          {
            ...sourceFixture,
            id: "source-legacy-ready",
            filename: "ready.csv",
            latest_job_id: null,
            latest_result: readyResult,
          },
        ],
        next_cursor: null,
      }),
      listUploadRepairs: async () => ({
        items: repaired
          ? []
          : [
              {
                id: "repair-legacy-ui",
                filename: "broken.exe",
                error: {
                  code: "UNSUPPORTED_FILE_TYPE",
                  message: "지원하지 않는 파일 형식입니다.",
                },
                status: "UNRESOLVED",
                generation: 0,
                role: "PURCHASE_REQUEST",
                vendor_scope: "*",
                requested_start_local_date: null,
                requested_through_local_date: null,
                configuration_confirmation_required: false,
                resolved_source_id: null,
                created_at: "2026-08-29T00:00:00Z",
                updated_at: "2026-08-29T00:00:00Z",
              },
            ],
        next_cursor: null,
      }),
      uploadSources: async () => {
        repaired = true;
        return {
          job_id: "parse-legacy-repair",
          items: [
            {
              filename: "fixed.csv",
              status: "ACCEPTED",
              source_id: "source-legacy-fixed",
              error: null,
              repair_obligation_id: "repair-legacy-ui",
              repair_generation: 1,
            },
          ],
        };
      },
      getJob: async (jobId: string) =>
        jobId === "parse-legacy-repair"
          ? {
              ...idleJob,
              id: jobId,
              type: "PARSE",
              status: "SUCCEEDED",
              items: [
                {
                  source_document_id: "source-legacy-fixed",
                  filename: "fixed.csv",
                  status: "SUCCESS",
                  total_rows: 1,
                  processed_rows: 1,
                  row_error_count: 0,
                  error: null,
                  mapping_required: null,
                },
              ],
            }
          : {
              ...idleJob,
              id: jobId,
              type: "COMPARE",
              status: "SUCCEEDED",
              items: [],
            },
      createComparisonJob: async (
        _workspaceId: string,
        _sourceIds: string[],
        version: number,
      ) => {
        comparisonVersions.push(version);
        compared = true;
        return {
          job_id: "compare-legacy-recovered",
          status: "QUEUED",
          workspace_status: "ANALYZING",
          row_version: 4,
        };
      },
    });
    renderWorkroom(api, analyzing.id);

    expect(
      await screen.findByRole("heading", { name: "추천자료 가져오기" }),
    ).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "도서 비교 다시 시도" }),
    ).not.toBeInTheDocument();
    await user.upload(
      await screen.findByLabelText("broken.exe 수정한 파일 선택"),
      new File(["제목\n복구된 책\n"], "fixed.csv", { type: "text/csv" }),
    );

    expect(await screen.findByRole("heading", { name: "후보 확인" })).toBeVisible();
    expect(comparisonVersions).toEqual([3]);
  });

  test("부분 처리된 추천자료는 화면에서 파일을 교체해 후보까지 이어간다", async () => {
    const user = userEvent.setup();
    const analyzing = workspace(
      "workspace-partial-source-ui",
      "부분 자료 복구",
      "ANALYZING",
    );
    const reopened = { ...analyzing, status: "DRAFT" as const, row_version: 3 };
    const reviewed = {
      ...analyzing,
      status: "CANDIDATE_REVIEW" as const,
      row_version: 5,
    };
    const partialResult = {
      source_document_id: "source-partial-ui",
      filename: "partial.csv",
      status: "PARTIAL" as const,
      total_rows: 2,
      processed_rows: 1,
      row_error_count: 0,
      error: { code: "ROW_ERROR", message: "제목이 없는 행이 있습니다.", type: null },
      mapping_required: null,
    };
    const partialSource = {
      ...sourceFixture,
      id: "source-partial-ui",
      filename: "partial.csv",
      status: "ROW_ERROR" as const,
      mapping: { 제목: "title" },
      latest_job_id: "ingest-partial-ui",
      latest_result: partialResult,
    };
    const partialIngest = {
      ...idleJob,
      id: "ingest-partial-ui",
      workspace_id: analyzing.id,
      type: "INGEST",
      status: "PARTIAL",
      items: [partialResult],
    };
    const partialCompare = {
      ...idleJob,
      id: "compare-partial-ui",
      workspace_id: analyzing.id,
      type: "COMPARE",
      status: "PARTIAL",
      error: { code: "JOB_FAILED", message: "일부 책을 비교하지 못했습니다.", type: null },
      items: [],
    };
    let superseded = false;
    let compared = false;
    const mappings: Array<{ sourceId: string; role: string; version: number }> = [];
    const replacementRoles: string[] = [];
    const replacementSourceIds: Array<string | undefined> = [];
    type UploadResponse = Awaited<
      ReturnType<ReturnType<typeof createFixtureApi>["uploadSources"]>
    >;
    let resolveReplacementUpload!: (response: UploadResponse) => void;
    const replacementUpload = new Promise<UploadResponse>((resolve) => {
      resolveReplacementUpload = resolve;
    });
    const comparisons: Array<{ sourceIds: string[]; version: number }> = [];
    const api = candidateApi({
      getWorkspace: async () => ({
        data: compared ? reviewed : superseded ? reopened : analyzing,
        etag: compared ? '"5"' : superseded ? '"3"' : '"2"',
      }),
      listWorkspaceJobs: async (
        _workspaceId: string,
        filters?: { type?: string },
      ) => ({
        items: filters?.type === "COMPARE" ? [partialCompare] : [partialIngest],
        next_cursor: null,
      }),
      listSources: async () => ({ items: [partialSource], next_cursor: null }),
      listUploadRepairs: async () => ({ items: [], next_cursor: null }),
      getSource: async () => ({ data: partialSource, etag: '"1"' }),
      updateSourceMapping: async (
        sourceId: string,
        input: Parameters<ReturnType<typeof createFixtureApi>["updateSourceMapping"]>[1],
        version: number,
      ) => {
        mappings.push({ sourceId, role: input.role, version });
        superseded = true;
        return {
          data: { ...partialSource, role: input.role, row_version: 2 },
          etag: '"2"',
        };
      },
      uploadSources: async (
        _workspaceId: string,
        input: Parameters<ReturnType<typeof createFixtureApi>["uploadSources"]>[1],
      ) => {
        replacementRoles.push(input.role);
        replacementSourceIds.push(input.replacementSourceId);
        return await replacementUpload;
      },
      getJob: async (jobId: string) =>
        jobId === "ingest-corrected-ui"
          ? {
              ...idleJob,
              id: jobId,
              type: "INGEST",
              status: "SUCCEEDED",
              items: [
                {
                  source_document_id: "source-corrected-ui",
                  filename: "corrected-a.csv",
                  status: "SUCCESS",
                  total_rows: 2,
                  processed_rows: 2,
                  row_error_count: 0,
                  error: null,
                  mapping_required: null,
                },
              ],
            }
          : {
              ...idleJob,
              id: jobId,
              workspace_id: analyzing.id,
              type: "COMPARE",
              status: "SUCCEEDED",
              items: [],
            },
      createComparisonJob: async (
        _workspaceId: string,
        sourceIds: string[],
        version: number,
      ) => {
        comparisons.push({ sourceIds, version });
        compared = true;
        return {
          job_id: "compare-corrected-ui",
          status: "QUEUED",
          workspace_status: "ANALYZING",
          row_version: version + 1,
        };
      },
    });
    renderWorkroom(api, analyzing.id);

    expect(
      await screen.findByRole("heading", { name: "추천자료 가져오기" }),
    ).toBeVisible();
    await user.selectOptions(screen.getByLabelText("자료 역할"), "VENDOR_QUOTE");
    const replacementInput = await screen.findByLabelText(
      "partial.csv 수정한 파일 선택",
    );
    const correctedA = new File(
      ["제목,저자\n정상 추천,저자\n수정 추천,저자\n"],
      "corrected-a.csv",
      { type: "text/csv" },
    );
    const correctedB = new File(
      ["제목,저자\n다른 추천,저자\n"],
      "corrected-b.csv",
      { type: "text/csv" },
    );
    fireEvent.change(replacementInput, { target: { files: [correctedA] } });
    fireEvent.change(replacementInput, { target: { files: [correctedB] } });
    await waitFor(() => expect(replacementRoles).toEqual(["PURCHASE_REQUEST"]));
    act(() => {
      superseded = true;
      resolveReplacementUpload({
        job_id: "ingest-corrected-ui",
        items: [
          {
            filename: "corrected-a.csv",
            status: "ACCEPTED",
            source_id: "source-corrected-ui",
            error: null,
            repair_obligation_id: null,
            repair_generation: null,
          },
        ],
      });
    });

    expect(await screen.findByRole("heading", { name: "후보 확인" })).toBeVisible();
    expect(mappings).toEqual([]);
    expect(comparisons).toEqual([
      { sourceIds: ["source-corrected-ui"], version: 3 },
    ]);
    expect(replacementRoles).toEqual(["PURCHASE_REQUEST"]);
    expect(replacementSourceIds).toEqual(["source-partial-ui"]);
  });

  test("실패 화면의 재시도 직전에 이미 끝난 작업이면 재시도하지 않고 후보를 연다", async () => {
    const user = userEvent.setup();
    const analyzing = workspace("workspace-stale-failed", "오래된 실패 상태", "ANALYZING");
    const reviewed = { ...analyzing, status: "CANDIDATE_REVIEW", row_version: 2 };
    const failed = {
      ...idleJob,
      id: "compare-stale-failed",
      workspace_id: analyzing.id,
      type: "COMPARE",
      status: "FAILED",
      stage: "FAILED",
      error: { code: "JOB_FAILED", message: "도서 비교를 마치지 못했습니다.", type: null },
    };
    let workspaceReads = 0;
    let jobReads = 0;
    let retries = 0;
    const api = candidateApi({
      getWorkspace: async () => {
        workspaceReads += 1;
        return {
          data: workspaceReads > 2 ? reviewed : analyzing,
          etag: workspaceReads > 2 ? '"2"' : '"1"',
        };
      },
      listWorkspaceJobs: async () => ({ items: [failed], next_cursor: null }),
      getJob: async () => {
        jobReads += 1;
        return { ...failed, status: "SUCCEEDED", stage: "COMPLETED", error: null };
      },
      retryJob: async () => {
        retries += 1;
        return { ...failed, status: "QUEUED", stage: "QUEUED" };
      },
    });
    renderWorkroom(api, analyzing.id);

    await user.click(await screen.findByRole("button", { name: "도서 비교 다시 시도" }));

    expect(await screen.findByRole("heading", { name: "후보 확인" })).toBeVisible();
    expect(jobReads).toBe(1);
    expect(retries).toBe(0);
  });

  test("검토자는 실패한 비교를 확인하지만 담당자용 재시도 행동은 보지 않는다", async () => {
    const analyzing = workspace("workspace-reviewer-failed-compare", "검토자 비교 실패", "ANALYZING");
    const failed = {
      ...idleJob,
      id: "compare-reviewer-failed",
      workspace_id: analyzing.id,
      type: "COMPARE",
      status: "FAILED",
      stage: "FAILED",
      error: { code: "JOB_FAILED", message: "도서 비교를 마치지 못했습니다.", type: null },
    };
    const api = candidateApi({
      getCurrentUser: async () => ({ ...operator, roles: ["REVIEWER"] }),
      getWorkspace: async () => ({ data: analyzing, etag: '"1"' }),
      listWorkspaceJobs: async () => ({ items: [failed], next_cursor: null }),
    });
    renderWorkroom(api, analyzing.id);

    expect(await screen.findByRole("alert")).toHaveTextContent("도서 비교를 마치지 못했습니다");
    expect(
      screen.queryByRole("button", { name: "도서 비교 다시 시도" }),
    ).not.toBeInTheDocument();
  });

  test("검토자는 후보를 읽을 수 있지만 담당자 변경 행동은 할 수 없다", async () => {
    const api = candidateApi({ getCurrentUser: async () => ({ ...operator, roles: ["REVIEWER"] }) });
    renderWorkroom(api, candidateWorkspace.id);
    expect(await screen.findByRole("heading", { name: "후보 확인" })).toBeVisible();
    expect(screen.queryByRole("spinbutton", { name: /수량/u })).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "후보 확정하고 승인 요청" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /수서 후보로 포함/u })).not.toBeInTheDocument();
  });

  test("다른 작업실로 이동한 뒤 이전 작업의 늦은 재확인 응답이 새 후보 화면을 지우지 않는다", async () => {
    const user = userEvent.setup();
    const first = workspace("workspace-route-first", "첫 작업", "ANALYZING");
    const firstReviewed = {
      ...first,
      status: "CANDIDATE_REVIEW" as const,
      row_version: 2,
    };
    const second = workspace(
      "workspace-route-second",
      "둘째 작업",
      "CANDIDATE_REVIEW",
    );
    const failed = {
      ...idleJob,
      id: "compare-route-first",
      workspace_id: first.id,
      type: "COMPARE",
      status: "FAILED",
      stage: "FAILED",
      error: { code: "JOB_FAILED", message: "첫 작업 비교 실패", type: null },
    };
    type JobResponse = Awaited<
      ReturnType<ReturnType<typeof createFixtureApi>["getJob"]>
    >;
    let resolveOldCheck!: (job: JobResponse) => void;
    const oldCheck = new Promise<JobResponse>((resolve) => {
      resolveOldCheck = resolve;
    });
    let oldCheckCompleted = false;
    const secondCandidate = candidate(
      "candidate-route-second",
      "둘째 작업의 책",
      "NEEDS_REVIEW",
    );
    const api = candidateApi({
      getWorkspace: async (workspaceId: string) => {
        if (workspaceId === second.id) return { data: second, etag: '"1"' };
        return {
          data: oldCheckCompleted ? firstReviewed : first,
          etag: oldCheckCompleted ? '"2"' : '"1"',
        };
      },
      listWorkspaceJobs: async (workspaceId: string) => ({
        items: workspaceId === first.id ? [failed] : [],
        next_cursor: null,
      }),
      getJob: async () => await oldCheck,
      listCandidates: async (workspaceId: string, filters: { outcome: string }) => ({
        items:
          workspaceId === second.id && filters.outcome === "NEEDS_REVIEW"
            ? [secondCandidate]
            : [],
        next_cursor: null,
        total_count:
          workspaceId === second.id && filters.outcome === "NEEDS_REVIEW" ? 1 : 0,
        summary: {
          total_count: workspaceId === second.id ? 1 : 0,
          candidate_count: 0,
          needs_review_count: workspaceId === second.id ? 1 : 0,
          excluded_count: 0,
          unresolved_count: workspaceId === second.id ? 1 : 0,
          expected_total_won: 0,
        },
      }),
    });
    render(
      <MemoryRouter initialEntries={[`/workspaces/${first.id}`]}>
        <Link to={`/workspaces/${second.id}`}>둘째 작업 열기</Link>
        <App api={api} />
      </MemoryRouter>,
    );

    await user.click(
      await screen.findByRole("button", { name: "도서 비교 다시 시도" }),
    );
    await user.click(screen.getByRole("link", { name: "둘째 작업 열기" }));
    expect(await screen.findByText("둘째 작업의 책")).toBeVisible();
    act(() => {
      oldCheckCompleted = true;
      resolveOldCheck({
        ...failed,
        status: "SUCCEEDED",
        stage: "COMPLETED",
        error: null,
      });
    });
    await act(async () => undefined);

    expect(screen.getByText("둘째 작업의 책")).toBeVisible();
    expect(screen.getByRole("heading", { name: "후보 확인" })).toBeVisible();
  });

  test("확인 필요를 기본 우선하고 count·필터·exact ISBN 제외 사유와 되돌리기를 제공한다", async () => {
    const user = userEvent.setup();
    renderWorkroom(candidateApi(), candidateWorkspace.id);

    const needsReview = await screen.findByRole("tab", { name: "확인 필요 1" });
    expect(needsReview).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: "수서 후보 1" })).toBeVisible();
    expect(screen.getByRole("tab", { name: "제외된 책 1" })).toBeVisible();
    expect(screen.getByText("판본을 볼 책")).toBeVisible();

    await user.type(screen.getByRole("searchbox", { name: "후보 필터" }), "없는 제목");
    expect(screen.getByText("조건에 맞는 책이 없습니다.")).toBeVisible();
    await user.clear(screen.getByRole("searchbox", { name: "후보 필터" }));
    await user.click(screen.getByRole("tab", { name: "제외된 책 1" }));
    expect(screen.getByText("보유 장서와 ISBN이 정확히 일치합니다.")).toBeVisible();
    await user.click(
      screen.getByRole("button", { name: "이미 있는 책 제외 되돌리기" }),
    );
    expect(screen.getByRole("status")).toHaveTextContent("수서 후보로 되돌렸습니다");
  });

  test("outcome별 초기 응답이 엇갈리면 최신 행 판본에 맞춰 전체 요약도 갱신한다", async () => {
    const user = userEvent.setup();
    const older = {
      ...candidate(
        "candidate-interleaved-summary",
        "엇갈린 판본 책",
        "NEEDS_REVIEW",
        "판정을 기다립니다.",
      ),
      row_version: 2,
    };
    const newer = {
      ...older,
      outcome: "EXCLUDED",
      reason: "LIBRARIAN_DECISION",
      row_version: 3,
    };
    const olderSummary = {
      total_count: 1,
      candidate_count: 0,
      needs_review_count: 1,
      excluded_count: 0,
      unresolved_count: 1,
      expected_total_won: 0,
    };
    const newerSummary = {
      ...olderSummary,
      needs_review_count: 0,
      excluded_count: 1,
      unresolved_count: 0,
    };
    const api = candidateApi({
      listCandidates: async (
        _workspaceId: string,
        filters: { outcome: string },
      ) => ({
        items:
          filters.outcome === "NEEDS_REVIEW"
            ? [older]
            : filters.outcome === "EXCLUDED"
              ? [newer]
              : [],
        next_cursor: null,
        total_count: filters.outcome === "EXCLUDED" ? 1 : 0,
        summary:
          filters.outcome === "NEEDS_REVIEW" ? olderSummary : newerSummary,
      }),
    });
    renderWorkroom(api, candidateWorkspace.id);

    const excluded = await screen.findByRole("tab", { name: "제외된 책 1" });
    expect(screen.getByRole("tab", { name: "확인 필요 0" })).toBeVisible();
    await user.click(excluded);
    expect(await screen.findByText("엇갈린 판본 책")).toBeVisible();
    await user.type(screen.getByRole("spinbutton", { name: "승인 예산" }), "10000");
    expect(
      screen.getByRole("button", { name: "후보 확정하고 승인 요청" }),
    ).toBeEnabled();
  });

  test("탭 수·미해결·예상 금액은 서버 전체 요약을 쓰고 cursor 다음 후보를 이어 붙인다", async () => {
    const user = userEvent.setup();
    const first = candidate("candidate-page-1", "첫 페이지 책", "CANDIDATE");
    const second = candidate("candidate-page-2", "천 번째 책", "CANDIDATE");
    const api = candidateApi({
      listCandidates: async (_workspaceId: string, filters: { outcome: string; cursor?: string }) => ({
        items:
          filters.outcome === "CANDIDATE"
            ? filters.cursor
              ? [second]
              : [first]
            : [],
        next_cursor:
          filters.outcome === "CANDIDATE" && !filters.cursor ? "candidate-next" : null,
        total_count: filters.outcome === "CANDIDATE" ? 101 : 0,
        summary: {
          total_count: 102,
          candidate_count: 101,
          needs_review_count: 1,
          excluded_count: 0,
          unresolved_count: 1,
          expected_total_won: 555_000,
        },
      }),
    });
    renderWorkroom(api, candidateWorkspace.id);

    expect(await screen.findByRole("tab", { name: "수서 후보 101" })).toBeVisible();
    expect(screen.getByText("555,000원")).toBeVisible();
    await user.type(screen.getByRole("spinbutton", { name: "승인 예산" }), "600000");
    expect(
      screen.getByRole("button", { name: "후보 확정하고 승인 요청" }),
    ).toBeDisabled();
    expect(screen.getByText("확인 필요 1권의 판정을 먼저 마쳐 주세요.")).toBeVisible();

    await user.click(screen.getByRole("tab", { name: "수서 후보 101" }));
    expect(await screen.findByText("첫 페이지 책")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "수서 후보 더 보기" }));
    expect(await screen.findByText("천 번째 책")).toBeVisible();
  });

  test("다음 cursor가 같은 후보를 되돌려도 중복 표시하지 않는다", async () => {
    const user = userEvent.setup();
    const first = candidate("candidate-dedup", "한 번만 보일 책", "CANDIDATE");
    const second = candidate("candidate-next-unique", "다음 책", "CANDIDATE");
    const api = candidateApi({
      listCandidates: async (_workspaceId: string, filters: { outcome: string; cursor?: string }) => ({
        items:
          filters.outcome === "CANDIDATE"
            ? filters.cursor
              ? [first, second]
              : [first]
            : [],
        next_cursor:
          filters.outcome === "CANDIDATE" && !filters.cursor ? "dedup-next" : null,
        total_count: filters.outcome === "CANDIDATE" ? 2 : 0,
        summary: {
          total_count: 2,
          candidate_count: 2,
          needs_review_count: 0,
          excluded_count: 0,
          unresolved_count: 0,
          expected_total_won: 24_000,
        },
      }),
    });
    renderWorkroom(api, candidateWorkspace.id);

    await user.click(await screen.findByRole("tab", { name: "수서 후보 2" }));
    await user.click(screen.getByRole("button", { name: "수서 후보 더 보기" }));

    expect(await screen.findByText("다음 책")).toBeVisible();
    expect(screen.getAllByText("한 번만 보일 책")).toHaveLength(1);
  });

  test("판정 뒤 늦은 cursor가 예전 outcome을 보내도 이동한 후보와 권위 count를 되돌리지 않는다", async () => {
    const user = userEvent.setup();
    const moved = candidate(
      "candidate-moved-before-page",
      "이미 판정한 책",
      "NEEDS_REVIEW",
      "판정을 기다립니다.",
    );
    const later = candidate(
      "candidate-later-review",
      "다음 확인 책",
      "NEEDS_REVIEW",
      "확인이 필요합니다.",
    );
    const api = candidateApi({
      listCandidates: async (_workspaceId: string, filters: { outcome: string; cursor?: string }) => ({
        items:
          filters.outcome === "NEEDS_REVIEW"
            ? filters.cursor
              ? [moved, later]
              : [moved]
            : [],
        next_cursor:
          filters.outcome === "NEEDS_REVIEW" && !filters.cursor ? "review-next" : null,
        total_count: filters.outcome === "NEEDS_REVIEW" ? 2 : 0,
        summary: {
          total_count: 2,
          candidate_count: 0,
          needs_review_count: 2,
          excluded_count: 0,
          unresolved_count: 2,
          expected_total_won: 0,
        },
      }),
    });
    renderWorkroom(api, candidateWorkspace.id);

    await user.click(
      await screen.findByRole("button", { name: "이미 판정한 책 수서 후보로 포함" }),
    );
    expect(screen.getByRole("tab", { name: "확인 필요 1" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "확인 필요 더 보기" }));

    expect(await screen.findByText("다음 확인 책")).toBeVisible();
    expect(screen.queryByText("이미 판정한 책")).not.toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "확인 필요 1" })).toBeVisible();
  });

  test("판정 뒤 더 새로운 서버 판본은 다른 outcome과 전체 count를 함께 갱신한다", async () => {
    const user = userEvent.setup();
    const moved = candidate(
      "candidate-moved-newer-page",
      "다시 판정된 책",
      "NEEDS_REVIEW",
      "판정을 기다립니다.",
    );
    const excludedSeed = candidate(
      "candidate-excluded-seed",
      "기존 제외 책",
      "EXCLUDED",
      "EXACT_ISBN_MATCH",
    );
    const newer = {
      ...moved,
      outcome: "EXCLUDED",
      reason: "LIBRARIAN_DECISION",
      row_version: 3,
    };
    const initialSummary = {
      total_count: 2,
      candidate_count: 0,
      needs_review_count: 1,
      excluded_count: 1,
      unresolved_count: 1,
      expected_total_won: 0,
    };
    const api = candidateApi({
      listCandidates: async (
        _workspaceId: string,
        filters: { outcome: string; cursor?: string },
      ) => {
        if (filters.cursor === "excluded-next") {
          return {
            items: [newer],
            next_cursor: null,
            total_count: 2,
            summary: {
              ...initialSummary,
              candidate_count: 0,
              needs_review_count: 0,
              excluded_count: 2,
              unresolved_count: 0,
            },
          };
        }
        const items =
          filters.outcome === "NEEDS_REVIEW"
            ? [moved]
            : filters.outcome === "EXCLUDED"
              ? [excludedSeed]
              : [];
        return {
          items,
          next_cursor:
            filters.outcome === "EXCLUDED" ? "excluded-next" : null,
          total_count: items.length,
          summary: initialSummary,
        };
      },
    });
    renderWorkroom(api, candidateWorkspace.id);

    await user.click(
      await screen.findByRole("button", { name: "다시 판정된 책 수서 후보로 포함" }),
    );
    expect(screen.getByRole("tab", { name: "수서 후보 1" })).toBeVisible();
    await user.click(screen.getByRole("tab", { name: "제외된 책 1" }));
    await user.click(screen.getByRole("button", { name: "제외된 책 더 보기" }));

    expect(await screen.findByText("다시 판정된 책")).toBeVisible();
    expect(screen.getByRole("tab", { name: "수서 후보 0" })).toBeVisible();
    expect(screen.getByRole("tab", { name: "제외된 책 2" })).toBeVisible();
    expect(screen.getAllByText("다시 판정된 책")).toHaveLength(1);
  });

  test("cursor 행이 요약보다 새 판본이면 행 이동을 전체 count에도 한 번 반영한다", async () => {
    const user = userEvent.setup();
    const older = {
      ...candidate(
        "candidate-cursor-summary-skew",
        "cursor 요약 판본 책",
        "NEEDS_REVIEW",
        "판정을 기다립니다.",
      ),
      row_version: 2,
    };
    const newer = {
      ...older,
      outcome: "EXCLUDED",
      reason: "LIBRARIAN_DECISION",
      row_version: 3,
    };
    const excludedSeed = candidate(
      "candidate-cursor-summary-seed",
      "기존 cursor 제외 책",
      "EXCLUDED",
      "EXACT_ISBN_MATCH",
    );
    const olderSummary = {
      total_count: 2,
      candidate_count: 0,
      needs_review_count: 1,
      excluded_count: 1,
      unresolved_count: 1,
      expected_total_won: 0,
    };
    const api = candidateApi({
      listCandidates: async (
        _workspaceId: string,
        filters: { outcome: string; cursor?: string },
      ) => {
        if (filters.cursor === "skew-next") {
          return {
            items: [newer],
            next_cursor: null,
            total_count: 2,
            summary: olderSummary,
          };
        }
        return {
          items:
            filters.outcome === "NEEDS_REVIEW"
              ? [older]
              : filters.outcome === "EXCLUDED"
                ? [excludedSeed]
                : [],
          next_cursor: filters.outcome === "EXCLUDED" ? "skew-next" : null,
          total_count: filters.outcome === "EXCLUDED" ? 2 : 1,
          summary: olderSummary,
        };
      },
    });
    renderWorkroom(api, candidateWorkspace.id);

    await user.click(await screen.findByRole("tab", { name: "제외된 책 1" }));
    await user.click(screen.getByRole("button", { name: "제외된 책 더 보기" }));

    expect(await screen.findByText("cursor 요약 판본 책")).toBeVisible();
    expect(screen.getByRole("tab", { name: "확인 필요 0" })).toBeVisible();
    expect(screen.getByRole("tab", { name: "제외된 책 2" })).toBeVisible();
  });

  test("검색 재조회는 동일 판본을 되돌리지 않고 더 최신 판본만 받아들인다", async () => {
    const user = userEvent.setup();
    const original = candidate(
      "candidate-search-fence",
      "검색 판본 책",
      "NEEDS_REVIEW",
      "판정을 기다립니다.",
    );
    let phase: "equal" | "newer" = "equal";
    const api = candidateApi({
      listCandidates: async (
        _workspaceId: string,
        filters: { outcome: string; search?: string },
      ) => {
        if (!filters.search) {
          const included = filters.outcome === "NEEDS_REVIEW";
          return {
            items: included ? [original] : [],
            next_cursor: null,
            total_count: included ? 1 : 0,
            summary: {
              total_count: 1,
              candidate_count: 0,
              needs_review_count: 1,
              excluded_count: 0,
              unresolved_count: 1,
              expected_total_won: 0,
            },
          };
        }
        const outcome = phase === "equal" ? "NEEDS_REVIEW" : "EXCLUDED";
        const serverCandidate = {
          ...original,
          outcome,
          row_version: phase === "equal" ? 2 : 3,
        };
        const included = filters.outcome === outcome;
        return {
          items: included ? [serverCandidate] : [],
          next_cursor: null,
          total_count: included ? 1 : 0,
          summary: {
            total_count: 1,
            candidate_count: 0,
            needs_review_count: phase === "equal" ? 1 : 0,
            excluded_count: phase === "newer" ? 1 : 0,
            unresolved_count: phase === "equal" ? 1 : 0,
            expected_total_won: 0,
          },
        };
      },
    });
    renderWorkroom(api, candidateWorkspace.id);

    await user.click(
      await screen.findByRole("button", { name: "검색 판본 책 수서 후보로 포함" }),
    );
    const searchbox = screen.getByRole("searchbox", { name: "후보 필터" });
    await user.type(searchbox, "검색");
    expect(await screen.findByRole("tab", { name: "수서 후보 1" })).toBeVisible();
    expect(screen.getByRole("tab", { name: "확인 필요 0" })).toBeVisible();
    await user.click(screen.getByRole("tab", { name: "수서 후보 1" }));
    expect(screen.getByText("검색 판본 책")).toBeVisible();

    phase = "newer";
    await user.clear(searchbox);
    await user.type(searchbox, "판본");
    expect(await screen.findByRole("tab", { name: "수서 후보 0" })).toBeVisible();
    await user.click(screen.getByRole("tab", { name: "제외된 책 1" }));
    expect(await screen.findByText("검색 판본 책")).toBeVisible();
  });

  test("이전 cursor 응답은 새 검색의 후보와 전체 금액을 되돌리지 않는다", async () => {
    const user = userEvent.setup();
    type Page = Awaited<
      ReturnType<ReturnType<typeof createFixtureApi>["listCandidates"]>
    >;
    let resolveOldPage: ((page: Page) => void) | undefined;
    const emptySummary = {
      total_count: 0,
      candidate_count: 0,
      needs_review_count: 0,
      excluded_count: 0,
      unresolved_count: 0,
      expected_total_won: 0,
    };
    const api = candidateApi({
      listCandidates: async (
        _workspaceId: string,
        filters: { outcome: string; search?: string; cursor?: string },
      ) => {
        if (filters.cursor === "old-next") {
          return await new Promise<Page>((resolve) => {
            resolveOldPage = resolve;
          });
        }
        if (filters.search === "새 검색") {
          const isCandidate = filters.outcome === "CANDIDATE";
          return {
            items: isCandidate
              ? [candidate("candidate-new-query", "새 검색 결과", "CANDIDATE")]
              : [],
            next_cursor: null,
            total_count: isCandidate ? 1 : 0,
            summary: {
              ...emptySummary,
              total_count: 1,
              candidate_count: 1,
              expected_total_won: 12_000,
            },
          };
        }
        const isCandidate = filters.outcome === "CANDIDATE";
        return {
          items: isCandidate
            ? [candidate("candidate-old-first", "이전 첫 페이지", "CANDIDATE")]
            : [],
          next_cursor: isCandidate ? "old-next" : null,
          total_count: isCandidate ? 101 : 0,
          summary: {
            ...emptySummary,
            total_count: 101,
            candidate_count: 101,
            expected_total_won: 1_212_000,
          },
        };
      },
    });
    renderWorkroom(api, candidateWorkspace.id);
    await user.click(await screen.findByRole("tab", { name: "수서 후보 101" }));
    await user.click(screen.getByRole("button", { name: "수서 후보 더 보기" }));
    await waitFor(() => expect(resolveOldPage).toBeDefined());

    await user.type(screen.getByRole("searchbox", { name: "후보 필터" }), "새 검색");
    expect(await screen.findByText("새 검색 결과")).toBeVisible();
    expect(screen.getByRole("tab", { name: "수서 후보 1" })).toBeVisible();
    expect(screen.getByText(/예상 금액/u)).toHaveTextContent("예상 금액 12,000원");

    act(() =>
      resolveOldPage?.({
        items: [candidate("candidate-old-late", "늦은 이전 결과", "CANDIDATE")],
        next_cursor: null,
        total_count: 999,
        summary: {
          ...emptySummary,
          total_count: 999,
          candidate_count: 999,
          expected_total_won: 9_990_000,
        },
      }),
    );
    await act(async () => undefined);
    expect(screen.queryByText("늦은 이전 결과")).not.toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "수서 후보 1" })).toBeVisible();
    expect(screen.getByText(/예상 금액/u)).toHaveTextContent("예상 금액 12,000원");
  });

  test("저장된 수량과 금액은 먼저 시작한 cursor 응답보다 우선한다", async () => {
    const user = userEvent.setup();
    type Page = Awaited<
      ReturnType<ReturnType<typeof createFixtureApi>["listCandidates"]>
    >;
    let resolveOldPage: ((page: Page) => void) | undefined;
    type CandidateUpdate = Awaited<
      ReturnType<ReturnType<typeof createFixtureApi>["updateCandidate"]>
    >;
    let resolveSave: ((updated: CandidateUpdate) => void) | undefined;
    const current = candidate("candidate-race-save", "저장 우선 책", "CANDIDATE");
    const summary = {
      total_count: 1,
      candidate_count: 1,
      needs_review_count: 0,
      excluded_count: 0,
      unresolved_count: 0,
      expected_total_won: 12_000,
    };
    const api = candidateApi({
      listCandidates: async (
        _workspaceId: string,
        filters: { outcome: string; search?: string; cursor?: string },
      ) => {
        if (filters.cursor === "save-race-next") {
          return await new Promise<Page>((resolve) => {
            resolveOldPage = resolve;
          });
        }
        const isCandidate = filters.outcome === "CANDIDATE";
        return {
          items: isCandidate ? [current] : [],
          next_cursor: isCandidate ? "save-race-next" : null,
          total_count: isCandidate ? 1 : 0,
          summary,
        };
      },
      updateCandidate: async () =>
        await new Promise<CandidateUpdate>((resolve) => {
          resolveSave = resolve;
        }),
    });
    renderWorkroom(api, candidateWorkspace.id);
    await user.click(await screen.findByRole("tab", { name: "수서 후보 1" }));
    await user.click(screen.getByRole("button", { name: "수서 후보 더 보기" }));
    await waitFor(() => expect(resolveOldPage).toBeDefined());

    const quantity = screen.getByRole("spinbutton", { name: "저장 우선 책 수량" });
    await user.click(quantity);
    await waitFor(() => expect(quantity).not.toHaveAttribute("readonly"));
    await user.clear(quantity);
    await user.type(quantity, "2");
    await user.tab();
    await waitFor(() => expect(resolveSave).toBeDefined());

    act(() =>
      resolveOldPage?.({
        items: [candidate("candidate-stale-save", "늦은 저장 전 행", "CANDIDATE")],
        next_cursor: null,
        total_count: 999,
        summary: {
          ...summary,
          total_count: 999,
          candidate_count: 999,
          expected_total_won: 9_990_000,
        },
      }),
    );
    await act(async () => undefined);
    expect(screen.queryByText("늦은 저장 전 행")).not.toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "수서 후보 1" })).toBeVisible();

    act(() =>
      resolveSave?.({
        data: { ...current, quantity: 2, row_version: 2 },
        etag: '"2"',
      }),
    );
    expect(await screen.findByText(/저장됨 \d{2}:\d{2}/u)).toBeVisible();
    expect(quantity).toHaveValue(2);
    expect(screen.queryByText("늦은 저장 전 행")).not.toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "수서 후보 1" })).toBeVisible();
    expect(screen.getByText(/예상 금액/u)).toHaveTextContent("예상 금액 24,000원");
  });

  test("저장 요청 뒤 시작한 서버 검색도 저장 응답과 같은 변경을 두 번 합산하지 않는다", async () => {
    const user = userEvent.setup();
    const current = candidate(
      "candidate-commit-before-response",
      "응답 기다리는 책",
      "CANDIDATE",
    );
    type CandidateUpdate = Awaited<
      ReturnType<ReturnType<typeof createFixtureApi>["updateCandidate"]>
    >;
    let resolveSave: ((updated: CandidateUpdate) => void) | undefined;
    let committedSearches = 0;
    const api = candidateApi({
      listCandidates: async (
        _workspaceId: string,
        filters: { outcome: string; search?: string },
      ) => {
        const committed = filters.search === "응답";
        if (committed) committedSearches += 1;
        const included = filters.outcome === "CANDIDATE";
        return {
          items: included
            ? [{ ...current, quantity: committed ? 2 : 1, row_version: committed ? 2 : 1 }]
            : [],
          next_cursor: null,
          total_count: included ? 1 : 0,
          summary: {
            total_count: 1,
            candidate_count: 1,
            needs_review_count: 0,
            excluded_count: 0,
            unresolved_count: 0,
            expected_total_won: committed ? 24_000 : 12_000,
          },
        };
      },
      updateCandidate: async () =>
        await new Promise<CandidateUpdate>((resolve) => {
          resolveSave = resolve;
        }),
    });
    renderWorkroom(api, candidateWorkspace.id);
    await user.click(await screen.findByRole("tab", { name: "수서 후보 1" }));
    const quantity = screen.getByRole("spinbutton", { name: "응답 기다리는 책 수량" });
    await user.click(quantity);
    await waitFor(() => expect(quantity).not.toHaveAttribute("readonly"));
    await user.clear(quantity);
    await user.type(quantity, "2");
    await user.tab();
    await waitFor(() => expect(resolveSave).toBeDefined());

    await user.type(screen.getByRole("searchbox", { name: "후보 필터" }), "응답");
    await waitFor(() => expect(committedSearches).toBe(3));
    act(() =>
      resolveSave?.({
        data: { ...current, quantity: 2, row_version: 2 },
        etag: '"2"',
      }),
    );

    expect(await screen.findByText(/저장됨 \d{2}:\d{2}/u)).toBeVisible();
    expect(screen.getByText(/예상 금액/u)).toHaveTextContent("예상 금액 24,000원");
    expect(screen.getAllByText("응답 기다리는 책")).toHaveLength(1);
  });

  test("후보 검색은 브라우저 첫 페이지가 아니라 서버 필터를 다시 요청한다", async () => {
    const user = userEvent.setup();
    const filtersSeen: Array<{ outcome: string; search?: string }> = [];
    const api = candidateApi({
      listCandidates: async (_workspaceId: string, filters: { outcome: string; search?: string }) => {
        filtersSeen.push(filters);
        return {
          items: [],
          next_cursor: null,
          total_count: 0,
          summary: {
            total_count: 0,
            candidate_count: 0,
            needs_review_count: 0,
            excluded_count: 0,
            unresolved_count: 0,
            expected_total_won: 0,
          },
        };
      },
    });
    renderWorkroom(api, candidateWorkspace.id);
    await screen.findByRole("tab", { name: "확인 필요 0" });
    await user.type(screen.getByRole("searchbox", { name: "후보 필터" }), "마법 학교");

    await waitFor(() => {
      expect(filtersSeen.some((filters) => filters.search === "마법 학교")).toBe(true);
    });
  });

  test("느린 이전 검색 응답은 최신 검색 결과와 전체 요약을 되돌리지 않는다", async () => {
    const user = userEvent.setup();
    type Page = Awaited<ReturnType<ReturnType<typeof createFixtureApi>["listCandidates"]>>;
    const slowResolvers: Array<(page: Page) => void> = [];
    const emptySummary = {
      total_count: 0,
      candidate_count: 0,
      needs_review_count: 0,
      excluded_count: 0,
      unresolved_count: 0,
      expected_total_won: 0,
    };
    const api = candidateApi({
      listCandidates: async (_workspaceId: string, filters: { outcome: string; search?: string }) => {
        if (filters.search === "느림") {
          return await new Promise<Page>((resolve) => slowResolvers.push(resolve));
        }
        const fastSearch = filters.search === "빠름";
        const fast = fastSearch && filters.outcome === "CANDIDATE";
        return {
          items: fast ? [candidate("candidate-fast", "빠름 결과", "CANDIDATE")] : [],
          next_cursor: null,
          total_count: fast ? 1 : 0,
          summary: fastSearch
            ? { ...emptySummary, total_count: 1, candidate_count: 1, expected_total_won: 12_000 }
            : emptySummary,
        };
      },
    });
    renderWorkroom(api, candidateWorkspace.id);
    const search = await screen.findByRole("searchbox", { name: "후보 필터" });
    await user.type(search, "느림");
    await waitFor(() => expect(slowResolvers).toHaveLength(3));
    await user.clear(search);
    await user.type(search, "빠름");
    await user.click(await screen.findByRole("tab", { name: "수서 후보 1" }));
    expect(await screen.findByText("빠름 결과")).toBeVisible();

    act(() => {
      for (const resolve of slowResolvers) {
        resolve({
          items: [candidate("candidate-slow", "느림 결과", "CANDIDATE")],
          next_cursor: null,
          total_count: 999,
          summary: {
            ...emptySummary,
            total_count: 999,
            candidate_count: 999,
            expected_total_won: 9_990_000,
          },
        });
      }
    });
    await act(async () => undefined);
    expect(screen.getByText("빠름 결과")).toBeVisible();
    expect(screen.queryByText("느림 결과")).not.toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "수서 후보 1" })).toBeVisible();
  });

  test("확인 필요 판정을 끝내고 보이는 승인 예산을 입력해야 승인 요청할 수 있다", async () => {
    const user = userEvent.setup();
    renderWorkroom(candidateApi(), candidateWorkspace.id);

    const approval = await screen.findByRole("button", {
      name: "후보 확정하고 승인 요청",
    });
    expect(approval).toBeDisabled();
    await user.click(
      await screen.findByRole("button", { name: "판본을 볼 책 수서 후보로 포함" }),
    );

    expect(screen.getByRole("status")).toHaveTextContent("수서 후보로 옮겼습니다");
    expect(screen.getByRole("tab", { name: "확인 필요 0" })).toBeVisible();
    expect(screen.getByRole("tab", { name: "수서 후보 2" })).toBeVisible();
    await user.type(screen.getByRole("spinbutton", { name: "승인 예산" }), "30000");
    expect(approval).toBeEnabled();
    await user.click(approval);
    expect(screen.getByRole("status")).toHaveTextContent(
      "승인을 요청했습니다. 검토 담당자에게 전달했습니다.",
    );
  });

  test("blur 자동 저장은 저장 시각과 잠금을 알리고 오프라인에서는 입력값을 지킨다", async () => {
    const user = userEvent.setup();
    renderWorkroom(candidateApi(), candidateWorkspace.id);
    await user.click(await screen.findByRole("tab", { name: "수서 후보 1" }));
    const quantity = screen.getByRole("spinbutton", { name: "새로 살 책 수량" });

    await user.click(quantity);
    expect(await screen.findByText(/김사서님이 편집 중 · .*까지/)).toBeVisible();
    await user.clear(quantity);
    await user.type(quantity, "3");
    await user.tab();
    expect(await screen.findByText(/저장됨 \d{2}:\d{2}/)).toBeVisible();
    expect(quantity).toHaveValue(3);

    Object.defineProperty(window.navigator, "onLine", {
      configurable: true,
      value: false,
    });
    await user.click(quantity);
    await user.clear(quantity);
    await user.type(quantity, "4");
    await user.tab();
    expect(screen.getByRole("status")).toHaveTextContent(
      "연결이 끊겨 변경하지 않았습니다. 입력한 내용은 그대로 보관합니다.",
    );
    expect(quantity).toHaveValue(4);
  });

  test("편집 잠금을 얻기 전에는 값을 바꿀 수 없다", async () => {
    const user = userEvent.setup();
    let releaseLock: (() => void) | undefined;
    const lock = new Promise<Awaited<ReturnType<ReturnType<typeof createFixtureApi>["lockCandidate"]>>>(
      (resolve) => {
        releaseLock = () =>
          resolve({
            candidate_id: "candidate-ok",
            actor_id: operator.id,
            expires_at: "2026-08-29T08:07:00.000000Z",
          });
      },
    );
    renderWorkroom(
      candidateApi({ lockCandidate: async () => await lock }),
      candidateWorkspace.id,
    );
    await user.click(await screen.findByRole("tab", { name: "수서 후보 1" }));
    const quantity = screen.getByRole("spinbutton", { name: "새로 살 책 수량" });
    expect(quantity).toHaveAttribute("readonly");
    await user.click(quantity);
    await user.keyboard("9");
    expect(quantity).toHaveValue(1);

    act(() => releaseLock?.());
    await waitFor(() => expect(quantity).not.toHaveAttribute("readonly"));
  });

  test("빠른 두 번의 blur는 직렬 저장하고 이전 응답이 최신 입력을 덮지 않는다", async () => {
    const user = userEvent.setup();
    type MutationResult = Awaited<ReturnType<ReturnType<typeof createFixtureApi>["updateCandidate"]>>;
    const calls: Array<{ quantity: number | undefined; version: number }> = [];
    const resolvers: Array<(value: MutationResult) => void> = [];
    const api = candidateApi({
      updateCandidate: async (_candidateId: string, input: { changes: { quantity?: number } }, version: number) => {
        calls.push({ quantity: input.changes.quantity, version });
        return await new Promise<MutationResult>((resolve) => resolvers.push(resolve));
      },
    });
    renderWorkroom(api, candidateWorkspace.id);
    await user.click(await screen.findByRole("tab", { name: "수서 후보 1" }));
    const quantity = screen.getByRole("spinbutton", { name: "새로 살 책 수량" });
    await user.click(quantity);
    await waitFor(() => expect(quantity).not.toHaveAttribute("readonly"));
    await user.clear(quantity);
    await user.type(quantity, "2");
    await user.tab();
    await user.click(quantity);
    await user.clear(quantity);
    await user.type(quantity, "3");
    await user.tab();
    expect(calls).toEqual([{ quantity: 2, version: 1 }]);

    act(() =>
      resolvers[0]?.({
        data: {
          id: "candidate-ok",
          outcome: "CANDIDATE",
          quantity: 2,
          unit_price: 12_000,
          row_version: 2,
        },
        etag: '"2"',
      }),
    );
    await waitFor(() => expect(calls).toEqual([
      { quantity: 2, version: 1 },
      { quantity: 3, version: 2 },
    ]));
    expect(quantity).toHaveValue(3);
    act(() =>
      resolvers[1]?.({
        data: {
          id: "candidate-ok",
          outcome: "CANDIDATE",
          quantity: 3,
          unit_price: 12_000,
          row_version: 3,
        },
        etag: '"3"',
      }),
    );
    expect(await screen.findByText(/저장됨 \d{2}:\d{2}/)).toBeVisible();
    expect(quantity).toHaveValue(3);
  });

  test("수량 blur와 판정 클릭은 한 줄로 직렬화하고 판정은 저장된 새 버전을 쓴다", async () => {
    const user = userEvent.setup();
    type MutationResult = Awaited<ReturnType<ReturnType<typeof createFixtureApi>["updateCandidate"]>>;
    const calls: Array<{
      quantity: number | undefined;
      outcome: string | undefined;
      version: number;
    }> = [];
    const resolvers: Array<(value: MutationResult) => void> = [];
    const api = candidateApi({
      updateCandidate: async (
        _candidateId: string,
        input: { changes: { quantity?: number; outcome?: string } },
        version: number,
      ) => {
        calls.push({
          quantity: input.changes.quantity,
          outcome: input.changes.outcome,
          version,
        });
        return await new Promise<MutationResult>((resolve) => resolvers.push(resolve));
      },
    });
    renderWorkroom(api, candidateWorkspace.id);
    const quantity = await screen.findByRole("spinbutton", { name: "판본을 볼 책 수량" });
    await user.click(quantity);
    await waitFor(() => expect(quantity).not.toHaveAttribute("readonly"));
    await user.clear(quantity);
    await user.type(quantity, "2");
    await user.click(
      screen.getByRole("button", { name: "판본을 볼 책 수서 후보로 포함" }),
    );

    expect(calls).toEqual([{ quantity: 2, outcome: undefined, version: 1 }]);
    act(() =>
      resolvers[0]?.({
        data: {
          id: "candidate-review",
          outcome: "NEEDS_REVIEW",
          quantity: 2,
          unit_price: 12_000,
          row_version: 2,
        },
        etag: '"2"',
      }),
    );
    await waitFor(() =>
      expect(calls).toEqual([
        { quantity: 2, outcome: undefined, version: 1 },
        { quantity: undefined, outcome: "CANDIDATE", version: 2 },
      ]),
    );
    act(() =>
      resolvers[1]?.({
        data: {
          id: "candidate-review",
          outcome: "CANDIDATE",
          quantity: 2,
          unit_price: 12_000,
          row_version: 3,
        },
        etag: '"3"',
      }),
    );

    expect(await screen.findByRole("tab", { name: "확인 필요 0" })).toBeVisible();
    expect(screen.getByRole("tab", { name: "수서 후보 2" })).toBeVisible();
  });

  test("편집 잠금 시간이 지나면 입력을 다시 잠그고 다음 수정 전에 새 잠금을 받는다", async () => {
    const user = userEvent.setup();
    let locks = 0;
    const api = candidateApi({
      lockCandidate: async (candidateId: string) => {
        locks += 1;
        return {
          candidate_id: candidateId,
          actor_id: operator.id,
          expires_at: new Date(Date.now() + 30).toISOString(),
        };
      },
    });
    renderWorkroom(api, candidateWorkspace.id);
    await user.click(await screen.findByRole("tab", { name: "수서 후보 1" }));
    const quantity = screen.getByRole("spinbutton", { name: "새로 살 책 수량" });
    await user.click(quantity);
    await waitFor(() => expect(quantity).not.toHaveAttribute("readonly"));
    await waitFor(() => expect(quantity).toHaveAttribute("readonly"), { timeout: 500 });
    await user.click(quantity);
    await waitFor(() => expect(locks).toBe(2));
  });

  test("제외 되돌리기도 먼저 해당 후보 편집 잠금을 얻는다", async () => {
    const user = userEvent.setup();
    const calls: string[] = [];
    const api = candidateApi({
      lockCandidate: async (candidateId: string) => {
        calls.push(`lock:${candidateId}`);
        return {
          candidate_id: candidateId,
          actor_id: operator.id,
          expires_at: new Date(Date.now() + 120_000).toISOString(),
        };
      },
      updateCandidate: async (candidateId: string, input: { changes: { outcome?: string } }, version: number) => {
        calls.push(`update:${candidateId}:${input.changes.outcome}:${version}`);
        return {
          data: {
            id: candidateId,
            outcome: "CANDIDATE",
            quantity: 1,
            unit_price: 12_000,
            row_version: 2,
          },
          etag: '"2"',
        };
      },
    });
    renderWorkroom(api, candidateWorkspace.id);
    await user.click(await screen.findByRole("tab", { name: "제외된 책 1" }));
    await user.click(screen.getByRole("button", { name: "이미 있는 책 제외 되돌리기" }));

    await waitFor(() =>
      expect(calls).toEqual([
        "lock:candidate-excluded",
        "update:candidate-excluded:CANDIDATE:1",
      ]),
    );
  });

  test("412는 현재 내용과 내 변경을 나란히 보여주고 dialog를 닫으면 초점을 돌려준다", async () => {
    const user = userEvent.setup();
    const api = candidateApi({
      updateCandidate: async () => {
        throw new FixtureApiError(
          412,
          "ROW_VERSION_CONFLICT",
          "다른 사용자가 먼저 수정했습니다. 현재 내용과 변경 내용을 확인해 주세요.",
          [
            { field: "current", value: 2, message: null },
            { field: "submitted", value: 1, message: null },
          ],
        );
      },
      getCandidate: async () => ({
        data: {
          ...candidate("candidate-ok", "새로 살 책", "CANDIDATE"),
          quantity: 5,
          row_version: 7,
        },
        etag: '"7"',
      }),
    });
    renderWorkroom(api, candidateWorkspace.id);
    await user.click(await screen.findByRole("tab", { name: "수서 후보 1" }));
    const quantity = screen.getByRole("spinbutton", { name: "새로 살 책 수량" });
    await user.click(quantity);
    await waitFor(() => expect(quantity).not.toHaveAttribute("readonly"));
    await user.clear(quantity);
    await user.type(quantity, "2");
    await user.tab();

    const dialog = await screen.findByRole("dialog", { name: "수정 내용 충돌" });
    expect(within(dialog).getByRole("heading", { name: "현재 저장 내용" })).toBeVisible();
    expect(within(dialog).getByRole("heading", { name: "내 변경" })).toBeVisible();
    expect(within(dialog).getByText("수량 5권")).toBeVisible();
    expect(within(dialog).getByText("수량 2권")).toBeVisible();
    expect(within(dialog).getByRole("button", { name: "현재 내용 사용" })).toHaveFocus();
    await user.click(within(dialog).getByRole("button", { name: "현재 내용 사용" }));

    expect(dialog).not.toBeInTheDocument();
    expect(quantity).toHaveFocus();
    expect(quantity).toHaveValue(5);
  });

  test("후보 확정 주요 카피를 그대로 쓰고 저장·다음 이동 버튼은 두지 않는다", async () => {
    renderWorkroom(candidateApi(), candidateWorkspace.id);

    expect(
      await screen.findByRole("button", { name: "후보 확정하고 승인 요청" }),
    ).toBeVisible();
    expect(screen.queryByRole("button", { name: /^저장$/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^다음$/ })).not.toBeInTheDocument();
  });
});
