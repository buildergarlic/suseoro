import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { createLibraryApi } from "../src/simple/api";
import { SettingsDialog } from "../src/simple/SettingsDialog";
import type { ErrorReportInfo } from "../src/simple/types";

function fixture() {
  const state = { report: { enabled: true, configured: true, recipient: "buildergarlic@gmail.com", queued_count: 0, last_sent_at: null, message: "업데이트 오류를 자동 보고합니다." } as ErrorReportInfo, failPreference: false, failTest: false };
  const api = Object.assign(createLibraryApi(), {
    errorReports: vi.fn(async () => ({ ...state.report })),
    errorReportPreferences: vi.fn(async (enabled: boolean) => {
      if (state.failPreference) throw new Error("보고 설정을 저장하지 못했습니다.");
      state.report = { ...state.report, enabled };
      return { ...state.report };
    }),
    testErrorReport: vi.fn(async () => {
      if (state.failTest) throw new Error("시험 보고를 저장하지 못했습니다.");
      state.report = { ...state.report, queued_count: 1, message: "연결되면 자동으로 다시 전송합니다." };
      return { ...state.report };
    }),
  });
  render(<SettingsDialog settings={{ school_name: "우리 학교", nl_api_key_configured: false }} version="2.3.0" update={null} api={api} onClose={vi.fn()} onSettings={vi.fn()} onRestored={vi.fn(async () => {})} onExited={vi.fn()} onUpdate={vi.fn()} onUpdateStarted={vi.fn()} />);
  return { api, state };
}

it("loads the saved reporting preference and discloses the destination and limited data", async () => {
  const user = userEvent.setup(), { api } = fixture();
  const checkbox = screen.getByRole("checkbox", { name: "업데이트 오류를 개발자에게 자동 보고" });
  await waitFor(() => expect(checkbox).toBeChecked());
  const section = screen.getByRole("region", { name: "업데이트 오류 자동 보고" });
  expect(within(section).getByText(/Netlify 중계 서버를 통해 buildergarlic@gmail.com/)).toBeInTheDocument();
  expect(within(section).getByText(/도서 목록·학교 이름·PC 사용자 이름·파일 경로·인증키는 포함하지 않습니다/)).toBeInTheDocument();
  await user.click(checkbox);
  await waitFor(() => expect(checkbox).not.toBeChecked());
  expect(api.errorReportPreferences).toHaveBeenCalledWith(false);
  expect(screen.getByRole("button", { name: "시험 보고 보내기" })).toBeDisabled();
});

it("keeps the saved preference when persisting a toggle fails", async () => {
  const user = userEvent.setup(), { state } = fixture();
  const checkbox = screen.getByRole("checkbox", { name: "업데이트 오류를 개발자에게 자동 보고" });
  await waitFor(() => expect(checkbox).toBeChecked());
  state.failPreference = true;
  await user.click(checkbox);
  expect(await screen.findByRole("alert")).toHaveTextContent("보고 설정을 저장하지 못했습니다.");
  expect(checkbox).toBeChecked();
});

it("shows offline test reports in the queue and refreshes server acceptance without polling", async () => {
  const user = userEvent.setup(), { api, state } = fixture();
  const test = screen.getByRole("button", { name: "시험 보고 보내기" });
  await waitFor(() => expect(test).toBeEnabled());
  await user.click(test);
  expect(await screen.findByText("전송 대기 1건")).toBeInTheDocument();
  expect(api.testErrorReport).toHaveBeenCalledTimes(1);
  state.report = { ...state.report, queued_count: 0, last_sent_at: "2026-10-12T13:00:00Z", message: "보고 서버가 접수했습니다." };
  await user.click(screen.getByRole("button", { name: "보고 상태 새로고침" }));
  expect(await screen.findByText(/전송 대기 0건 · 최근 서버 접수/)).toBeInTheDocument();
  expect(api.errorReports).toHaveBeenCalledTimes(2);
});

it("shows a failed test request without claiming it was queued or delivered", async () => {
  const user = userEvent.setup(), { state } = fixture();
  const test = screen.getByRole("button", { name: "시험 보고 보내기" });
  await waitFor(() => expect(test).toBeEnabled());
  state.failTest = true;
  await user.click(test);
  expect(await screen.findByRole("alert")).toHaveTextContent("시험 보고를 저장하지 못했습니다.");
  expect(screen.getByText("전송 대기 0건")).toBeInTheDocument();
  expect(screen.queryByText(/전송 대기열에 추가했습니다/)).not.toBeInTheDocument();
});
