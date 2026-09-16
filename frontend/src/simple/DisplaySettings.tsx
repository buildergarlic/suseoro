import type { useDisplaySettings } from "./useDisplaySettings";

export function DisplaySettings({ settings, disabled = false }: { settings: ReturnType<typeof useDisplaySettings>; disabled?: boolean }) {
  return <div className="workspace-tools" aria-label="화면 보기 설정">
    <label>글자 크기<select aria-label="글자 크기" disabled={disabled} value={settings.fontSize} onChange={e => settings.setFontSize(Number(e.target.value))}>
      <option value="16">기본 · 16</option><option value="18">크게 · 18</option><option value="20">더 크게 · 20</option>
    </select></label>
    <button className="button secondary small" disabled={disabled} aria-pressed={settings.density === "compact"} aria-label="간결한 행 간격" onClick={() => settings.setDensity(settings.density === "compact" ? "comfortable" : "compact")}>간결하게</button>
  </div>;
}

