import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, test } from "vitest";

import { App } from "../src/app/App";
import {
  createFixtureApi,
  FixtureApiError,
  operator,
  reviewer,
  workspace,
} from "../src/test/fixtures";

function renderApp(
  api: ReturnType<typeof createFixtureApi>,
  initialPath = "/workspaces",
) {
  return render(
    <MemoryRouter initialEntries={[initialPath]}>
      <App api={api} />
    </MemoryRouter>,
  );
}

describe("로그인", () => {
  test("잘못된 로그인은 자연스러운 오류를 알리고 입력을 보존한다", async () => {
    const user = userEvent.setup();
    const api = createFixtureApi({
      getCurrentUser: async () => {
        throw new FixtureApiError(401, "AUTHENTICATION_REQUIRED", "로그인이 필요합니다.");
      },
      login: async () => {
        throw new FixtureApiError(
          401,
          "INVALID_CREDENTIALS",
          "학교, 아이디 또는 비밀번호를 다시 확인해 주세요.",
        );
      },
    });
    renderApp(api, "/login");

    await screen.findByRole("heading", { level: 1, name: "로그인" });
    await user.type(screen.getByLabelText("학교 코드"), "school-01");
    await user.type(screen.getByLabelText("아이디"), "librarian");
    await user.type(screen.getByLabelText("비밀번호"), "wrong password");
    await user.click(screen.getByRole("button", { name: "로그인" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "학교, 아이디 또는 비밀번호를 다시 확인해 주세요.",
    );
    expect(screen.getByLabelText("학교 코드")).toHaveValue("school-01");
    expect(screen.getByLabelText("아이디")).toHaveValue("librarian");
    expect(screen.getAllByRole("button")).toHaveLength(1);
  });

  test("로그인 성공 뒤 담당자에게 이어서 할 작업 하나를 먼저 보여준다", async () => {
    const user = userEvent.setup();
    const workspaces = [
      workspace("waiting", "승인 기다리는 목록", "APPROVAL_PENDING"),
      workspace("review", "살펴보던 후보", "CANDIDATE_REVIEW"),
    ];
    const api = createFixtureApi({
      getCurrentUser: async () => {
        throw new FixtureApiError(401, "AUTHENTICATION_REQUIRED", "로그인이 필요합니다.");
      },
      login: async () => operator,
      listWorkspaces: async () => ({ items: workspaces, next_cursor: null }),
    });
    renderApp(api, "/login");

    await user.type(await screen.findByLabelText("학교 코드"), "school-01");
    await user.type(screen.getByLabelText("아이디"), "operator");
    await user.type(screen.getByLabelText("비밀번호"), "correct password");
    await user.click(screen.getByRole("button", { name: "로그인" }));

    expect(
      await screen.findByRole("heading", { level: 1, name: "내 수서 업무" }),
    ).toBeVisible();
    expect(screen.getByText("살펴보던 후보")).toBeVisible();
    expect(screen.getAllByRole("link", { name: "이어 하기" })).toHaveLength(1);
  });
});

describe("역할별 내 수서 업무", () => {
  const workspaces = [
    workspace("done", "지난 봄 수서", "COMPLETED", "2026-08-27T08:00:00Z"),
    workspace("approval", "승인할 신간", "APPROVAL_PENDING", "2026-08-29T06:00:00Z"),
    workspace("draft", "이어 할 추천 목록", "DRAFT", "2026-08-28T07:00:00Z"),
  ];

  test("담당자는 이어서 할 작업을 먼저 정렬하고 다섯 정보만 표로 보여준다", async () => {
    const user = userEvent.setup();
    const api = createFixtureApi({
      getCurrentUser: async () => operator,
      listWorkspaces: async () => ({ items: workspaces, next_cursor: null }),
    });
    renderApp(api);

    const table = await screen.findByRole("table", { name: "수서 업무 목록" });
    const headers = within(table)
      .getAllByRole("columnheader")
      .map((header) => header.textContent);
    expect(headers).toEqual(["학교", "작업명", "상태", "다음 담당", "마지막 저장"]);
    const rows = within(table).getAllByRole("row").slice(1);
    expect(within(rows[0]).getByText("이어 할 추천 목록")).toBeVisible();
    expect(within(rows[0]).getByText("담당자")).toBeVisible();
    expect(screen.getAllByRole("link", { name: "이어 하기" })).toHaveLength(1);

    await user.tab();
    expect(screen.getByRole("link", { name: "본문으로 건너뛰기" })).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(screen.getByRole("main")).toHaveFocus();
  });

  test("검토자는 승인할 작업을 먼저 정렬하고 주요 행동을 하나만 둔다", async () => {
    const api = createFixtureApi({
      getCurrentUser: async () => reviewer,
      listWorkspaces: async () => ({ items: workspaces, next_cursor: null }),
    });
    renderApp(api);

    const table = await screen.findByRole("table", { name: "수서 업무 목록" });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(within(rows[0]).getByText("승인할 신간")).toBeVisible();
    expect(within(rows[0]).getByText("검토자")).toBeVisible();
    expect(
      screen.getAllByRole("link", { name: "승인할 작업 보기" }),
    ).toHaveLength(1);
    expect(screen.queryByRole("link", { name: "이어 하기" })).not.toBeInTheDocument();
  });

  test("세 shell 밖의 화면 제목이나 화면 전환용 저장·다음 버튼을 만들지 않는다", async () => {
    const api = createFixtureApi({
      getCurrentUser: async () => operator,
      listWorkspaces: async () => ({ items: workspaces, next_cursor: null }),
    });
    renderApp(api);

    await screen.findByRole("heading", { level: 1, name: "내 수서 업무" });
    expect(screen.queryByRole("button", { name: /^저장$/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^다음$/ })).not.toBeInTheDocument();
    await waitFor(() => {
      expect(document.title).toBe("내 수서 업무 · 수서로");
    });
  });
});
