export type ScanCode =
  | "NORMAL"
  | "OVER"
  | "NOT_ORDERED"
  | "EDITION_MISMATCH";

export const SCAN_AUDIO_PATTERNS: Record<ScanCode, readonly number[]> = {
  NORMAL: [740],
  OVER: [330, 250],
  NOT_ORDERED: [220, 220, 220],
  EDITION_MISMATCH: [520, 360, 520],
};

export function playScanAudio(code: ScanCode) {
  if (typeof window === "undefined" || !window.AudioContext) return;
  try {
    const context = new AudioContext();
    let at = context.currentTime;
    for (const frequency of SCAN_AUDIO_PATTERNS[code]) {
      const oscillator = context.createOscillator();
      const gain = context.createGain();
      oscillator.frequency.value = frequency;
      gain.gain.setValueAtTime(0.08, at);
      gain.gain.exponentialRampToValueAtTime(0.001, at + 0.11);
      oscillator.connect(gain).connect(context.destination);
      oscillator.start(at);
      oscillator.stop(at + 0.12);
      at += 0.14;
    }
    window.setTimeout(
      () => void context.close(),
      Math.ceil((at - context.currentTime + 0.2) * 1000),
    );
  } catch {
    // Sound is an additional cue; the large live text remains authoritative.
  }
}
