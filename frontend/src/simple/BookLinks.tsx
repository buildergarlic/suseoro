/** ISBN links are searches; provider record identifiers are never invented. */
function canonicalIsbn(value: string): string | null {
  const digits = value.normalize("NFKC").replace(/[\s-]/g, "").toUpperCase();
  if (/^\d{9}[\dX]$/.test(digits)) {
    const sum = [...digits].reduce((n, digit, i) => n + (digit === "X" ? 10 : Number(digit)) * (10 - i), 0);
    if (sum % 11) return null;
    const prefix = "978" + digits.slice(0, 9);
    const check = (10 - [...prefix].reduce((n, digit, i) => n + Number(digit) * (i % 2 ? 3 : 1), 0) % 10) % 10;
    return prefix + check;
  }
  if (!/^97[89]\d{10}$/.test(digits)) return null;
  return [...digits].reduce((n, digit, i) => n + Number(digit) * (i % 2 ? 3 : 1), 0) % 10 === 0 ? digits : null;
}
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
