import { act, renderHook } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { createLibraryApi } from "../src/simple/api";
import { emptyBook, type Book } from "../src/simple/types";
import { useBulkReview } from "../src/simple/useBulkReview";

it("retries only the read after a saved bulk change or undo cannot refresh the screen", async () => {
  const api = Object.assign(createLibraryApi(), {
    bulkBooks: vi.fn(async () => ({ updated: 1, skipped: [], operation_id: "operation" })),
    undoOperation: vi.fn(async () => ({ restored: 1 })),
  });
  const book: Book = { ...emptyBook(), id: "a", title: "책", held: false, duplicate: false };
  const refresh = vi.fn<() => Promise<void>>().mockRejectedValueOnce(new Error("화면 갱신 실패")).mockResolvedValue(undefined);
  const { result } = renderHook(() => useBulkReview(api, "list", "scope", [book], refresh));
  act(() => result.current.setChecked(["a"]));
  await act(async () => result.current.run("hold"));
  expect(result.current.refreshNeeded).toBe(true);
  await act(async () => result.current.retryRefresh());
  expect(result.current.refreshNeeded).toBe(false);
  expect(api.bulkBooks).toHaveBeenCalledOnce();
  refresh.mockRejectedValueOnce(new Error("다시 갱신 실패"));
  await act(async () => result.current.undo());
  expect(result.current.refreshNeeded).toBe(true);
  await act(async () => result.current.retryRefresh());
  expect(api.undoOperation).toHaveBeenCalledOnce();
  expect(result.current.refreshNeeded).toBe(false);
});
