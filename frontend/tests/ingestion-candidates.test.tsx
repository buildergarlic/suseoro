import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, test } from "vitest";

import { App } from "../src/app/App";
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
  test("현재 과정만 펼치고 여러 파일의 진행·부분 실패·재시도·취소를 글로 알린다", async () => {
    const user = userEvent.setup();
    const draft = workspace("workspace-draft", "여름방학 추천도서", "DRAFT");
    const api = createFixtureApi({
      getWorkspace: async () => ({ data: draft, etag: '"1"' }),
      uploadSources: async () => ({
        job_id: "job-partial",
        items: [
          {
            filename: "books.csv",
            status: "ACCEPTED",
            source_id: "source-books",
            error: null,
          },
          {
            filename: "broken.exe",
            status: "FAILED",
            source_id: null,
            error: {
              code: "UNSUPPORTED_FILE_TYPE",
              message: "지원하지 않는 파일 형식입니다.",
            },
          },
        ],
      }),
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
            status: "SUCCEEDED",
            total_rows: 2,
            processed_rows: 2,
            error: null,
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
    expect(books).toHaveTextContent("분석 중");
    expect(books).toHaveTextContent("50%");
    expect(books).toHaveTextContent("2권 읽음");
    expect(books).toHaveTextContent("확인 필요 0");
    expect(within(books).getByRole("button", { name: "books.csv 취소" })).toBeVisible();

    const broken = screen.getByRole("listitem", { name: "broken.exe 처리 상태" });
    expect(broken).toHaveTextContent("읽지 못함");
    expect(broken).toHaveTextContent("지원하지 않는 파일 형식입니다.");
    expect(
      within(broken).getByRole("button", { name: "broken.exe 다시 시도" }),
    ).toBeVisible();
    expect(screen.getByRole("status")).toHaveTextContent("1개 파일은 확인이 필요합니다");
  });

  test("붙여넣은 글을 메모리에 보존하고 낮은 신뢰도 양식은 20행 열 연결 dialog로 연다", async () => {
    const user = userEvent.setup();
    const draft = workspace("workspace-draft", "낯선 추천양식", "DRAFT");
    const lines = ["책이름,쓴이", ...Array.from({ length: 25 }, (_, i) => `책 ${i + 1},저자 ${i + 1}`)];
    const api = createFixtureApi({
      getWorkspace: async () => ({ data: draft, etag: '"1"' }),
      uploadSources: async () => ({
        job_id: "job-mapping",
        items: [
          {
            filename: "붙여넣은-자료.txt",
            status: "ACCEPTED",
            source_id: sourceFixture.id,
            error: null,
          },
        ],
      }),
      getJob: async () => ({
        ...idleJob,
        id: "job-mapping",
        status: "PARTIAL",
        stage: "MAPPING",
        items: [
          {
            source_document_id: sourceFixture.id,
            filename: "붙여넣은-자료.txt",
            status: "NEEDS_MAPPING",
            total_rows: 25,
            processed_rows: 0,
            error: {
              code: "MAPPING_REQUIRED",
              message: "열 연결을 확인해 주세요.",
              type: "MAPPING",
            },
          },
        ],
      }),
      getSource: async () => ({ data: sourceFixture, etag: '"1"' }),
    });
    renderWorkroom(api, draft.id);

    const pasted = await screen.findByLabelText("추천자료 글 붙여넣기");
    await user.type(pasted, lines.join("\n"));
    await user.click(
      screen.getByRole("button", { name: "우리 도서관에 없는 책 찾기" }),
    );

    const dialog = await screen.findByRole("dialog", { name: "열 연결 확인" });
    expect(within(dialog).getAllByRole("row")).toHaveLength(21);
    expect(within(dialog).getByLabelText("이 양식 기억")).toBeChecked();
    expect(within(dialog).getByLabelText("제목 열")).toHaveValue("책이름");
    expect(within(dialog).getByLabelText("저자 열")).toHaveValue("쓴이");
    await user.click(within(dialog).getByRole("button", { name: "열 연결 적용" }));

    await waitFor(() => expect(dialog).not.toBeInTheDocument());
    expect(screen.getByRole("status")).toHaveTextContent("열 연결을 저장했습니다");
    expect(pasted).toHaveValue(lines.join("\n"));
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
    return createFixtureApi({
      getCurrentUser: async () => operator,
      getWorkspace: async () => ({ data: candidateWorkspace, etag: '"1"' }),
      listCandidates: async (_workspaceId, filters) => ({
        items: candidates[filters.outcome as keyof typeof candidates] ?? [],
        next_cursor: null,
      }),
      ...overrides,
    });
  }

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
    });
    renderWorkroom(api, candidateWorkspace.id);
    await user.click(await screen.findByRole("tab", { name: "수서 후보 1" }));
    const quantity = screen.getByRole("spinbutton", { name: "새로 살 책 수량" });
    await user.clear(quantity);
    await user.type(quantity, "2");
    await user.tab();

    const dialog = await screen.findByRole("dialog", { name: "수정 내용 충돌" });
    expect(within(dialog).getByRole("heading", { name: "현재 저장 내용" })).toBeVisible();
    expect(within(dialog).getByRole("heading", { name: "내 변경" })).toBeVisible();
    expect(within(dialog).getByText("수량 1권")).toBeVisible();
    expect(within(dialog).getByText("수량 2권")).toBeVisible();
    expect(within(dialog).getByRole("button", { name: "현재 내용 사용" })).toHaveFocus();
    await user.click(within(dialog).getByRole("button", { name: "현재 내용 사용" }));

    expect(dialog).not.toBeInTheDocument();
    expect(quantity).toHaveFocus();
    expect(quantity).toHaveValue(1);
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
