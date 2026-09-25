import { useEffect, useMemo, useState, type FormEvent } from "react";
import type { LibraryApi } from "./api";
import { errorMessage, money, type Book, type BookFields, type HoldingsPage } from "./types";
import "./registered-library-views.css";

const HOLDINGS_PAGE_SIZE = 50;
const UNKNOWN_SOURCE = "출처 미입력";

type HoldingsLibraryViewProps = {
  api: Pick<LibraryApi, "holdings">;
  onImport: () => void;
  refreshKey: number;
};

function sourceName(book: Pick<BookFields, "source">) {
  return book.source.trim() || UNKNOWN_SOURCE;
}

function recommendationGroupName(book: Book) {
  const filename = book.provenance?.filename?.trim();
  if (!filename) return sourceName(book);
  const filenameStart = book.source.indexOf(filename);
  if (filenameStart < 0) return sourceName(book);
  const institution = book.source.slice(0, filenameStart).replace(/\s*·\s*$/, "").trim();
  return institution ? `${institution} · ${filename}` : filename;
}

export function HoldingsLibraryView({ api, onImport, refreshKey }: HoldingsLibraryViewProps) {
  const [searchInput, setSearchInput] = useState("");
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(1);
  const [retryKey, setRetryKey] = useState(0);
  const [result, setResult] = useState<HoldingsPage | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    void api.holdings({ query, page, page_size: HOLDINGS_PAGE_SIZE }).then(data => {
      if (!active) return;
      if (page > 1 && data.total <= (page - 1) * HOLDINGS_PAGE_SIZE) {
        setPage(1);
        return;
      }
      setResult(data);
      setError("");
      setLoading(false);
    }).catch((caught: unknown) => {
      if (!active) return;
      setResult(null);
      setError(errorMessage(caught));
      setLoading(false);
    });
    return () => { active = false; };
  }, [api, query, page, refreshKey, retryKey]);

  function search(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setPage(1);
    setQuery(searchInput.trim());
    setLoading(true);
    setRetryKey(value => value + 1);
  }

  function clearSearch() {
    setSearchInput("");
    setPage(1);
    setQuery("");
    setLoading(true);
    setRetryKey(value => value + 1);
  }

  function goToPage(next: number) {
    setPage(next);
    setLoading(true);
  }

  const total = result?.total ?? 0;
  const first = total ? (page - 1) * HOLDINGS_PAGE_SIZE + 1 : 0;
  const last = Math.min(page * HOLDINGS_PAGE_SIZE, total);

  return <section className="registered-view" aria-label="등록된 학교 소장목록">
    <header className="registered-heading">
      <div><h2>학교 소장목록</h2><p>등록된 학교 전체 소장자료를 검색하고 확인할 수 있습니다. 이 화면은 읽기 전용입니다.</p></div>
      <button type="button" className="button secondary" onClick={onImport}>소장목록 가져오기</button>
    </header>
    <div className="registered-toolbar">
      <form className="registered-search" role="search" onSubmit={search}>
        <label htmlFor="registered-holdings-search" className="sr-only">소장목록 검색</label>
        <input id="registered-holdings-search" type="search" value={searchInput} onChange={event => setSearchInput(event.target.value)} placeholder="책 제목, 저자, ISBN, 출판사, 출처 검색" maxLength={200} />
        <button type="submit" className="button primary small">검색</button>
        {query && <button type="button" className="button secondary small" onClick={clearSearch}>검색 해제</button>}
      </form>
      <span className="registered-count" aria-live="polite">{loading ? "조회 중…" : error ? "조회 실패" : `총 ${total.toLocaleString("ko-KR")}권`}</span>
    </div>
    {error && <div className="registered-error" role="alert"><span>{error}</span><button type="button" className="button secondary small" onClick={() => { setLoading(true); setRetryKey(value => value + 1); }}>다시 시도</button></div>}
    <div className="registered-table-wrap">
      <table className="registered-table" aria-label="학교 소장목록" aria-busy={loading}>
        <thead><tr><th scope="col">도서명</th><th scope="col">저자</th><th scope="col">출판사</th><th scope="col">ISBN</th><th scope="col">출처</th></tr></thead>
        <tbody>
          {loading ? <tr><td colSpan={5} className="registered-empty">소장목록을 불러오는 중입니다.</td></tr>
            : error ? <tr><td colSpan={5} className="registered-empty">조회할 수 없습니다. 다시 시도해 주세요.</td></tr>
              : !result?.items.length ? <tr><td colSpan={5} className="registered-empty">{query ? "검색 조건에 맞는 소장자료가 없습니다." : "등록된 학교 소장목록이 없습니다. 소장목록을 가져와 주세요."}</td></tr>
                : result.items.map((book, index) => <tr key={`${page}-${index}`}>
                  <td className="registered-title">{book.title || "제목 미입력"}</td>
                  <td>{book.author || <span className="registered-muted">—</span>}</td>
                  <td>{book.publisher || <span className="registered-muted">—</span>}</td>
                  <td className="registered-isbn">{book.isbn || <span className="registered-muted">—</span>}</td>
                  <td>{sourceName(book)}</td>
                </tr>)}
        </tbody>
      </table>
    </div>
    <footer className="registered-footer">
      <span>{!loading && !error && total > 0 ? `${first.toLocaleString("ko-KR")}–${last.toLocaleString("ko-KR")} / ${total.toLocaleString("ko-KR")}권` : ""}</span>
      <div className="registered-pages">
        <button type="button" className="button secondary small" onClick={() => goToPage(page - 1)} disabled={loading || page <= 1}>이전 페이지</button>
        <span aria-live="polite">{page} / {Math.max(1, Math.ceil(total / HOLDINGS_PAGE_SIZE))}</span>
        <button type="button" className="button secondary small" onClick={() => goToPage(page + 1)} disabled={loading || !result || page * HOLDINGS_PAGE_SIZE >= total}>다음 페이지</button>
      </div>
    </footer>
  </section>;
}

type RecommendationsLibraryViewProps = { books: Book[]; listName: string };

export function RecommendationsLibraryView({ books, listName }: RecommendationsLibraryViewProps) {
  const [search, setSearch] = useState("");
  const [selectedSource, setSelectedSource] = useState("");
  const sources = useMemo(() => [...new Set(books.map(recommendationGroupName))].sort((a, b) => a.localeCompare(b, "ko-KR")), [books]);
  const sourceFilter = sources.includes(selectedSource) ? selectedSource : "";
  const groups = useMemo(() => {
    const query = search.trim().toLocaleLowerCase("ko-KR");
    const grouped = new Map<string, Book[]>();
    for (const book of books) {
      const source = recommendationGroupName(book);
      if (sourceFilter && source !== sourceFilter) continue;
      if (query && ![book.title, book.author, book.publisher, book.isbn, book.source, book.category, book.requester, book.note]
        .some(value => value.toLocaleLowerCase("ko-KR").includes(query))) continue;
      const items = grouped.get(source) ?? [];
      items.push(book);
      grouped.set(source, items);
    }
    return [...grouped].sort(([a], [b]) => a.localeCompare(b, "ko-KR"));
  }, [books, search, sourceFilter]);
  const visibleCount = groups.reduce((sum, [, items]) => sum + items.length, 0);

  return <section className="registered-view" aria-label="저장된 추천도서 조회">
    <header className="registered-heading"><div>
      <h2>출처별 저장 도서</h2>
      <p>“{listName}”에 저장된 도서를 추천 출처별로 묶었습니다. 원본 업로드 이력이 아닙니다.</p>
    </div></header>
    <div className="registered-toolbar">
      <div className="registered-filters">
        <label className="registered-filter"><span>추천 출처</span><select value={sourceFilter} onChange={event => setSelectedSource(event.target.value)}><option value="">전체 출처</option>{sources.map(source => <option value={source} key={source}>{source}</option>)}</select></label>
        <label className="registered-search"><span className="sr-only">추천도서 검색</span><input type="search" value={search} onChange={event => setSearch(event.target.value)} placeholder="제목, 저자, ISBN, 출처 검색" /></label>
      </div>
      <span className="registered-count" aria-live="polite">{visibleCount.toLocaleString("ko-KR")}권 / 전체 {books.length.toLocaleString("ko-KR")}권</span>
    </div>
    <div className="registered-table-wrap">
      <table className="registered-table registered-recommendations-table" aria-label="추천 출처별 저장 도서">
        <thead><tr><th scope="col">도서명</th><th scope="col">저자</th><th scope="col">출판사</th><th scope="col">ISBN</th><th scope="col">정가</th><th scope="col">구입 선택</th></tr></thead>
        {!groups.length ? <tbody><tr><td colSpan={6} className="registered-empty">{books.length ? "검색 조건에 맞는 도서가 없습니다." : "현재 구입목록에 저장된 도서가 없습니다."}</td></tr></tbody>
          : groups.map(([source, items]) => <tbody key={source}>
            <tr className="registered-group"><th scope="rowgroup" colSpan={6}><span>{source}</span><small>{items.length.toLocaleString("ko-KR")}권</small></th></tr>
            {items.map(book => <tr key={book.id}>
              <td className="registered-title">{book.title || "제목 미입력"}</td>
              <td>{book.author || <span className="registered-muted">—</span>}</td>
              <td>{book.publisher || <span className="registered-muted">—</span>}</td>
              <td className="registered-isbn">{book.isbn || <span className="registered-muted">—</span>}</td>
              <td className="registered-price">{book.price === null ? "가격 미확인" : money(book.price)}</td>
              <td>{book.selected ? "선택" : "보류"}</td>
            </tr>)}
          </tbody>)}
      </table>
    </div>
    <footer className="registered-footer"><span>현재 구입목록의 저장된 도서를 표시합니다.</span></footer>
  </section>;
}
