import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Locator, type Page } from "@playwright/test";
import { Buffer } from "node:buffer";
import path from "node:path";

import { login, metadata } from "./helpers";

const visual = (filename: string) =>
  path.resolve("..", "output", "playwright", filename);

const captureVisual = async (page: Page, filename: string) => {
  await expect(page.locator(".skip-link")).not.toBeFocused();
  await page.screenshot({
    path: visual(filename),
    fullPage: true,
    style: ".skip-link { display: none !important; }",
  });
};

const focusCleanPreview = async (target: Locator) => {
  await target.evaluate((element) => {
    element.setAttribute("tabindex", "-1");
    (element as HTMLElement).focus({ preventScroll: true });
  });
  await expect(target).toBeFocused();
};

const tabToAndActivate = async (page: Page, target: Locator, key = "Enter") => {
  let reached = false;
  for (let step = 0; step < 100; step += 1) {
    await page.keyboard.press("Tab");
    if (await target.evaluate((element) => element === document.activeElement)) {
      reached = true;
      break;
    }
  }
  expect(reached, "target must be reachable in the natural Tab order").toBe(true);
  await expect(target).toBeFocused();
  await page.keyboard.press(key);
};

const assertNoSeriousAxeViolations = async (page: Page, state: string) => {
  const result = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    .analyze();
  expect(
    result.violations.filter((item) =>
      item.impact === "critical" || item.impact === "serious"
    ),
    `${state}: critical/serious axe violations`,
  ).toEqual([]);
};

const assertNoOverflowAt200Percent = async (page: Page, state: string) => {
  const previousViewport = page.viewportSize();
  await page.setViewportSize({ width: 640, height: 720 });
  await page.evaluate(() => { document.documentElement.style.fontSize = "200%"; });
  const layout = await page.evaluate(() => ({
    clientWidth: document.documentElement.clientWidth,
    scrollWidth: document.documentElement.scrollWidth,
  }));
  expect(
    layout.scrollWidth,
    `${state}: ${JSON.stringify(layout)}`,
  ).toBeLessThanOrEqual(layout.clientWidth + 1);
  await page.evaluate(() => { document.documentElement.style.fontSize = ""; });
  if (previousViewport) await page.setViewportSize(previousViewport);
};

const assertAccessibleState = async (page: Page, state: string) => {
  await assertNoSeriousAxeViolations(page, state);
  await assertNoOverflowAt200Percent(page, state);
};

const quoteA = [
  "ISBN,제목,저자,수량,공급가,표시가격,품절",
  "9788937464010,도서관의 책,김사서,1,12000,15000,false",
  "9788936434267,차분한 수서,이담당,1,15000,18000,false",
].join("\n");

const quoteB = [
  "ISBN,제목,저자,수량,공급가,표시가격,품절",
  "9788937464010,도서관의 책,김사서,1,40000,42000,false",
  "9788936434267,차분한 수서,이담당,1,40000,42000,false",
].join("\n");

test("keyboard-activated reviewer two-action and operator nine-action core flow", async ({
  browser,
  page,
  request,
}) => {
  const seed = await metadata(request);
  const reviewerActions: string[] = [];

  await login(page, seed, seed.reviewer_username);
  reviewerActions.push("승인할 작업 보기");
  await tabToAndActivate(page, page.getByRole("link", { name: "핵심 흐름 수서" }));
  await expect(page.getByText("후보 2권")).toBeVisible();
  await assertAccessibleState(page, "approval");
  await focusCleanPreview(page.getByRole("heading", { name: "목록의 핵심만 확인해 주세요" }));
  await captureVisual(page, "01-reviewer-approval.png");
  reviewerActions.push("이 목록 승인");
  await tabToAndActivate(page, page.getByRole("button", { name: "이 목록 승인" }));
  await expect(page.getByRole("heading", { name: "업체 견적 비교" })).toBeVisible();
  expect(reviewerActions).toEqual(["승인할 작업 보기", "이 목록 승인"]);
  expect(reviewerActions).toHaveLength(2);

  const operatorContext = await browser.newContext({ acceptDownloads: true });
  const operatorPage = await operatorContext.newPage();
  const operatorActions: string[] = [];
  await login(operatorPage, seed, seed.operator_username);

  operatorActions.push("작업 열기");
  await tabToAndActivate(operatorPage, operatorPage.getByRole("link", { name: "핵심 흐름 수서" }));
  await expect(operatorPage.getByRole("heading", { name: "업체 견적 비교" })).toBeVisible();

  await operatorPage.getByLabel("업체 이름").fill("푸른서점");
  await operatorPage.getByLabel("견적 파일", { exact: true }).setInputFiles({
    name: "quote-a.csv",
    mimeType: "text/csv",
    buffer: Buffer.from(quoteA),
  });
  operatorActions.push("견적 A 추가");
  await tabToAndActivate(operatorPage, operatorPage.getByRole("button", { name: "견적서 비교하기" }));
  await expect(operatorPage.getByRole("heading", { name: "푸른서점" })).toBeVisible();

  await operatorPage.getByLabel("업체 이름").fill("초과서점");
  await operatorPage.getByLabel("견적 파일", { exact: true }).setInputFiles({
    name: "quote-b.csv",
    mimeType: "text/csv",
    buffer: Buffer.from(quoteB),
  });
  operatorActions.push("견적 B 추가");
  await tabToAndActivate(operatorPage, operatorPage.getByRole("button", { name: "견적서 비교하기" }));
  await expect(operatorPage.getByText(/30,000원 초과/)).toBeVisible();
  await expect(operatorPage.getByText(/목록을 자동으로 줄이지 않습니다/)).toBeVisible();
  await assertAccessibleState(operatorPage, "quote comparison");
  await focusCleanPreview(operatorPage.getByRole("heading", { name: "업체 견적 비교" }));
  await captureVisual(operatorPage, "02-quote-comparison.png");

  operatorActions.push("이 견적 사용");
  await tabToAndActivate(operatorPage, operatorPage.getByRole("button", { name: "이 견적 사용" }));
  await expect(operatorPage.getByRole("heading", { name: "발주파일 준비" })).toBeVisible();
  await expect(operatorPage.getByText("승인 예산")).toBeVisible();
  await expect(operatorPage.getByText("선택 견적")).toBeVisible();
  await expect(operatorPage.getByText("차이")).toBeVisible();
  await assertAccessibleState(operatorPage, "order ready");
  await focusCleanPreview(operatorPage.getByRole("heading", { name: "발주파일 준비" }));
  await captureVisual(operatorPage, "03-order-ready.png");

  operatorActions.push("발주파일 받기");
  const orderDownload = operatorPage.waitForEvent("download");
  await tabToAndActivate(operatorPage, operatorPage.getByRole("button", { name: "발주파일 받기" }));
  const downloaded = await orderDownload;
  expect(downloaded.suggestedFilename()).toMatch(/\.xlsx$/);
  await expect(operatorPage.getByText(
    "발주파일이 완성되었습니다. 실제 주문 전 업체에 직접 전달해 주세요.",
  )).toBeVisible();

  operatorActions.push("업체에 전달했어요");
  await tabToAndActivate(operatorPage, operatorPage.getByRole("button", { name: "업체에 전달했어요" }), "Space");
  await expect(operatorPage.getByRole("heading", { name: "도착한 책 확인" })).toBeVisible();

  await operatorPage.getByLabel("납품명세서 파일", { exact: true }).setInputFiles({
    name: "delivery.xlsx",
    mimeType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    buffer: Buffer.from(seed.delivery_xlsx_base64, "base64"),
  });
  operatorActions.push("납품명세서 비교");
  await tabToAndActivate(operatorPage, operatorPage.getByRole("button", { name: "납품명세서 비교하기" }));
  const receivingSummary = operatorPage.getByLabel("수령 진행 요약");
  await expect(receivingSummary.getByText("납품명세서 반영").locator(".."))
    .toContainText("2 / 2권");
  await expect(receivingSummary.getByText("바코드 확인").locator(".."))
    .toContainText("0 / 2권");
  await assertAccessibleState(operatorPage, "receiving differences");

  operatorActions.push("바코드 검수 시작");
  await tabToAndActivate(operatorPage, operatorPage.getByRole("button", { name: "바코드 검수 시작" }));
  const scanner = operatorPage.getByLabel("ISBN 바코드");
  await expect(scanner).toBeFocused();
  await expect(receivingSummary.getByText("처리 대기 차이").locator(".."))
    .toContainText("2건");
  await expect(operatorPage.getByText("도서관의 책 · ISBN 9788937464010")).toBeVisible();
  await assertAccessibleState(operatorPage, "scanner");
  await expect(scanner).toBeFocused();
  await captureVisual(operatorPage, "04-scanner-focus.png");
  await operatorPage.evaluate(() => {
    (window as typeof window & { scannerClicks: number }).scannerClicks = 0;
    document.addEventListener("click", () => {
      (window as typeof window & { scannerClicks: number }).scannerClicks += 1;
    });
  });
  for (const isbn of seed.isbns) {
    await operatorPage.keyboard.type(isbn);
    await operatorPage.keyboard.press("Enter");
    await expect(scanner).toBeFocused();
  }
  expect(await operatorPage.evaluate(
    () => (window as typeof window & { scannerClicks: number }).scannerClicks,
  )).toBe(0);
  await expect(operatorPage.getByText("모든 주문 수량과 차이 처리 방침을 확인했습니다.")).toBeVisible();

  operatorActions.push("검수 완료");
  await tabToAndActivate(operatorPage, operatorPage.getByRole("button", { name: "검수 완료하고 작업 끝내기" }));
  await expect(operatorPage.getByText("도착한 책의 납품 검수까지 마쳐 이 작업을 완료했습니다.")).toBeVisible();
  await expect(operatorPage.getByText("현재 과정")).toHaveCount(0);
  await assertAccessibleState(operatorPage, "completed workroom");
  await focusCleanPreview(operatorPage.getByRole("heading", { name: "수서 작업실" }));
  await captureVisual(operatorPage, "05-completed-workroom.png");
  expect(operatorActions).toHaveLength(9);
  expect(operatorActions.length).toBeLessThanOrEqual(10);
  await operatorContext.close();
});
