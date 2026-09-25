import { useState } from "react";
import type { LibraryApi } from "./api";
import { errorMessage, type Book, type BulkAction, type BulkResult } from "./types";

export function useBulkReview(api: LibraryApi, listId: string, scope: string, books: Book[], refresh: () => Promise<void>) {
  const [selection, setSelection] = useState<{ scope: string; ids: Set<string> }>({ scope: "", ids: new Set() });
  const [busy, setBusy] = useState(false);
  const [refreshNeeded, setRefreshNeeded] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState<{ listId: string; value: BulkResult; label: string } | null>(null);
  const [operation, setOperation] = useState<{ listId: string; id: string; label: string } | null>(null);
  const existing = new Set(books.map(book => book.id));
  const checked = new Set(selection.scope === scope ? [...selection.ids].filter(id => existing.has(id)) : []);
  function setChecked(ids: Iterable<string>) { setSelection({ scope, ids: new Set(ids) }); }
  function toggle(id: string) { const next = new Set(checked); if (next.has(id)) next.delete(id); else next.add(id); setChecked(next); }
  function remember(id: string, label: string) { setOperation({ listId, id, label }); }
  async function reloadSaved() {
    try { await refresh(); setRefreshNeeded(false); }
    catch (caught) { setRefreshNeeded(true); setError(`저장은 완료됐지만 화면을 다시 읽지 못했습니다. ${errorMessage(caught)}`); }
  }
  async function retryRefresh() {
    if (busy) return;
    setBusy(true); setError("");
    try { await reloadSaved(); } finally { setBusy(false); }
  }
  async function run(action: BulkAction) {
    if (busy || refreshNeeded || !checked.size) return;
    setBusy(true); setError(""); setResult(null);
    const label = action === "confirm_metadata" ? "서지 확인" : action === "select" ? "구입 선택" : "보류";
    try {
      const value = await api.bulkBooks(listId, [...checked], action);
      setResult({ listId, value, label });
      if (value.updated && value.operation_id) remember(value.operation_id, label);
      setChecked(value.skipped.map(item => item.id));
      await reloadSaved();
    } catch (caught) { setError(errorMessage(caught)); }
    finally { setBusy(false); }
  }
  async function undo() {
    if (busy || refreshNeeded || !operation || operation.listId !== listId) return;
    setBusy(true); setError("");
    try {
      await api.undoOperation(listId, operation.id);
      setOperation(null); setResult(null); setChecked([]);
      await reloadSaved();
    } catch (caught) { setError(errorMessage(caught)); }
    finally { setBusy(false); }
  }
  return { checked, setChecked, toggle, run, undo, remember, busy, error, refreshNeeded, retryRefresh,
    result: result?.listId === listId ? result : null,
    operation: operation?.listId === listId ? operation : null };
}
