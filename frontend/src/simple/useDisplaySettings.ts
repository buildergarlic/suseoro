import { useCallback, useEffect, useRef, useState } from "react";
import type { LibraryApi } from "./api";
import { errorMessage, type Settings } from "./types";

function read(key: string, fallback: string) {
  try { return localStorage.getItem(key) ?? fallback; } catch { return fallback; }
}
function save(key: string, value: string) {
  try { localStorage.setItem(key, value); } catch { /* Readability still works if storage is blocked. */ }
}
export function useDisplaySettings(api: LibraryApi) {
  const [fontSize, applyFontSize] = useState(() => {
    const value = Number(read("suseoro.text-size", "16"));
    return [16, 18, 20].includes(value) ? value : 16;
  });
  const [density, applyDensity] = useState<"comfortable" | "compact">(() => read("suseoro.density", "comfortable") === "compact" ? "compact" : "comfortable");
  const [saveError, setSaveError] = useState("");
  const saves = useRef(Promise.resolve());
  // The desktop server uses a new port on each launch. App data is authoritative;
  // localStorage is only a same-origin cache while bootstrap is loading.
  const hydrate = useCallback((settings: Settings) => {
    if (settings.text_size && [16, 18, 20].includes(settings.text_size)) applyFontSize(settings.text_size);
    if (settings.row_density === "comfortable" || settings.row_density === "compact") applyDensity(settings.row_density);
  }, []);
  function persist(patch: Parameters<LibraryApi["settings"]>[0]) {
    saves.current = saves.current.then(async () => { await api.settings(patch); setSaveError(""); }).catch((caught: unknown) => setSaveError(`화면 설정을 저장하지 못했습니다. ${errorMessage(caught)}`));
  }
  function setFontSize(value: number) {
    if (![16, 18, 20].includes(value)) return;
    applyFontSize(value); persist({ text_size: value });
  }
  function setDensity(value: "comfortable" | "compact") { applyDensity(value); persist({ row_density: value }); }
  useEffect(() => {
    const original = document.documentElement.style.fontSize;
    document.documentElement.style.fontSize = `${fontSize}px`;
    save("suseoro.text-size", String(fontSize));
    return () => { document.documentElement.style.fontSize = original; };
  }, [fontSize]);
  useEffect(() => { save("suseoro.density", density); }, [density]);
  return { fontSize, setFontSize, density, setDensity, hydrate, saveError };
}
