import { useEffect, useRef, type ReactNode } from "react";

export function Modal({ title, description, children, onClose, returnFocusTarget, wide = false, busy = false, className = "" }: { title: string; description?: string; children: ReactNode; onClose: () => void; returnFocusTarget?: () => HTMLElement | null; wide?: boolean; busy?: boolean; className?: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const busyRef = useRef(busy);
  useEffect(() => { busyRef.current = busy; }, [busy]);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const panel = ref.current;
    const visible = (item: HTMLElement) => !item.hidden && !item.closest('[hidden], details:not([open])') && item.getAttribute('type') !== 'hidden';
    ([...panel?.querySelectorAll<HTMLElement>('input:not([type="file"]):not(:disabled),select:not(:disabled),textarea:not(:disabled)') ?? []].find(visible) ?? panel?.querySelector<HTMLElement>("button:not(:disabled)"))?.focus();
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape" && !busyRef.current) onClose();
      if (event.key !== "Tab" || !panel) return;
      const items = [...panel.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea:not(:disabled),a[href],[tabindex="0"],summary')].filter(item => visible(item) || (item.tagName === "SUMMARY" && !item.parentElement?.parentElement?.closest('details:not([open]),[hidden]')));
      const first = items[0], last = items.at(-1);
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }
    document.addEventListener("keydown", onKey);
    const overflow = document.body.style.overflow; document.body.style.overflow = "hidden";
    return () => { document.removeEventListener("keydown", onKey); document.body.style.overflow = overflow; (returnFocusTarget?.() ?? previous)?.focus(); };
  }, [onClose, returnFocusTarget]);
  return <div className="simple-modal-shade"><div ref={ref} className={`simple-modal${wide ? " wide" : ""}${className ? ` ${className}` : ""}`} role="dialog" aria-modal="true" aria-labelledby="simple-dialog-title">
    <header className="modal-heading"><div><p className="eyebrow">수서로 · 내 서재 관리</p><h2 id="simple-dialog-title">{title}</h2>{description && <p className="muted">{description}</p>}</div><button className="icon-button" aria-label="닫기" onClick={onClose} disabled={busy}>×</button></header>
    {children}
  </div></div>;
}

export function InlineError({ message }: { message: string }) { return message ? <div className="inline-error" role="alert">{message}</div> : null; }
