import { expect, type APIRequestContext, type Page } from "@playwright/test";

export interface E2eMetadata {
  school_id: string;
  workspace_id: string;
  accessibility_workspace_id: string;
  mapping_workspace_id: string;
  password: string;
  operator_username: string;
  reviewer_username: string;
  isbns: string[];
  delivery_partial_a_xlsx_base64: string;
  delivery_partial_b_xlsx_base64: string;
}

export interface MajorActionEvidence {
  action: string;
  label: string;
}

export async function instrumentMajorActions(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const storageKey = "suseoro:e2e:major-actions";
    if (sessionStorage.getItem(storageKey) === null) {
      sessionStorage.setItem(storageKey, "[]");
    }
    document.addEventListener("click", (event) => {
      const origin = event.target;
      if (!(origin instanceof Element)) return;
      const target = origin.closest<HTMLElement>("[data-major-action]");
      if (!target || target.matches(":disabled") || target.getAttribute("aria-disabled") === "true") return;
      const current = JSON.parse(sessionStorage.getItem(storageKey) ?? "[]") as Array<{
        action: string;
        label: string;
      }>;
      current.push({
        action: target.dataset.majorAction ?? "",
        label: target.innerText.trim(),
      });
      sessionStorage.setItem(storageKey, JSON.stringify(current));
    }, true);
  });
}

export async function majorActionEvidence(page: Page): Promise<MajorActionEvidence[]> {
  return await page.evaluate(() => JSON.parse(
    sessionStorage.getItem("suseoro:e2e:major-actions") ?? "[]",
  ) as MajorActionEvidence[]);
}

export async function assertNoOverflowAt200Percent(
  page: Page,
  state: string,
): Promise<void> {
  const previousViewport = page.viewportSize();
  try {
    await page.setViewportSize({ width: 640, height: 720 });
    await page.evaluate(() => { document.documentElement.style.fontSize = "200%"; });
    const layout = await page.evaluate(() => {
      const root = document.documentElement;
      const clientWidth = root.clientWidth;
      const offenders = Array.from(document.querySelectorAll<HTMLElement>("body *"))
        .filter((element) => {
          const style = getComputedStyle(element);
          const rect = element.getBoundingClientRect();
          if (style.display === "none" || style.visibility === "hidden" || rect.width === 0) return false;
          if (rect.left >= -1 && rect.right <= clientWidth + 1) return false;
          let ancestor = element.parentElement;
          while (ancestor && ancestor !== document.body) {
            const ancestorStyle = getComputedStyle(ancestor);
            const ancestorRect = ancestor.getBoundingClientRect();
            if (
              /^(auto|scroll)$/.test(ancestorStyle.overflowX)
              && ancestorRect.left >= -1
              && ancestorRect.right <= clientWidth + 1
            ) return false;
            ancestor = ancestor.parentElement;
          }
          return true;
        })
        .slice(0, 12)
        .map((element) => {
          const rect = element.getBoundingClientRect();
          return {
            className: element.className,
            left: rect.left,
            right: rect.right,
            tagName: element.tagName,
            text: element.innerText.slice(0, 80),
            width: rect.width,
          };
        });
      return {
        clientWidth,
        offenders,
        scrollWidth: root.scrollWidth,
      };
    });
    expect(layout.scrollWidth, `${state}: ${JSON.stringify(layout)}`)
      .toBeLessThanOrEqual(layout.clientWidth + 1);
    expect(layout.offenders, `${state}: visible overflow offenders`).toEqual([]);
  } finally {
    await page.evaluate(() => { document.documentElement.style.fontSize = ""; });
    if (previousViewport) await page.setViewportSize(previousViewport);
  }
}

export async function metadata(request: APIRequestContext): Promise<E2eMetadata> {
  const response = await request.get("http://127.0.0.1:8000/__e2e__/metadata");
  expect(response.ok()).toBeTruthy();
  return await response.json() as E2eMetadata;
}

export async function login(
  page: Page,
  seed: E2eMetadata,
  username: string,
): Promise<void> {
  await page.goto("/login");
  await page.getByLabel("학교 코드").fill(seed.school_id);
  await page.getByLabel("아이디").fill(username);
  await page.getByLabel("비밀번호").fill(seed.password);
  await page.getByRole("button", { name: "로그인" }).click();
  await expect(page).toHaveURL(/\/workspaces$/);
  await expect(page.getByRole("heading", { name: "내 수서 업무" })).toBeVisible();
}
