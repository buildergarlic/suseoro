import { canonicalIsbn } from "./isbn";

function safeReference(value: string): string | null {
  try {
    const url = new URL(value);
    return ["https:", "http:"].includes(url.protocol) && !url.username && !url.password ? url.href : null;
  } catch { return null; }
}
export function BookLinks({ isbn, title, link = "" }: { isbn: string; title: string; link?: string }) {
  const code = canonicalIsbn(isbn), reference = safeReference(link);
  const aladin = code ? `https://www.aladin.co.kr/search/wsearchresult.aspx?SearchTarget=Book&SearchWord=${code}` : null;
  const library = code ? `https://www.nl.go.kr/NL/contents/search.do?kwd=${code}` : null;
  if (!code && !reference) return null;
  return <div className="book-links">
    {aladin && <a href={aladin} target="_blank" rel="noopener noreferrer" aria-label={`${title || "도서"} 알라딘 ISBN 검색`}>알라딘 ↗</a>}
    {library && <a href={library} target="_blank" rel="noopener noreferrer" aria-label={`${title || "도서"} 국립중앙도서관 ISBN 검색`}>국립중앙도서관 ↗</a>}
    {reference && reference !== library && reference !== aladin && <a href={reference} target="_blank" rel="noopener noreferrer" aria-label={`${title || "도서"} 참고 링크`}>참고 링크 ↗</a>}
  </div>;
}
