import { useCallback, useEffect, useRef, useState } from "react";
import { createLibraryApi, type LibraryApi } from "./api";
import { BulkReviewToolbar } from "./BulkReviewToolbar";
import { useBulkReview } from "./useBulkReview";
import { canonicalIsbn } from "./isbn";
import { BookDialog, IsbnDialog } from "./BookDialog";
import { ExportDialog } from "./ExportDialog";
import { ImportDialog } from "./ImportDialog";
import { HelpDialog } from "./HelpDialog";
import { HoldingsStatus } from "./HoldingsStatus";
import { BookCover } from "./BookCover";
import { BookLinks } from "./BookLinks";
import { CommandIcon } from "./CommandIcon";
import { DisplaySettings } from "./DisplaySettings";
import { HoldingsLibraryView, RecommendationsLibraryView } from "./RegisteredLibraryViews";
import { useDisplaySettings } from "./useDisplaySettings";
import { SupportDialog } from "./SupportDialog";
import { InlineError } from "./Modal";
import { ListDialog, SettingsDialog } from "./SettingsDialog";
import { errorMessage, money, orderAmount, type AcquisitionList, type Book, type Bootstrap, type ListDetail, type UpdateInfo } from "./types";

type Dialog = { kind: "import"; importKind?: "recommendations" | "holdings" } | { kind: "isbn" | "export" | "settings" | "help" | "support" | "new-list" | "edit-list" } | { kind: "book"; book?: Book };
type Tab = "all" | "selected" | "hold" | "review" | "held" | "no-isbn";
type View = "purchase" | "holdings" | "recommendations";
const defaultApi = createLibraryApi();
const needsAttention = (book: Book) => book.needs_review || book.price === null || !book.title.trim() || (!!book.isbn && !canonicalIsbn(book.isbn));

export function SimpleLibraryApp({ api = defaultApi }: { api?: LibraryApi }) {
  const display = useDisplaySettings(api);
  const hydrateDisplay = display.hydrate;
  const searchInput = useRef<HTMLInputElement>(null);
  const menuPanel = useRef<HTMLElement>(null), menuButton = useRef<HTMLButtonElement>(null), menuClose = useRef<HTMLButtonElement>(null);
  const menuWasOpen = useRef(false), dialogFromMenu = useRef(false);
  const [bootstrap, setBootstrap] = useState<Bootstrap | null>(null), [listId, setListId] = useState("");
  const [detail, setDetail] = useState<ListDetail | null>(null), [dialog, setDialog] = useState<Dialog | null>(null);
  const [error, setError] = useState(""), [notice, setNotice] = useState(""), [loading, setLoading] = useState(true), [pendingBook, setPendingBook] = useState("");
  const [search, setSearch] = useState(""), [tab, setTab] = useState<Tab>("all");
  const [page, setPage] = useState(0);
  const [view, setView] = useState<View>("purchase"), [menuOpen, setMenuOpen] = useState(false), [holdingsRevision, setHoldingsRevision] = useState(0);
  const [exited, setExited] = useState<"closed" | "updating" | null>(null);
  const [deleted, setDeleted] = useState<{ listId: string; book: Book } | null>(null);
  const requestNumber = useRef(0);
  const activeList = useRef("");
  const updateRevision = useRef(0);
  const closeDialog = useCallback(() => setDialog(null), []);
  function openDialog(next: Dialog) {
    if (menuOpen) { dialogFromMenu.current = true; setMenuOpen(false); }
    setDialog(next);
  }
  useEffect(() => {
    if (!dialog && dialogFromMenu.current) {
      dialogFromMenu.current = false;
      menuButton.current?.focus();
    }
  }, [dialog]);
  useEffect(() => {
    if (!menuOpen) {
      if (menuWasOpen.current && !dialog) menuButton.current?.focus();
      menuWasOpen.current = false;
      return;
    }
    menuWasOpen.current = true;
    menuClose.current?.focus();
    function onMenuKey(event: KeyboardEvent) {
      if (event.key === "Escape") { event.preventDefault(); setMenuOpen(false); return; }
      if (event.key !== "Tab") return;
      const focusable = [...menuPanel.current?.querySelectorAll<HTMLElement>('a[href], button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), summary, [tabindex]:not([tabindex="-1"])') ?? []]
        .filter(element => element.getClientRects().length > 0);
      const first = focusable[0], last = focusable.at(-1);
      if (!first || !last) return;
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
      else if (!menuPanel.current?.contains(document.activeElement)) { event.preventDefault(); first.focus(); }
    }
    document.addEventListener("keydown", onMenuKey);
    return () => document.removeEventListener("keydown", onMenuKey);
  }, [menuOpen, dialog]);
  useEffect(() => {
    function onResize() { if (window.innerWidth > 900) setMenuOpen(false); }
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);
  useEffect(() => {
    function searchShortcut(event: KeyboardEvent) {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "f" && !dialog && searchInput.current) {
        event.preventDefault(); searchInput.current.focus(); searchInput.current.select();
      }
    }
    document.addEventListener("keydown", searchShortcut);
    return () => document.removeEventListener("keydown", searchShortcut);
  }, [dialog]);
  const receiveUpdate = useCallback((update: UpdateInfo) => {
    updateRevision.current += 1;
    setBootstrap(previous => previous ? { ...previous, update } : previous);
  }, []);
  const initialize = useCallback(async () => {
    try { const result = await api.bootstrap(); hydrateDisplay(result.settings); setBootstrap(result); activeList.current = result.lists[0]?.id ?? ""; setListId(activeList.current); if (!result.lists.length) { setDetail(null); setLoading(false); } }
    catch (caught) { setError(errorMessage(caught)); setLoading(false); }
  }, [api, hydrateDisplay]);
  useEffect(() => {
    let active = true;
    void api.bootstrap().then(result => { if (active) { hydrateDisplay(result.settings); setBootstrap(result); activeList.current = result.lists[0]?.id ?? ""; setListId(activeList.current); if (!result.lists.length) setLoading(false); } }).catch((caught: unknown) => { if (active) { setError(errorMessage(caught)); setLoading(false); } });
    return () => { active = false; };
  }, [api, hydrateDisplay]);
  const initialized = bootstrap !== null;
  useEffect(() => {
    if (!initialized || exited) return;
    let active = true;
    let timer: number;
    async function poll() {
      const revision = updateRevision.current;
      try {
        const update = await api.updateStatus();
        // A late background response must not undo a preference just saved by the user.
        if (active && revision === updateRevision.current) setBootstrap(previous => previous ? { ...previous, update } : previous);
      } catch { /* Keep the last known state; a temporary polling failure does not interrupt work. */ }
      if (active) timer = window.setTimeout(() => { void poll(); }, 10_000);
    }
    timer = window.setTimeout(() => { void poll(); }, 10_000);
    return () => { active = false; window.clearTimeout(timer); };
  }, [api, initialized, exited]);
  const refresh = useCallback(async (id = listId) => {
    if (!id || id !== activeList.current) return;
    const sequence = ++requestNumber.current;
    const result = await api.list(id);
    if (sequence === requestNumber.current && activeList.current === id) { setDetail(result); setBootstrap(previous => previous ? { ...previous, lists: previous.lists.map(list => list.id === id ? result.list : list) } : previous); }
  }, [api, listId]);
  useEffect(() => {
    if (!listId) return;
    let active = true;
    void refresh(listId).then(() => { if (active) { setLoading(false); setError(""); } }).catch((caught: unknown) => { if (active) { setError(errorMessage(caught)); setLoading(false); } });
    return () => { active = false; };
  }, [listId, refresh]);
  function selectList(id: string) { if (view !== "recommendations") setView("purchase"); setMenuOpen(false); if (id === listId) return; activeList.current = id; setListId(id); setDetail(null); setLoading(true); setSearch(""); setTab("all"); setPage(0); setNotice(""); setError(""); }
  async function toggleBook(book: Book) { setPendingBook(book.id); setError(""); try { await api.updateBook(listId, book.id, { selected: !book.selected }); await refresh(); } catch (caught) { setError(errorMessage(caught)); } finally { setPendingBook(""); } }
  async function undoDelete() { if (!deleted) return; setError(""); try { await api.restoreBook(deleted.listId, deleted.book.id); if (listId === deleted.listId) await refresh(); setDeleted(null); setNotice("삭제한 책을 되돌렸습니다."); } catch (caught) { setError(errorMessage(caught)); } }
  async function listSaved(list: AcquisitionList) { setBootstrap(previous => previous ? { ...previous, lists: [...previous.lists.filter(item => item.id !== list.id), list] } : previous); if (listId === list.id) await refresh(list.id); else selectList(list.id); setNotice("목록 설정을 저장했습니다."); }
  const books = detail?.books ?? [], summary = detail?.summary;
  const selectedCount = books.filter(book => book.selected).length, holdCount = books.length - selectedCount, reviewCount = books.filter(needsAttention).length;
  const query = search.trim().toLocaleLowerCase("ko-KR");
  const visible = books.filter(book => (tab === "all" || (tab === "selected" && book.selected) || (tab === "hold" && !book.selected) || (tab === "held" && book.held) || (tab === "no-isbn" && !book.isbn.trim()) || (tab === "review" && needsAttention(book))) && (!query || [book.title, book.author, book.isbn, book.publisher, book.source, book.requester, book.category, book.note, ...(book.sources ?? [])].some(value => value.toLocaleLowerCase("ko-KR").includes(query))));
  const pageCount = Math.max(1, Math.ceil(visible.length / 100));
  const currentPage = Math.min(page, pageCount - 1);
  const pageBooks = visible.slice(currentPage * 100, (currentPage + 1) * 100);
  const bulk = useBulkReview(api, listId, JSON.stringify([listId, tab, query]), visible, refresh);
  const years = [...new Set(bootstrap?.lists.map(list => list.year) ?? [])].sort((a, b) => b - a);
  const percent = detail && summary && detail.list.budget > 0 ? (summary.order_total / detail.list.budget) * 100 : 0;
  const school = bootstrap?.settings.school_name || "우리 학교";
  const update = bootstrap?.update;
  const automaticOnExit = update?.auto_supported === true && update.auto_enabled === true;
  if (exited) return <main className="simple-app"><div className="empty-state"><h1>{exited === "updating" ? "업데이트 설치를 시작했습니다." : "수서로를 종료했습니다."}</h1><p>{exited === "updating" ? "설치 창의 안내를 따라 주세요. 이 브라우저 탭은 닫아도 됩니다." : "자료를 안전하게 보관했습니다. 이 브라우저 탭을 닫아 주세요."}</p></div></main>;
  return <div className={`simple-app density-${display.density}${menuOpen ? " menu-open" : ""}`}>
    <a href="#book-list" className="skip-link">도서 목록으로 바로가기</a>
    <aside ref={menuPanel} className="sidebar" aria-label="작업 메뉴" role={menuOpen ? "dialog" : undefined} aria-modal={menuOpen || undefined}>
      <div className="brand"><CommandIcon name="book" /><div><strong>수서로</strong><small>{school} · 학교도서관</small></div><button ref={menuClose} type="button" className="icon-button sidebar-close" aria-label="메뉴 닫기" onClick={() => setMenuOpen(false)}>×</button></div>
      <div className="sidebar-scroll">
        <div className="sidebar-section-heading"><span>내 구입 목록</span><button className="icon-button" aria-label="새 구입 목록" onClick={() => openDialog({ kind: "new-list" })} disabled={!bootstrap}>＋</button></div>
        <nav className="purchase-nav" aria-label="구입 목록">{years.map(year => <div className="year-group" key={year}><span className="year-label">{year}</span>{bootstrap?.lists.filter(list => list.year === year).map(list => <button key={list.id} className={`list-nav ${list.id === listId && view === "purchase" ? "active" : ""}`} aria-current={list.id === listId && view === "purchase" ? "page" : undefined} disabled={bulk.busy} onClick={() => selectList(list.id)}><CommandIcon name="list" /><span>{list.name}</span></button>)}</div>)}</nav>
        <button className="new-list-link" onClick={() => openDialog({ kind: "new-list" })} disabled={!bootstrap}><span aria-hidden="true">＋</span> 새 목록 만들기</button>
        {detail && summary && <section className="sidebar-budget" aria-label="예산 현황">
          <div className="sidebar-section-heading"><span>이번 목록 예산</span><button className="sidebar-text-action" onClick={() => openDialog({ kind: "edit-list" })}>목록 · 예산 설정</button></div>
          <dl><div><dt>전체 예산</dt><dd>{money(detail.list.budget)}</dd></div><div><dt>구입 선택 금액</dt><dd>{money(summary.order_total)}</dd></div><div className={summary.remaining < 0 ? "over-budget" : ""}><dt>{summary.remaining < 0 ? "예산 초과 금액" : "남은 예산"}</dt><dd>{money(Math.abs(summary.remaining))}</dd></div></dl>
          <div className="sidebar-budget-progress"><div className="progress-track" role="progressbar" aria-label="예산 사용률" aria-valuenow={Math.min(100, Math.round(percent))} aria-valuemin={0} aria-valuemax={100} aria-valuetext={`예산의 ${percent.toFixed(1)}% 선택`}><div className={summary.remaining < 0 ? "over" : ""} style={{ width: `${Math.min(100, Math.max(0, percent))}%` }} /></div><span>{percent.toFixed(1)}%</span></div>
          <p className="sidebar-budget-meta">{selectedCount}종 · {summary.total_quantity}권 선택 · 할인율 {detail.list.discount_percent}%</p>
          {summary.missing_price_count > 0 && <p className="budget-warning">가격 미확인 {summary.missing_price_count}종은 선택 금액에 포함되지 않았어요.</p>}
        </section>}
        <div className="sidebar-section-heading sidebar-work-heading">주요 작업</div>
        <div className="sidebar-actions"><button className="button primary" onClick={() => openDialog({ kind: "isbn" })} disabled={!detail}><CommandIcon name="add" /> ISBN 추가</button><button className="button secondary" onClick={() => openDialog({ kind: "import" })} disabled={!detail}><CommandIcon name="upload" /> 파일 가져오기</button><button className="button secondary" onClick={() => openDialog({ kind: "export" })} disabled={!detail}><CommandIcon name="download" /> 발주서 저장</button></div>
        <div className="sidebar-section-heading sidebar-data-heading">등록 자료</div>
        <nav className="registered-nav" aria-label="등록 자료"><button className={`list-nav${view === "holdings" ? " active" : ""}`} aria-current={view === "holdings" ? "page" : undefined} aria-label="학교 소장목록 열람" onClick={() => { setView("holdings"); setMenuOpen(false); }}><CommandIcon name="book" /><span>학교 소장목록</span><small>{detail?.holdings_count?.toLocaleString("ko-KR") ?? 0}권</small></button><button className={`list-nav${view === "recommendations" ? " active" : ""}`} aria-current={view === "recommendations" ? "page" : undefined} aria-label="추천도서 목록 열람" onClick={() => { setView("recommendations"); setMenuOpen(false); }}><CommandIcon name="list" /><span>추천도서 목록</span></button></nav>
        <div className="sidebar-holdings-status"><span>{detail?.holdings_count ? `소장자료 ${detail.holdings_count.toLocaleString("ko-KR")}권 · 중복 ${books.filter(book => book.held).length}종` : "소장자료를 등록하면 중복을 표시합니다."}</span><button className="sidebar-text-action" onClick={() => openDialog({ kind: "import", importKind: "holdings" })} disabled={!detail}>{detail?.holdings_count ? "소장목록 갱신" : "소장목록 가져오기"}</button></div>
      <div className="sidebar-bottom"><DisplaySettings settings={display} disabled={!bootstrap} /><div className="local-note"><span className="status-dot" /><span>내 컴퓨터에 자동 저장</span></div><button className="settings-button" onClick={() => openDialog({ kind: "settings" })} disabled={!bootstrap}><CommandIcon name="settings" /> 학교 설정 · 백업</button><button className="help-button" onClick={() => openDialog({ kind: "help" })}><span aria-hidden="true">?</span> 사용설명서</button><button className="support-button" onClick={() => openDialog({ kind: "support" })}><span aria-hidden="true">♡</span> 개발자 후원</button><details className="cover-attribution"><summary>표지 정보 제공처</summary><a href="https://www.aladin.co.kr/" target="_blank" rel="noopener noreferrer">알라딘</a> · <a href="https://openlibrary.org/" target="_blank" rel="noopener noreferrer">Open Library</a> · <a href="https://books.google.com/" target="_blank" rel="noopener noreferrer">Google Books</a></details><span className="sidebar-version">수서로 v{bootstrap?.version || "2.2.3"}</span></div>
      </div>
    </aside>
    {menuOpen && <button type="button" className="sidebar-backdrop" aria-label="메뉴 바깥 영역" tabIndex={-1} onClick={() => setMenuOpen(false)} />}
    <main className={`main-content view-${view}`} id="book-list" inert={menuOpen}><header className="page-header workspace-header"><button ref={menuButton} type="button" className="icon-button menu-toggle" aria-label="메뉴 열기" aria-expanded={menuOpen} onClick={() => setMenuOpen(true)}>☰</button><div><h1>{view === "purchase" ? detail?.list.name || (loading ? "구입 목록을 여는 중…" : "나의 도서 구입 목록") : view === "holdings" ? "학교 소장목록" : "추천도서 목록"}</h1><p className="page-subtitle">{view === "purchase" ? "구입할 책을 고르고 필요한 정보를 확인하세요." : view === "holdings" ? "등록된 학교 소장자료를 조회합니다." : `${detail?.list.name ?? "현재 구입 목록"}에 저장된 추천 자료를 조회합니다.`}</p></div>{view !== "purchase" && <button className="button secondary" onClick={() => setView("purchase")}>구입 목록으로 돌아가기</button>}</header>
      <InlineError message={display.saveError} /><InlineError message={error} />{error && !detail && <button className="button secondary" onClick={() => { setLoading(true); setError(""); if (!listId) { void initialize(); return; } void refresh(listId).then(() => setLoading(false)).catch((caught: unknown) => { setError(errorMessage(caught)); setLoading(false); }); }}>다시 연결</button>}
      {(update?.phase === "downloading" || update?.phase === "ready") && <div className="update-banner" role="status"><div><strong>{update.phase === "downloading" ? "새 버전을 내려받고 있습니다." : `새 버전 ${update.latest_version ?? ""}이 준비되었습니다.`}</strong><p>{update.phase === "downloading" ? "수서 작업을 계속하셔도 됩니다." : automaticOnExit ? "수서로를 닫으면 새 버전이 자동으로 설치됩니다." : "원할 때 업데이트 설정에서 설치할 수 있습니다."}</p></div><button className="text-button" onClick={() => openDialog({ kind: "settings" })}>업데이트 설정</button></div>}
      {notice && <div className="notice" role="status"><span>✓ {notice}</span><button className="icon-button" aria-label="알림 닫기" onClick={() => setNotice("")}>×</button></div>}
      {deleted && <div className="notice undo-notice" role="status"><span>‘{deleted.book.title || "서명 미확인 자료"}’을 삭제했습니다.</span><button className="text-button" onClick={() => { void undoDelete(); }}>삭제 취소</button><button className="icon-button" aria-label="삭제 알림 닫기" onClick={() => setDeleted(null)}>×</button></div>}
      {view === "purchase" && detail && summary && <>
        <section className="collection-panel" aria-label="도서 구입 후보"><div className="collection-header"><h2>도서 목록 <span>{books.length}</span></h2><p>표에서 제목을 누르면 책 정보를 자세히 수정할 수 있습니다.</p>{!!books.length && <BulkReviewToolbar bulk={bulk} visible={visible} pageBooks={pageBooks} />}</div>
          <div className="list-toolbar"><div className="filter-tabs" role="group" aria-label="도서 상태 필터">{([{ id: "all", title: "전체", count: books.length }, { id: "selected", title: "구입 선택", count: selectedCount }, { id: "hold", title: "보류", count: holdCount }, { id: "held", title: "소장 중복", count: books.filter(book => book.held).length }, { id: "review", title: "확인 필요", count: reviewCount }, { id: "no-isbn", title: "ISBN 없음", count: books.filter(book => !book.isbn.trim()).length }] as const).map(item => <button key={item.id} aria-pressed={tab === item.id} className={tab === item.id ? "active" : ""} disabled={bulk.busy} onClick={() => { setTab(item.id); setPage(0); }}>{item.title}<span>{item.count}</span></button>)}</div><label className="search-box"><svg aria-hidden="true" viewBox="0 0 20 20"><circle cx="8.5" cy="8.5" r="5.5" /><path d="m13 13 4 4" /></svg><input ref={searchInput} aria-label="도서 검색" placeholder="제목, 저자, ISBN 검색 · Ctrl+F" value={search} disabled={bulk.busy} onChange={e => { setSearch(e.target.value); setPage(0); }} />{search && <button aria-label="검색어 지우기" className="icon-button" disabled={bulk.busy} onClick={() => { setSearch(""); setPage(0); }}>×</button>}</label></div>
          {!books.length ? <div className="empty-state"><div className="empty-art"><CommandIcon name="book" /></div><h3>도서를 추가해 목록을 시작하세요</h3><p>책 뒷면의 ISBN을 입력하거나<br />추천도서 파일을 가져올 수 있습니다.</p><button className="text-button" onClick={() => openDialog({ kind: "book" })}>책 정보를 직접 입력할게요 <span aria-hidden="true">→</span></button></div> : !visible.length ? <div className="empty-state compact"><h3>조건에 맞는 도서가 없습니다.</h3><p>검색어나 선택한 필터를 바꿔 보세요.</p><button className="text-button" onClick={() => { setSearch(""); setTab("all"); }}>전체 목록 보기</button></div> : <div className="book-table-wrap"><table className="book-table" aria-label="도서 목록"><thead><tr><th scope="col" className="batch-col">작업</th><th scope="col" className="select-col">구입</th><th scope="col">책 정보</th><th scope="col" className="source-col">출처 · 확인</th><th scope="col" className="number-col">정가</th><th scope="col" className="quantity-col">수량</th><th scope="col" className="number-col">선택 금액</th><th scope="col" className="edit-col"><span className="sr-only">수정</span></th></tr></thead><tbody>{pageBooks.map(book => <tr key={book.id} className={[!book.selected ? "on-hold" : "", book.held ? "held-row" : ""].filter(Boolean).join(" ")}><td className="batch-col"><input type="checkbox" aria-label={`${book.title || "서명 미확인"} 일괄 작업 선택`} checked={bulk.checked.has(book.id)} disabled={bulk.busy} onChange={() => bulk.toggle(book.id)} /></td><td className="select-col"><input type="checkbox" aria-label={`${book.title || "서명 미확인"} 구입 선택`} checked={book.selected} disabled={!!pendingBook || bulk.busy} onChange={() => { void toggleBook(book); }} /></td><td className="book-info"><div className="book-cell-layout"><BookCover isbn={book.isbn} title={book.title} /><div className="book-info-text"><button className="book-title" title={book.title} onClick={() => openDialog({ kind: "book", book })}>{book.title || "서명 미확인 자료"}</button><HoldingsStatus book={book} /><span>{[book.author, book.publisher].filter(Boolean).join(" · ") || "저자·출판사 미입력"}</span><small className="isbn">{book.isbn || "ISBN 미입력"}{book.category ? ` · ${book.category}` : ""}</small><BookLinks isbn={book.isbn} title={book.title} link={book.link} />{book.requester && <small className="requester">요청: {book.requester}</small>}</div></div></td><td className="source-col"><div className="book-badges">{book.source && <span className="badge source" title={book.source}>{book.source}</span>}{(book.recommendation_count ?? 0) > 1 && <span className="badge">추천 {book.recommendation_count}회 · 1종으로 통합</span>}{book.duplicate && <span className="badge amber">중복 추천</span>}{book.needs_review && <span className="badge amber">확인 필요</span>}{!book.selected && <span className="badge neutral">보류</span>}</div></td><td className="number-col">{book.price === null ? <span className="unknown-price">가격 미확인</span> : money(book.price)}</td><td className="quantity-col">{book.quantity}</td><td className="number-col order-value">{!book.selected ? <span className="muted">—</span> : book.price === null ? <span className="muted">미정</span> : money(orderAmount(book.price, detail.list.discount_percent, book.quantity))}</td><td className="edit-col"><button className="row-edit" aria-label={`${book.title || "서명 미확인"} 수정`} onClick={() => openDialog({ kind: "book", book })}>수정</button></td></tr>)}</tbody></table></div>}
          {pageCount > 1 && <nav className="list-pagination" aria-label="도서 목록 페이지"><button className="button secondary small" disabled={currentPage === 0 || bulk.busy} onClick={() => setPage(currentPage - 1)}>이전 100건</button><span>{currentPage + 1} / {pageCount}쪽 · {visible.length}건 중 {currentPage * 100 + 1}–{Math.min((currentPage + 1) * 100, visible.length)}건</span><button className="button secondary small" disabled={currentPage + 1 >= pageCount || bulk.busy} onClick={() => setPage(currentPage + 1)}>다음 100건</button></nav>}
          <footer className="collection-footer"><span>총 {books.length}종{query || tab !== "all" ? ` 중 ${visible.length}종 표시` : ""}<span className="footer-divider">·</span>선택 {selectedCount}종</span><button className="text-button" onClick={() => openDialog({ kind: "book" })}>＋ 직접 추가</button></footer>
        </section>
      </>}
      {view === "holdings" && <HoldingsLibraryView api={api} onImport={() => openDialog({ kind: "import", importKind: "holdings" })} refreshKey={holdingsRevision} />}
      {view === "recommendations" && detail && <RecommendationsLibraryView books={books} listName={detail.list.name} />}
      {view === "recommendations" && loading && <div className="loading-state" role="status"><span className="loading-dot" />추천도서 목록을 불러오고 있습니다.</div>}
      {view === "purchase" && loading && <div className="loading-state" role="status"><span className="loading-dot" />도서 목록을 불러오고 있습니다.</div>}
    </main>
    {dialog?.kind === "help" && <HelpDialog onClose={closeDialog} />}
    {dialog?.kind === "support" && <SupportDialog onClose={closeDialog} />}
    {dialog?.kind === "settings" && bootstrap && <SettingsDialog onExited={() => setExited("closed")} onUpdateStarted={() => setExited("updating")} update={bootstrap.update} onUpdate={receiveUpdate} settings={bootstrap.settings} version={bootstrap.version} api={api} onClose={closeDialog} onSettings={settings => setBootstrap(previous => previous ? { ...previous, settings } : previous)} onRestored={async () => { setDetail(null); setListId(""); await initialize(); setHoldingsRevision(value => value + 1); setView("purchase"); setNotice("백업 자료를 복원했습니다."); }} />}
    {dialog?.kind === "new-list" && <ListDialog api={api} onClose={closeDialog} onSaved={listSaved} />}
    {dialog?.kind === "edit-list" && detail && <ListDialog list={detail.list} api={api} onClose={closeDialog} onSaved={listSaved} />}
    {dialog?.kind === "book" && detail && <BookDialog book={dialog.book} api={api} listId={listId} onClose={closeDialog} onSaved={async () => { await refresh(); setNotice(dialog.book ? "책 정보를 저장했습니다." : "새 책을 목록에 추가했습니다."); }} onDeleted={async book => { setDeleted({ listId, book }); await refresh(); }} />}
    {dialog?.kind === "isbn" && detail && <IsbnDialog api={api} keyConfigured={bootstrap?.settings.nl_api_key_configured} onSettings={settings => setBootstrap(previous => previous ? { ...previous, settings } : previous)} listId={listId} onClose={closeDialog} onAdded={refresh} onManual={() => openDialog({ kind: "book" })} />}
    {dialog?.kind === "import" && detail && <ImportDialog api={api} initialKind={dialog.importKind} listId={listId} onClose={closeDialog} onImported={async (message, operationId) => { if (operationId) bulk.remember(operationId, "추천도서 목록 가져오기"); await refresh(); setHoldingsRevision(value => value + 1); setNotice(message); }} />}
    {dialog?.kind === "export" && detail && <ExportDialog api={api} detail={detail} school={school} onClose={closeDialog} />}
  </div>;
}
