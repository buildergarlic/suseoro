import type { useDisplaySettings } from "./useDisplaySettings";
import { DEFAULT_TEXT_SIZE, TEXT_SIZES } from "./displayOptions";

export function DisplaySettings({ settings, disabled = false }: { settings: ReturnType<typeof useDisplaySettings>; disabled?: boolean }) {
  const sizeIndex = TEXT_SIZES.indexOf(settings.fontSize);
  return <div className="workspace-tools" aria-label="화면 보기 설정">
    <div className="text-size-controls" role="group" aria-label="글자 크기 조절">
      <span className="text-size-label">글자 크기</span>
      <button className="button secondary text-size-step" disabled={disabled || sizeIndex <= 0} aria-label="글자 작게" title="글자 작게" onClick={() => settings.setFontSize(TEXT_SIZES[sizeIndex - 1])}>A−</button>
      <select aria-label="글자 크기" disabled={disabled} value={settings.fontSize} onChange={e => settings.setFontSize(Number(e.target.value))}>
        {TEXT_SIZES.map(size => <option key={size} value={size}>{size}px{size === DEFAULT_TEXT_SIZE ? " · 기본" : ""}</option>)}
      </select>
      <button className="button secondary text-size-step" disabled={disabled || sizeIndex >= TEXT_SIZES.length - 1} aria-label="글자 크게" title="글자 크게" onClick={() => settings.setFontSize(TEXT_SIZES[sizeIndex + 1])}>A+</button>
    </div>
    <button className="button secondary small" disabled={disabled} aria-pressed={settings.density === "compact"} aria-label="간결한 행 간격" onClick={() => settings.setDensity(settings.density === "compact" ? "comfortable" : "compact")}>간결하게</button>
  </div>;
}

