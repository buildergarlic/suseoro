import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { createLibraryApi } from "../src/simple/api";
import { ListDialog } from "../src/simple/SettingsDialog";
import type { AcquisitionList } from "../src/simple/types";

const list: AcquisitionList = { id: "list-1", name: "2026 도서 구입", year: 2026, budget: 15_000_000, discount_percent: 10, created_at: "2026-09-25" };

function renderDialog() {
  const api = Object.assign(createLibraryApi(async () => new Response("{}")), {
    updateList: vi.fn(async (_id: string, fields: Partial<AcquisitionList>) => ({ ...list, ...fields })),
  });
  render(<ListDialog list={list} api={api} onClose={vi.fn()} onSaved={vi.fn(async () => {})} />);
  return api;
}

it("lets a librarian clear each numeric list setting without inserting zero", async () => {
  const user = userEvent.setup();
  renderDialog();

  for (const name of ["연도", "예산 (원)", "할인율 (%)"]) {
    const input = screen.getByRole("spinbutton", { name });
    await user.clear(input);
    expect((input as HTMLInputElement).value, `${name} should remain editable while empty`).toBe("");
  }
});

it("saves the replacement budget after clearing the previous value", async () => {
  const user = userEvent.setup();
  const api = renderDialog();
  const budget = screen.getByRole("spinbutton", { name: "예산 (원)" });

  await user.clear(budget);
  expect((budget as HTMLInputElement).value).toBe("");
  await user.type(budget, "50505050");
  await user.click(screen.getByRole("button", { name: "저장" }));

  expect(api.updateList).toHaveBeenCalledWith("list-1", { name: list.name, year: 2026, budget: 50_505_050, discount_percent: 10 });
});
