import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { BookCover } from "../src/simple/BookCover";

afterEach(() => { vi.useRealTimers(); });

it("retries temporary failures and offers a manual retry after bounded attempts", async () => {
  vi.useFakeTimers();
  render(<BookCover isbn="9791160517408" title="겁 없이 달리는 소녀" />);
  for (const delay of [1000, 3000, 8000]) {
    fireEvent.error(screen.getByRole("img"));
    expect(screen.getByRole("status")).toHaveTextContent("재시도 중");
    await act(() => vi.advanceTimersByTime(delay));
    expect(screen.getByRole("img")).toHaveAttribute("src", expect.stringContaining("?retry="));
  }
  fireEvent.error(screen.getByRole("img"));
  const retry = screen.getByRole("button", { name: "겁 없이 달리는 소녀 표지 다시 불러오기" });
  await act(() => vi.advanceTimersByTime(60000));
  expect(screen.queryByRole("img")).not.toBeInTheDocument();
  fireEvent.click(retry);
  expect(screen.getByRole("img")).toHaveAttribute("src", "/api/library/covers/9791160517408");
});

it("cancels a pending retry when the ISBN changes", async () => {
  vi.useFakeTimers();
  const view = render(<BookCover isbn="9791160517408" title="첫 책" />);
  fireEvent.error(screen.getByRole("img"));
  view.rerender(<BookCover isbn="9791193379813" title="다른 책" />);
  await act(() => vi.advanceTimersByTime(10000));
  expect(screen.getByRole("img", { name: "다른 책 표지" })).toHaveAttribute("src", "/api/library/covers/9791193379813");
});
