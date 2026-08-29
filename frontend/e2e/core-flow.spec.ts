import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Locator, type Page } from "@playwright/test";
import { Buffer } from "node:buffer";
import path from "node:path";

import {
  assertNoOverflowAt200Percent,
  instrumentMajorActions,
  login,
  majorActionEvidence,
  metadata,
} from "./helpers";

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
  await target.evaluate((element) => {
    (element as HTMLElement).blur();
    element.removeAttribute("tabindex");
  });
  await expect(target).not.toBeFocused();
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

const assertAccessibleState = async (page: Page, state: string) => {
  await assertNoSeriousAxeViolations(page, state);
  await assertNoOverflowAt200Percent(page, state);
};

const quoteA = [
  "ISBN,제목,저자,수량,공급가,표시가격,품절",
  "9788937464010,도서관의 책,김사서,1,12000,12000,false",
  "9788936434267,차분한 수서,이담당,1,15000,15000,false",
].join("\n");

const quoteB = [
  "ISBN,제목,저자,수량,공급가,표시가격,품절",
  "9788937464010,도서관의 책,김사서,1,40000,42000,false",
  "9788936434267,차분한 수서,이담당,1,40000,42000,false",
].join("\n");

const mappingQuote = [
  "A,B,C,D",
  "9788937464010,열 연결할 책,1,12000",
].join("\n");

test("keyboard-activated reviewer two-action and operator ten-action core flow", async ({
  browser,
  page,
  request,
}) => {
  const seed = await metadata(request);

  await instrumentMajorActions(page);
  await login(page, seed, seed.reviewer_username);
  await tabToAndActivate(page, page.getByRole("link", { name: "핵심 흐름 수서" }));
  await expect(page.getByText("후보 2권")).toBeVisible();
  await assertAccessibleState(page, "approval");
  await focusCleanPreview(page.getByRole("heading", { name: "목록의 핵심만 확인해 주세요" }));
  await captureVisual(page, "01-reviewer-approval.png");
  await tabToAndActivate(page, page.getByRole("button", { name: "이 목록 승인" }));
  await expect(page.getByRole("heading", { name: "업체 견적 비교" })).toBeVisible();
  expect((await majorActionEvidence(page)).map((item) => item.action)).toEqual([
    "OPEN_WORKSPACE",
    "APPROVE_LIST",
  ]);
  expect(await majorActionEvidence(page)).toHaveLength(2);
  await expect(page.getByLabel("업체 이름")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "견적서 비교하기" })).toHaveCount(0);

  const operatorContext = await browser.newContext({ acceptDownloads: true });
  const operatorPage = await operatorContext.newPage();
  await instrumentMajorActions(operatorPage);
  await login(operatorPage, seed, seed.operator_username);

  await tabToAndActivate(operatorPage, operatorPage.getByRole("link", { name: "핵심 흐름 수서" }));
  await expect(operatorPage.getByRole("heading", { name: "업체 견적 비교" })).toBeVisible();

  await operatorPage.getByLabel("업체 이름").fill("푸른서점");
  await operatorPage.getByLabel("견적 파일", { exact: true }).setInputFiles({
    name: "quote-a.csv",
    mimeType: "text/csv",
    buffer: Buffer.from(quoteA),
  });
  await tabToAndActivate(operatorPage, operatorPage.getByRole("button", { name: "견적서 비교하기" }));
  await expect(operatorPage.getByRole("heading", { name: "푸른서점" })).toBeVisible();

  await operatorPage.getByLabel("업체 이름").fill("초과서점");
  await operatorPage.getByLabel("견적 파일", { exact: true }).setInputFiles({
    name: "quote-b.csv",
    mimeType: "text/csv",
    buffer: Buffer.from(quoteB),
  });
  await tabToAndActivate(operatorPage, operatorPage.getByRole("button", { name: "견적서 비교하기" }));
  await expect(operatorPage.getByText(/30,000원 초과/)).toBeVisible();
  await expect(operatorPage.getByText(/목록을 자동으로 줄이지 않습니다/)).toBeVisible();
  await assertAccessibleState(operatorPage, "quote comparison");
  await focusCleanPreview(operatorPage.getByRole("heading", { name: "업체 견적 비교" }));
  await captureVisual(operatorPage, "02-quote-comparison.png");

  const blockedQuote = operatorPage.locator("article").filter({
    has: operatorPage.getByRole("heading", { name: "초과서점" }),
  });
  await expect(blockedQuote.getByRole("button", { name: "이 견적 사용" })).toBeDisabled();
  await expect(blockedQuote).toContainText("다시 승인이 필요합니다");
  const usableQuote = operatorPage.locator("article").filter({
    has: operatorPage.getByRole("heading", { name: "푸른서점" }),
  });
  await expect(usableQuote.getByRole("button", { name: "이 견적 사용" })).toBeEnabled();
  await tabToAndActivate(operatorPage, usableQuote.getByRole("button", { name: "이 견적 사용" }));
  await expect(operatorPage.getByRole("heading", { name: "발주파일 준비" })).toBeVisible();
  await expect(operatorPage.getByText("승인 예산")).toBeVisible();
  await expect(operatorPage.getByText("선택 견적")).toBeVisible();
  await expect(operatorPage.getByText("차이")).toBeVisible();
  await assertAccessibleState(operatorPage, "order ready");
  await focusCleanPreview(operatorPage.getByRole("heading", { name: "발주파일 준비" }));
  await captureVisual(operatorPage, "03-order-ready.png");

  const orderDownload = operatorPage.waitForEvent("download");
  await tabToAndActivate(operatorPage, operatorPage.getByRole("button", { name: "발주파일 받기" }));
  const downloaded = await orderDownload;
  expect(downloaded.suggestedFilename()).toMatch(/\.xlsx$/);
  await expect(operatorPage.getByText(
    "발주파일이 완성되었습니다. 실제 주문 전 업체에 직접 전달해 주세요.",
  )).toBeVisible();

  await tabToAndActivate(operatorPage, operatorPage.getByRole("button", { name: "업체에 전달했어요" }), "Space");
  await expect(operatorPage.getByRole("heading", { name: "도착한 책 확인" })).toBeVisible();

  await operatorPage.getByLabel("납품명세서 파일", { exact: true }).setInputFiles({
    name: "delivery-part-a.xlsx",
    mimeType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    buffer: Buffer.from(seed.delivery_partial_a_xlsx_base64, "base64"),
  });
  await tabToAndActivate(operatorPage, operatorPage.getByRole("button", { name: "납품명세서 비교하기" }));
  const receivingSummary = operatorPage.getByLabel("수령 진행 요약");
  await expect(receivingSummary.getByText("납품명세서 반영").locator(".."))
    .toContainText("1 / 2권");
  await operatorPage.reload();
  await expect(operatorPage.getByRole("heading", { name: "도착한 책 확인" })).toBeVisible();
  await expect(receivingSummary.getByText("납품명세서 반영").locator(".."))
    .toContainText("1 / 2권");

  await operatorPage.getByLabel("납품명세서 파일", { exact: true }).setInputFiles({
    name: "delivery-part-b.xlsx",
    mimeType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    buffer: Buffer.from(seed.delivery_partial_b_xlsx_base64, "base64"),
  });
  await tabToAndActivate(operatorPage, operatorPage.getByRole("button", { name: "납품명세서 비교하기" }));
  await expect(receivingSummary.getByText("납품명세서 반영").locator(".."))
    .toContainText("2 / 2권");
  await expect(receivingSummary.getByText("바코드 확인").locator(".."))
    .toContainText("0 / 2권");
  const completeReceiving = operatorPage.getByRole("button", { name: "검수 완료하고 작업 끝내기" });
  await expect(completeReceiving).toBeDisabled();
  await expect(operatorPage.getByText(
    "바코드로 확인하지 않았거나 차이 처리 방침이 없는 책이 2권 있습니다.",
    { exact: true },
  )).toBeVisible();
  await operatorPage.reload();
  await expect(receivingSummary.getByText("납품명세서 반영").locator(".."))
    .toContainText("2 / 2권");
  await expect(completeReceiving).toBeDisabled();
  await expect(operatorPage.getByText(
    "바코드로 확인하지 않았거나 차이 처리 방침이 없는 책이 2권 있습니다.",
    { exact: true },
  )).toBeVisible();
  await assertAccessibleState(operatorPage, "receiving differences");

  await page.reload();
  await expect(page.getByRole("heading", { name: "도착한 책 확인" })).toBeVisible();
  await expect(page.getByText("승인 예산", { exact: true })).toBeVisible();
  await expect(page.getByText("선택 견적", { exact: true })).toBeVisible();
  await expect(page.getByText("차이", { exact: true })).toBeVisible();
  await expect(page.getByLabel("납품명세서 파일", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "바코드 검수 시작" })).toHaveCount(0);
  await expect(page.getByLabel("처리 방침")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "방침 기록" })).toHaveCount(0);

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
  const scanRequests: Array<{ isbn: string; key: string }> = [];
  let abortFirstResponse = true;
  await operatorPage.route("**/api/v2/scans/sessions/*/events", async (route) => {
    const payload = route.request().postDataJSON() as { isbn: string };
    scanRequests.push({
      isbn: payload.isbn,
      key: route.request().headers()["idempotency-key"] ?? "",
    });
    if (abortFirstResponse) {
      abortFirstResponse = false;
      await route.fetch();
      await route.abort("failed");
      return;
    }
    await route.continue();
  });
  await scanner.fill(seed.isbns[0]);
  await scanner.press("Enter");
  await expect(operatorPage.getByText("다시 확인", { exact: true })).toBeVisible();
  expect(scanRequests).toHaveLength(1);
  await scanner.fill(seed.isbns[1]);
  await scanner.press("Enter");
  expect(scanRequests).toHaveLength(1);
  await expect(scanner).toHaveValue(seed.isbns[0]);
  await scanner.press("Enter");
  await expect.poll(() => scanRequests.length).toBe(3);
  expect(scanRequests.map((item) => item.isbn)).toEqual([
    seed.isbns[0],
    seed.isbns[0],
    seed.isbns[1],
  ]);
  expect(scanRequests[0].key).toBe(scanRequests[1].key);
  expect(scanRequests[2].key).not.toBe(scanRequests[0].key);
  await operatorPage.unroute("**/api/v2/scans/sessions/*/events");
  await expect(receivingSummary.getByText("바코드 확인").locator(".."))
    .toContainText("2 / 2권");
  await expect(scanner).toBeFocused();
  expect(await operatorPage.evaluate(
    () => (window as typeof window & { scannerClicks: number }).scannerClicks,
  )).toBe(0);
  await expect(operatorPage.getByText("모든 주문 수량과 차이 처리 방침을 확인했습니다.")).toBeVisible();

  await tabToAndActivate(operatorPage, operatorPage.getByRole("button", { name: "검수 완료하고 작업 끝내기" }));
  await expect(operatorPage.getByText("도착한 책의 납품 검수까지 마쳐 이 작업을 완료했습니다.")).toBeVisible();
  await expect(operatorPage.getByText("현재 과정")).toHaveCount(0);
  await assertAccessibleState(operatorPage, "completed workroom");
  await focusCleanPreview(operatorPage.getByRole("heading", { name: "수서 작업실" }));
  await captureVisual(operatorPage, "05-completed-workroom.png");
  const operatorEvidence = await majorActionEvidence(operatorPage);
  expect(operatorEvidence.map((item) => item.action)).toEqual([
    "OPEN_WORKSPACE",
    "COMPARE_QUOTE",
    "COMPARE_QUOTE",
    "SELECT_QUOTE",
    "DOWNLOAD_ORDER",
    "MARK_ORDER_SENT",
    "COMPARE_DELIVERY",
    "COMPARE_DELIVERY",
    "START_SCAN",
    "COMPLETE_RECEIVING",
  ]);
  expect(operatorEvidence).toHaveLength(10);
  expect(operatorEvidence.length).toBeLessThanOrEqual(10);
  await operatorContext.close();
});

test("durable mapping recovery is operator-only and composes canonical rows", async ({
  browser,
  page,
  request,
}) => {
  const seed = await metadata(request);
  await login(page, seed, seed.operator_username);
  await page.goto(`/workspaces/${seed.mapping_workspace_id}`);
  await expect(page.getByRole("heading", { name: "업체 견적 비교" })).toBeVisible();

  await page.getByLabel("업체 이름").fill("열연결서점");
  await page.getByLabel("견적 파일", { exact: true }).setInputFiles({
    name: "unknown-columns.csv",
    mimeType: "text/csv",
    buffer: Buffer.from(mappingQuote),
  });
  await page.getByRole("button", { name: "견적서 비교하기" }).click();
  const mappingButton = page.getByRole("button", {
    name: "unknown-columns.csv 열 연결하기",
  });
  await expect(mappingButton).toBeVisible();
  await page.reload();
  await expect(mappingButton).toBeVisible();

  const reviewerContext = await browser.newContext();
  const reviewerPage = await reviewerContext.newPage();
  await login(reviewerPage, seed, seed.reviewer_username);
  await reviewerPage.goto(`/workspaces/${seed.mapping_workspace_id}`);
  await expect(reviewerPage.getByText("unknown-columns.csv", { exact: true })).toBeVisible();
  await expect(reviewerPage.getByRole("button", { name: /열 연결하기/ })).toHaveCount(0);
  await expect(reviewerPage.getByRole("button", { name: "서버 처리 결과 반영하기" })).toHaveCount(0);
  await expect(reviewerPage.getByLabel("견적 파일", { exact: true })).toHaveCount(0);

  await mappingButton.click();
  const mappingDialog = page.getByRole("dialog", { name: "열 연결 확인" });
  await expect(mappingDialog).toBeVisible();
  await mappingDialog.getByLabel("제목 열").selectOption("B");
  await mappingDialog.getByLabel("ISBN 열").selectOption("A");
  await mappingDialog.getByLabel("수량 열").selectOption("C");
  await mappingDialog.getByLabel("단가 열").selectOption("D");
  await mappingDialog.getByRole("button", { name: "열 연결 적용" }).click();
  const composeButton = page.getByRole("button", { name: "서버 처리 결과 반영하기" });
  await expect(composeButton).toBeVisible();

  await reviewerPage.reload();
  await expect(reviewerPage.getByText("unknown-columns.csv", { exact: true })).toBeVisible();
  await expect(reviewerPage.getByRole("button", { name: "서버 처리 결과 반영하기" })).toHaveCount(0);

  await composeButton.click();
  const mappedQuote = page.locator("article").filter({
    has: page.getByRole("heading", { name: "열연결서점" }),
  });
  await expect(mappedQuote.getByText("12,000원", { exact: true })).toBeVisible();
  await reviewerContext.close();
});
