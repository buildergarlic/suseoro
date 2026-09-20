import { useEffect, useState } from "react";
import { canonicalIsbn } from "./isbn";
import { CommandIcon } from "./CommandIcon";

function MissingCover({ title }: { title: string }) {
  return <div className="book-cover book-cover-placeholder" role="img" aria-label={`${title || "도서"} 표지 없음`}>
    <CommandIcon name="book" /><span>표지 없음</span>
  </div>;
}

function CoverImage({ isbn, title }: { isbn: string; title: string }) {
  const [missing, setMissing] = useState(false);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    if (!missing || attempt >= 3) return;
    const timer = window.setTimeout(() => { setAttempt(value => value + 1); setMissing(false); }, [1000, 3000, 8000][attempt]);
    return () => window.clearTimeout(timer);
  }, [missing, attempt]);
  if (missing) return <div className="book-cover book-cover-placeholder">
    <CommandIcon name="book" />
    {attempt < 3 ? <span role="status">재시도 중</span> : <button className="cover-retry" aria-label={`${title || "도서"} 표지 다시 불러오기`} onClick={() => { setAttempt(0); setMissing(false); }}>표지 재조회</button>}
  </div>;
  return <div className="book-cover"><img className="book-cover-image" src={`/api/library/covers/${isbn}${attempt ? `?retry=${attempt}` : ""}`} alt={`${title || "도서"} 표지`} width={96} height={138} loading="lazy" decoding="async" referrerPolicy="no-referrer" onError={() => setMissing(true)} /></div>;
}

export function BookCover({ isbn, title }: { isbn: string; title: string }) {
  const canonical = canonicalIsbn(isbn);
  return canonical ? <CoverImage key={canonical} isbn={canonical} title={title} /> : <MissingCover title={title} />;
}
