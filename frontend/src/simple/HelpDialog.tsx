import { useEffect, useRef, useState } from "react";
import { Modal } from "./Modal";

const guides = [
  { file: "quick-start.html", title: "처음 따라 하기" },
  { file: "visual-guide.html", title: "그림 안내" },
  { file: "user-guide.html", title: "상세 설명서" },
] as const;

export function HelpDialog({ onClose }: { onClose: () => void }) {
  const [guide, setGuide] = useState<(typeof guides)[number]>(guides[0]);
  const [loading, setLoading] = useState(true);
  const [revision, setRevision] = useState(0);
  const frame = useRef<HTMLIFrameElement>(null);
  const source = `/help/${guide.file}`;

  useEffect(() => {
    function handleMessage(event: MessageEvent<unknown>) {
      // Sandboxed documents have an opaque origin. Only this frame may close its guide.
      if (event.source === frame.current?.contentWindow && event.data && typeof event.data === "object" && "type" in event.data && event.data.type === "suseoro-help-close") onClose();
    }
    window.addEventListener("message", handleMessage);
    return () => window.removeEventListener("message", handleMessage);
  }, [onClose]);

  function openGuide(next: (typeof guides)[number]) { setLoading(true); setGuide(next); setRevision(value => value + 1); }

  return <Modal title="사용설명서" description="처음이라면 '처음 따라 하기'에서 연습용 책 한 권으로 시작해 보세요." onClose={onClose} wide className="help-modal">
    <nav className="help-navigation" aria-label="설명서 선택">{guides.map(item => <button key={item.file} className={`button ${item.file === guide.file ? "primary" : "secondary"}`} aria-pressed={item.file === guide.file} onClick={() => openGuide(item)}>{item.title}</button>)}</nav>
    <div className="help-frame-wrap">
      {loading && <p className="help-loading" role="status">설명서를 여는 중…</p>}
      <iframe ref={frame} key={`${guide.file}-${revision}`} className="help-frame" src={source} title={`수서로 사용설명서 · ${guide.title}`} sandbox="allow-scripts allow-modals allow-popups allow-popups-to-escape-sandbox" tabIndex={0} onLoad={() => setLoading(false)} />
    </div>
    <footer className="help-footer"><div><p>인터넷 연결 없이 읽을 수 있어요. 새 창에 띄우면 프로그램과 나란히 볼 수 있습니다.</p><a href={source} target="_blank" rel="noreferrer">새 창으로 열어 나란히 보기 ↗</a><button className="text-button" onClick={() => openGuide(guide)}>다시 열기</button></div><button className="button primary" onClick={onClose}>작업 화면으로 돌아가기</button></footer>
  </Modal>;
}
