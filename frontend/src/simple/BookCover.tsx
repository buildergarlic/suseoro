import { useState } from "react";
import { canonicalIsbn } from "./isbn";
import { CommandIcon } from "./CommandIcon";

function MissingCover({ title }: { title: string }) {
  return <div className="book-cover book-cover-placeholder" role="img" aria-label={`${title || "도서"} 표지 없음`}>
    <CommandIcon name="book" /><span>표지 없음</span>
  </div>;
}

function CoverImage({ isbn, title }: { isbn: string; title: string }) {
  const [missing, setMissing] = useState(false);
  if (missing) return <MissingCover title={title} />;
  return <div className="book-cover"><img className="book-cover-image" src={`/api/library/covers/${isbn}`} alt={`${title || "도서"} 표지`} width={96} height={138} loading="lazy" decoding="async" referrerPolicy="no-referrer" onError={() => setMissing(true)} /></div>;
}

export function BookCover({ isbn, title }: { isbn: string; title: string }) {
  const canonical = canonicalIsbn(isbn);
  return canonical ? <CoverImage key={canonical} isbn={canonical} title={title} /> : <MissingCover title={title} />;
}
