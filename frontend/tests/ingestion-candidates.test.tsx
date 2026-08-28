import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode } from "react";
import { MemoryRouter } from "react-router-dom";
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
                }
              : {
                  filename: file.name,
                  status: "ACCEPTED",
                  source_id: "source-books",
                  error: null,
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
                },
                {
                  filename: "repair.exe",
                  status: "FAILED",
                  source_id: null,
                  error: { code: "FILE_TOO_LARGE", message: "파일을 확인해 주세요." },
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
      error: { code: "MAPPING_REQUIRED", message: "열 연결 확인", type: null },
      mapping_required: mappingRequired,
    });
    const api = createFixtureApi({
      uploadSources: async () => ({
        job_id: "ingest-two-mappings",
        items: [
          { filename: "first.xlsx", status: "ACCEPTED", source_id: "source-first", error: null },
          { filename: "second.xlsx", status: "ACCEPTED", source_id: "source-second", error: null },
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
