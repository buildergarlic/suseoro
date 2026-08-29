import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

import { login, metadata } from "./helpers";

test("approval workroom has no critical or serious axe violations", async ({ page, request }) => {
  const seed = await metadata(request);
  await login(page, seed, seed.reviewer_username);
  await page.goto(`/workspaces/${seed.accessibility_workspace_id}`);
  await expect(page.getByRole("button", { name: "수정 요청 보내기" })).toBeVisible();

  const result = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    .analyze();
  expect(result.violations.filter((item) =>
    item.impact === "critical" || item.impact === "serious"
  )).toEqual([]);
});

test("keyboard dialog trap returns focus and 200 percent layout does not overflow", async ({
  page,
  request,
}) => {
  const seed = await metadata(request);
  await login(page, seed, seed.reviewer_username);
  await page.goto(`/workspaces/${seed.accessibility_workspace_id}`);
  const opener = page.getByRole("button", { name: "수정 요청 보내기" });
  await opener.focus();
  await page.keyboard.press("Enter");
  const dialog = page.getByRole("dialog", { name: "수정 요청 보내기" });
  await expect(dialog).toBeVisible();
  await expect(page.getByLabel("수정 요청 사유")).toBeFocused();
  await page.keyboard.press("Shift+Tab");
  await expect(dialog.getByRole("button", { name: "수정 요청 보내기" })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(opener).toBeFocused();

  await page.setViewportSize({ width: 640, height: 720 });
  await page.evaluate(() => { document.documentElement.style.fontSize = "200%"; });
  await expect(page.getByRole("heading", { name: "목록의 핵심만 확인해 주세요" })).toBeVisible();
  const layout = await page.evaluate(() => {
    const root = document.documentElement;
    const clientWidth = root.clientWidth;
    const overflowing = Array.from(document.querySelectorAll<HTMLElement>("body *"))
      .filter((element) => {
        const rect = element.getBoundingClientRect();
        return rect.left < -1 || rect.right > clientWidth + 1;
      })
      .slice(0, 8)
      .map((element) => {
        const rect = element.getBoundingClientRect();
        return {
          className: element.className,
          left: rect.left,
          right: rect.right,
          tagName: element.tagName,
          width: rect.width,
        };
      });
    return {
      clientWidth,
      rootMinWidth: getComputedStyle(root).minWidth,
      scrollWidth: root.scrollWidth,
      overflowing,
    };
  });
  expect(layout.scrollWidth, JSON.stringify(layout, null, 2)).toBeLessThanOrEqual(
    layout.clientWidth + 1,
  );
});
