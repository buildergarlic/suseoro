import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { HoldingsLibraryView, RecommendationsLibraryView } from "../src/simple/RegisteredLibraryViews";
import { emptyBook, type Book, type BookFields, type HoldingsPage } from "../src/simple/types";

function savedBook(id: string, title: string, source: string): Book {
  return { ...emptyBook(), id, title, source, author: "지은이", publisher: "출판사", held: false, duplicate: false };
}

it("searches the registered school holdings and pages through the server results", async () => {
  const user = userEvent.setup();
  const holdings = vi.fn(async ({ query, page }: { query?: string; page?: number; page_size?: number } = {}): Promise<HoldingsPage> => {
    const item: BookFields = { ...emptyBook(), title: page === 2 ? "두 번째 소장 도서" : "첫 번째 소장 도서", author: "저자", isbn: "9788936434267" };
    return { items: [item], total: query ? 1 : 51, page: page ?? 1, page_size: 50 };
  });
  render(<HoldingsLibraryView api={{ holdings }} onImport={vi.fn()} refreshKey={0} />);

  expect(within(await screen.findByRole("table", { name: "학교 소장목록" })).getByText("첫 번째 소장 도서")).toBeVisible();
  await user.click(screen.getByRole("button", { name: "다음 페이지" }));
  expect(await screen.findByText("두 번째 소장 도서")).toBeVisible();
  await user.type(screen.getByRole("searchbox", { name: "소장목록 검색" }), "어린왕자");
  await user.click(screen.getByRole("button", { name: "검색" }));
  expect(holdings).toHaveBeenLastCalledWith({ query: "어린왕자", page: 1, page_size: 50 });
});

it("groups saved recommendations by source and filters without claiming upload history", async () => {
  const user = userEvent.setup();
  render(<RecommendationsLibraryView books={[
    savedBook("a", "첫 추천 도서", "교육청 추천.xlsx"),
    savedBook("b", "다른 추천 도서", "사서 추천.xlsx"),
    savedBook("c", "직접 고른 도서", "직접 입력"),
  ]} listName="2026년 구입목록" />);

  expect(screen.getByText(/원본 업로드 이력이 아닙니다/)).toBeVisible();
  const table = screen.getByRole("table", { name: "추천 출처별 저장 도서" });
  expect(within(table).getByText("교육청 추천.xlsx")).toBeVisible();
  expect(within(table).getByText("사서 추천.xlsx")).toBeVisible();
  await user.selectOptions(screen.getByRole("combobox", { name: "추천 출처" }), "교육청 추천.xlsx");
  expect(screen.getByText("첫 추천 도서")).toBeVisible();
  expect(screen.queryByText("다른 추천 도서")).not.toBeInTheDocument();
  expect(screen.queryByText("직접 고른 도서")).not.toBeInTheDocument();
  await user.type(screen.getByRole("searchbox", { name: "추천도서 검색" }), "없는 책");
  expect(screen.getByText("검색 조건에 맞는 도서가 없습니다.")).toBeVisible();
});

it("groups rows from one imported file together despite row-specific source text", () => {
  const imported = (id: string, title: string, row: number): Book => ({
    ...savedBook(id, title, `교육청 · 가을 추천.xlsx · 추천도서 · ${row}행`),
    provenance: { filename: "가을 추천.xlsx", sheet: "추천도서", row },
  });
  render(<RecommendationsLibraryView books={[imported("one", "첫 책", 2), imported("two", "둘째 책", 3)]} listName="가을 구입" />);

  expect(screen.getByRole("combobox", { name: "추천 출처" })).toHaveTextContent("교육청 · 가을 추천.xlsx");
  expect(screen.getAllByRole("rowgroup")).toHaveLength(2);
  expect(screen.getByText("첫 책")).toBeInTheDocument();
  expect(screen.getByText("둘째 책")).toBeInTheDocument();
});

it("uses an edited recommendation source instead of stale file provenance", () => {
  render(<RecommendationsLibraryView books={[{
    ...savedBook("one", "수정된 책", "학교장 추천"),
    provenance: { filename: "가을 추천.xlsx", row: 2 },
  }]} listName="가을 구입" />);
  expect(screen.getByRole("combobox", { name: "추천 출처" })).toHaveTextContent("학교장 추천");
  expect(screen.queryByText("가을 추천.xlsx")).not.toBeInTheDocument();
});

it("shows a merged book under every saved recommendation source without double-counting books", async () => {
  const user = userEvent.setup();
  render(<RecommendationsLibraryView books={[{
    ...savedBook("merged", "함께 추천한 책", "교육청 · 가을 추천.xlsx · 추천도서 · 2행"),
    recommendation_count: 2,
    sources: ["교육청 · 가을 추천.xlsx · 추천도서 · 2행", "학교 · 겨울 추천.xlsx · 추천도서 · 4행"],
    contributions: [
      { source: "교육청 · 가을 추천.xlsx · 추천도서 · 2행", filename: "가을 추천.xlsx" },
      { source: "학교 · 겨울 추천.xlsx · 추천도서 · 4행", filename: "겨울 추천.xlsx" },
    ],
  }]} listName="2026년 구입목록" />);

  const sourceFilter = screen.getByRole("combobox", { name: "추천 출처" });
  expect(sourceFilter).toHaveTextContent("교육청 · 가을 추천.xlsx");
  expect(sourceFilter).toHaveTextContent("학교 · 겨울 추천.xlsx");
  expect(screen.getAllByText("함께 추천한 책")).toHaveLength(2);
  expect(screen.getByText("1권 / 전체 1권")).toBeVisible();
  await user.selectOptions(sourceFilter, "학교 · 겨울 추천.xlsx");
  expect(screen.getAllByText("함께 추천한 책")).toHaveLength(1);
  await user.clear(screen.getByRole("searchbox", { name: "추천도서 검색" }));
  await user.type(screen.getByRole("searchbox", { name: "추천도서 검색" }), "가을 추천.xlsx");
  expect(screen.getByText("검색 조건에 맞는 도서가 없습니다.")).toBeVisible();
  await user.selectOptions(sourceFilter, "");
  await user.clear(screen.getByRole("searchbox", { name: "추천도서 검색" }));
  expect(screen.getAllByText("함께 추천한 책")).toHaveLength(2);
  expect(screen.getByText("1권 / 전체 1권")).toBeVisible();
});
