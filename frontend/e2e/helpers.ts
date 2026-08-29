import { expect, type APIRequestContext, type Page } from "@playwright/test";

export interface E2eMetadata {
  school_id: string;
  workspace_id: string;
  accessibility_workspace_id: string;
  password: string;
  operator_username: string;
  reviewer_username: string;
  isbns: string[];
  delivery_xlsx_base64: string;
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
