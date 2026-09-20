import { useCallback, useEffect, useRef, useState } from "react";
import { createLibraryApi, type LibraryApi } from "./api";
import { BookDialog, IsbnDialog } from "./BookDialog";
import { ExportDialog } from "./ExportDialog";
import { ImportDialog } from "./ImportDialog";
import { HelpDialog } from "./HelpDialog";
import { HoldingsStatus } from "./HoldingsStatus";
import { BookCover } from "./BookCover";
import { BookLinks } from "./BookLinks";
import { CommandIcon } from "./CommandIcon";
import { DisplaySettings } from "./DisplaySettings";
import { useDisplaySettings } from "./useDisplaySettings";
import { SupportDialog } from "./SupportDialog";
import { InlineError } from "./Modal";
import { ListDialog, SettingsDialog } from "./SettingsDialog";
import { errorMessage, money, orderAmount, type AcquisitionList, type Book, type Bootstrap, type ListDetail, type UpdateInfo } from "./types";

type Dialog = { kind: "import"; importKind?: "recommendations" | "holdings" } | { kind: "isbn" | "export" | "settings" | "help" | "support" | "new-list" | "edit-list" } | { kind: "book"; book?: Book };
type Tab = "all" | "selected" | "hold" | "review" | "held";
const defaultApi = createLibraryApi();
const needsAttention = (book: Book) => book.needs_review || book.price === null || !book.title.trim();

export function SimpleLibraryApp({ api = defaultApi }: { api?: LibraryApi }) {
  const display = useDisplaySettings(api);
  const hydrateDisplay = display.hydrate;
  const searchInput = useRef<HTMLInputElement>(null);
  const [bootstrap, setBootstrap] = useState<Bootstrap | null>(null), [listId, setListId] = useState("");
  const [detail, setDetail] = useState<ListDetail | null>(null), [dialog, setDialog] = useState<Dialog | null>(null);
  const [error, setError] = useState(""), [notice, setNotice] = useState(""), [loading, setLoading] = useState(true), [pendingBook, setPendingBook] = useState("");
  const [search, setSearch] = useState(""), [tab, setTab] = useState<Tab>("all");
  const [exited, setExited] = useState<"closed" | "updating" | null>(null);
  const [deleted, setDeleted] = useState<{ listId: string; book: Book } | null>(null);
  const requestNumber = useRef(0);
  const updateRevision = useRef(0);
  const closeDialog = useCallback(() => setDialog(null), []);
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
    try { const result = await api.bootstrap(); hydrateDisplay(result.settings); setBootstrap(result); setListId(result.lists[0]?.id ?? ""); if (!result.lists.length) { setDetail(null); setLoading(false); } }
    catch (caught) { setError(errorMessage(caught)); setLoading(false); }
  }, [api, hydrateDisplay]);
  useEffect(() => {
    let active = true;
    void api.bootstrap().then(result => { if (active) { hydrateDisplay(result.settings); setBootstrap(result); setListId(result.lists[0]?.id ?? ""); if (!result.lists.length) setLoading(false); } }).catch((caught: unknown) => { if (active) { setError(errorMessage(caught)); setLoading(false); } });
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
    if (!id) return;
    const sequence = ++requestNumber.current;
    const result = await api.list(id);
    if (sequence === requestNumber.current) { setDetail(result); setBootstrap(previous => previous ? { ...previous, lists: previous.lists.map(list => list.id === id ? result.list : list) } : previous); }
  }, [api, listId]);
  useEffect(() => {
    if (!listId) return;
    let active = true;
    void refresh(listId).then(() => { if (active) { setLoading(false); setError(""); } }).catch((caught: unknown) => { if (active) { setError(errorMessage(caught)); setLoading(false); } });
    return () => { active = false; };
  }, [listId, refresh]);
  function selectList(id: string) { if (id === listId) return; setListId(id); setDetail(null); setLoading(true); setSearch(""); setTab("all"); setNotice(""); setError(""); }
  async function toggleBook(book: Book) { setPendingBook(book.id); setError(""); try { await api.updateBook(listId, book.id, { selected: !book.selected }); await refresh(); } catch (caught) { setError(errorMessage(caught)); } finally { setPendingBook(""); } }
  async function undoDelete() { if (!deleted) return; setError(""); try { await api.restoreBook(deleted.listId, deleted.book.id); if (listId === deleted.listId) await refresh(); setDeleted(null); setNotice("삭제한 책을 되돌렸습니다."); } catch (caught) { setError(errorMessage(caught)); } }
  async function listSaved(list: AcquisitionList) { setBootstrap(previous => previous ? { ...previous, lists: [...previous.lists.filter(item => item.id !== list.id), list] } : previous); if (listId === list.id) await refresh(list.id); else selectList(list.id); setNotice("목록 설정을 저장했습니다."); }
  const books = detail?.books ?? [], summary = detail?.summary;
  const selectedCount = books.filter(book => book.selected).length, holdCount = books.length - selectedCount, reviewCount = books.filter(needsAttention).length;
  const query = search.trim().toLocaleLowerCase("ko-KR");
  const visible = books.filter(book => (tab === "all" || (tab === "selected" && book.selected) || (tab === "hold" && !book.selected) || (tab === "held" && book.held) || (tab === "review" && needsAttention(book))) && (!query || [book.title, book.author, book.isbn, book.publisher, book.source, book.requester, book.category, book.note].some(value => value.toLocaleLowerCase("ko-KR").includes(query))));
  const years = [...new Set(bootstrap?.lists.map(list => list.year) ?? [])].sort((a, b) => b - a);
  const percent = detail && summary && detail.list.budget > 0 ? (summary.order_total / detail.list.budget) * 100 : 0;
  const school = bootstrap?.settings.school_name || "우리 학교";
  const update = bootstrap?.update;
  const automaticOnExit = update?.auto_supported === true && update.auto_enabled === true;
  if (exited) return <main className="simple-app"><div className="empty-state"><h1>{exited === "updating" ? "업데이트 설치를 시작했습니다." : "수서로를 종료했습니다."}</h1><p>{exited === "updating" ? "설치 창의 안내를 따라 주세요. 이 브라우저 탭은 닫아도 됩니다." : "자료를 안전하게 보관했습니다. 이 브라우저 탭을 닫아 주세요."}</p></div></main>;
  return <div className={`simple-app density-${display.density}`}>
    <a href="#book-list" className="skip-link">도서 목록으로 바로가기</a>
    <aside className="sidebar"><div className="brand"><CommandIcon name="book" /><div><strong>수서로</strong><small>학교도서관 수서 관리</small></div></div><div className="sidebar-section-heading"><span>내 구입 목록</span><button className="icon-button" aria-label="새 구입 목록" onClick={() => setDialog({ kind: "new-list" })} disabled={!bootstrap}>＋</button></div><nav aria-label="구입 목록">{years.map(year => <div className="year-group" key={year}><span className="year-label">{year}</span>{bootstrap?.lists.filter(list => list.year === year).map(list => <button key={list.id} className={`list-nav ${list.id === listId ? "active" : ""}`} aria-current={list.id === listId ? "page" : undefined} onClick={() => selectList(list.id)}><CommandIcon name="list" /><span>{list.name}</span>{list.id === listId && <span className="active-dot" />}</button>)}</div>)}</nav><button className="new-list-link" onClick={() => setDialog({ kind: "new-list" })} disabled={!bootstrap}><span aria-hidden="true">＋</span> 새 목록 만들기</button><div className="sidebar-bottom"><div className="local-note"><span className="status-dot" /><span>내 컴퓨터에 자동 저장<small>변경 내용 자동 저장</small></span></div><button className="settings-button" onClick={() => setDialog({ kind: "settings" })} disabled={!bootstrap}><CommandIcon name="settings" /> 학교 설정 · 백업</button><button className="help-button" onClick={() => setDialog({ kind: "help" })}><span aria-hidden="true">?</span> 사용설명서</button><button className="support-button" onClick={() => setDialog({ kind: "support" })}><span aria-hidden="true">♡</span> 개발자 후원</button><span className="sidebar-version">수서로 v{bootstrap?.version || "2.1.1"}</span></div></aside>
    <main className="main-content" id="book-list"><header className="page-header workspace-header"><div><p className="eyebrow school-label"><span aria-hidden="true">⌂</span> {school} <span className="separator">/</span> 학교도서관</p><h1>{detail?.list.name || (loading ? "구입 목록을 여는 중…" : "나의 도서 구입 목록")}</h1><p className="page-subtitle">도서 검토, 예산 확인, 발주서 작성을 한곳에서</p></div><div><DisplaySettings settings={display} disabled={!bootstrap} /><button className="button secondary header-setting" onClick={() => setDialog({ kind: "edit-list" })} disabled={!detail}>목록 · 예산 설정</button></div></header>
      <InlineError message={display.saveError} /><InlineError message={error} />{error && !detail && <button className="button secondary" onClick={() => { setLoading(true); setError(""); setListId(""); void initialize(); }}>다시 연결</button>}
      {(update?.phase === "downloading" || update?.phase === "ready") && <div className="update-banner" role="status"><div><strong>{update.phase === "downloading" ? "새 버전을 내려받고 있습니다." : `새 버전 ${update.latest_version ?? ""}이 준비되었습니다.`}</strong><p>{update.phase === "downloading" ? "수서 작업을 계속하셔도 됩니다." : automaticOnExit ? "수서로를 닫으면 새 버전이 자동으로 설치됩니다." : "원할 때 업데이트 설정에서 설치할 수 있습니다."}</p></div><button className="text-button" onClick={() => setDialog({ kind: "settings" })}>업데이트 설정</button></div>}
      {notice && <div className="notice" role="status"><span>✓ {notice}</span><button className="icon-button" aria-label="알림 닫기" onClick={() => setNotice("")}>×</button></div>}
      {deleted && <div className="notice undo-notice" role="status"><span>‘{deleted.book.title || "서명 미확인 자료"}’을 삭제했습니다.</span><button className="text-button" onClick={() => { void undoDelete(); }}>삭제 취소</button><button className="icon-button" aria-label="삭제 알림 닫기" onClick={() => setDeleted(null)}>×</button></div>}
      {detail && summary && <>
        <section className="budget-panel" aria-label="예산 현황"><div className="budget-cards"><div className="budget-card"><div className="budget-label">전체 예산 <span className="budget-symbol" aria-hidden="true">₩</span></div><strong>{money(detail.list.budget)}</strong><small>{detail.list.year}년 · 할인율 {detail.list.discount_percent}%</small></div><div className="budget-card"><div className="budget-label">구입 선택 금액 <span className="budget-symbol" aria-hidden="true">✓</span></div><strong>{money(summary.order_total)}</strong><small>{selectedCount}종 · {summary.total_quantity}권 선택</small></div><div className={`budget-card remaining ${summary.remaining < 0 ? "over-budget" : ""}`}><div className="budget-label">{summary.remaining < 0 ? "예산 초과 금액" : "남은 예산"}<span className="budget-symbol" aria-hidden="true">↗</span></div><strong>{money(Math.abs(summary.remaining))}</strong><small>{summary.remaining < 0 ? "선택한 책이나 예산을 조정해 주세요" : "할인 적용 후 잔액"}</small></div></div><div className="budget-progress"><div className="progress-track" role="progressbar" aria-label="예산 사용률" aria-valuenow={Math.min(100, Math.round(percent))} aria-valuemin={0} aria-valuemax={100} aria-valuetext={`예산의 ${percent.toFixed(1)}% 선택`}><div className={summary.remaining < 0 ? "over" : ""} style={{ width: `${Math.min(100, Math.max(0, percent))}%` }} /></div><span>{percent.toFixed(1)}% 선택</span></div>{summary.missing_price_count > 0 && <p className="budget-warning">가격 미확인 {summary.missing_price_count}종은 선택 금액에 포함되지 않았어요.</p>}</section>
        <section className="collection-panel" aria-label="도서 구입 후보"><div className="collection-header"><div><h2>도서 목록 <span>{books.length}</span></h2><p>추천받은 책을 모으고, 구입할 책에 체크하세요.</p></div><div className="primary-actions"><button className="button primary" onClick={() => setDialog({ kind: "isbn" })}><CommandIcon name="add" /> ISBN 추가</button><button className="button secondary" onClick={() => setDialog({ kind: "import" })}><CommandIcon name="upload" /> 파일 가져오기</button><button className="button secondary" onClick={() => setDialog({ kind: "export" })}><CommandIcon name="download" /> 발주서 저장</button></div></div>
          <div className="holdings-summary"><span>{detail.holdings_count ? `우리 도서관 소장자료 ${detail.holdings_count.toLocaleString("ko-KR")}권과 대조 · 소장 중복 ${books.filter(book => book.held).length}종` : "소장목록을 가져오면 우리 도서관의 기존 책과 비교할 수 있습니다."}</span><button className="text-button" onClick={() => setDialog({ kind: "import", importKind: "holdings" })}>{detail.holdings_count ? "소장목록 갱신" : "소장목록 가져오기"}</button></div><div className="list-toolbar"><div className="filter-tabs" role="group" aria-label="도서 상태 필터">{([{ id: "all", title: "전체", count: books.length }, { id: "selected", title: "구입 선택", count: selectedCount }, { id: "hold", title: "보류", count: holdCount }, { id: "held", title: "소장 중복", count: books.filter(book => book.held).length }, { id: "review", title: "확인 필요", count: reviewCount }] as const).map(item => <button key={item.id} aria-pressed={tab === item.id} className={tab === item.id ? "active" : ""} onClick={() => setTab(item.id)}>{item.title}<span>{item.count}</span></button>)}</div><label className="search-box"><svg aria-hidden="true" viewBox="0 0 20 20"><circle cx="8.5" cy="8.5" r="5.5" /><path d="m13 13 4 4" /></svg><input ref={searchInput} aria-label="도서 검색" placeholder="제목, 저자, ISBN 검색 · Ctrl+F" value={search} onChange={e => setSearch(e.target.value)} />{search && <button aria-label="검색어 지우기" className="icon-button" onClick={() => setSearch("")}>×</button>}</label></div>
          {!books.length ? <div className="empty-state"><div className="empty-art"><CommandIcon name="book" /></div><h3>도서를 추가해 목록을 시작하세요</h3><p>책 뒷면의 ISBN을 입력하거나<br />추천도서 파일을 가져올 수 있습니다.</p><button className="text-button" onClick={() => setDialog({ kind: "book" })}>책 정보를 직접 입력할게요 <span aria-hidden="true">→</span></button></div> : !visible.length ? <div className="empty-state compact"><h3>조건에 맞는 도서가 없습니다.</h3><p>검색어나 선택한 필터를 바꿔 보세요.</p><button className="text-button" onClick={() => { setSearch(""); setTab("all"); }}>전체 목록 보기</button></div> : <div className="book-table-wrap"><table className="book-table"><thead><tr><th scope="col" className="select-col">구입</th><th scope="col">책 정보</th><th scope="col" className="source-col">출처 · 확인</th><th scope="col" className="number-col">정가</th><th scope="col" className="quantity-col">수량</th><th scope="col" className="number-col">선택 금액</th><th scope="col" className="edit-col"><span className="sr-only">수정</span></th></tr></thead><tbody>{visible.map(book => <tr key={book.id} className={[!book.selected ? "on-hold" : "", book.held ? "held-row" : ""].filter(Boolean).join(" ")}><td className="select-col"><input type="checkbox" aria-label={`${book.title || "서명 미확인"} 구입 선택`} checked={book.selected} disabled={!!pendingBook} onChange={() => { void toggleBook(book); }} /></td><td className="book-info"><div className="book-cell-layout"><BookCover isbn={book.isbn} title={book.title} /><div className="book-info-text"><button className="book-title" onClick={() => setDialog({ kind: "book", book })}>{book.title || "서명 미확인 자료"}</button><HoldingsStatus book={book} /><span>{[book.author, book.publisher].filter(Boolean).join(" · ") || "저자·출판사 미입력"}</span><small className="isbn">{book.isbn || "ISBN 미입력"}{book.category ? ` · ${book.category}` : ""}</small><BookLinks isbn={book.isbn} title={book.title} link={book.link} />{book.requester && <small className="requester">요청: {book.requester}</small>}</div></div></td><td className="source-col"><div className="book-badges">{book.source && <span className="badge source">{book.source}</span>}{book.duplicate && <span className="badge amber">중복 추천</span>}{book.needs_review && <span className="badge amber">확인 필요</span>}{!book.selected && <span className="badge neutral">보류</span>}</div></td><td className="number-col">{book.price === null ? <span className="unknown-price">가격 미확인</span> : money(book.price)}</td><td className="quantity-col">{book.quantity}</td><td className="number-col order-value">{!book.selected ? <span className="muted">—</span> : book.price === null ? <span className="muted">미정</span> : money(orderAmount(book.price, detail.list.discount_percent, book.quantity))}</td><td className="edit-col"><button className="row-edit" aria-label={`${book.title || "서명 미확인"} 수정`} onClick={() => setDialog({ kind: "book", book })}>수정</button></td></tr>)}</tbody></table></div>}
          <footer className="collection-footer"><span>총 {books.length}종{query || tab !== "all" ? ` 중 ${visible.length}종 표시` : ""}<span className="footer-divider">·</span>선택 {selectedCount}종</span><button className="text-button" onClick={() => setDialog({ kind: "book" })}>＋ 직접 추가</button></footer>
        </section><p className="page-footnote">변경 내용은 자동 저장됩니다. 구입 선택한 도서만 발주서에 포함됩니다. <span className="cover-attribution">표지: <a href="https://www.aladin.co.kr/" target="_blank" rel="noopener noreferrer">알라딘</a> 우선 · <a href="https://openlibrary.org/" target="_blank" rel="noopener noreferrer">Open Library</a> · <a href="https://books.google.com/" target="_blank" rel="noopener noreferrer">Google Books</a></span></p>
      </>}
      {loading && <div className="loading-state" role="status"><span className="loading-dot" />도서 목록을 불러오고 있습니다.</div>}
    </main>
    {dialog?.kind === "help" && <HelpDialog onClose={closeDialog} />}
    {dialog?.kind === "support" && <SupportDialog onClose={closeDialog} />}
    {dialog?.kind === "settings" && bootstrap && <SettingsDialog onExited={() => setExited("closed")} onUpdateStarted={() => setExited("updating")} update={bootstrap.update} onUpdate={receiveUpdate} settings={bootstrap.settings} version={bootstrap.version} api={api} onClose={closeDialog} onSettings={settings => setBootstrap(previous => previous ? { ...previous, settings } : previous)} onRestored={async () => { setDetail(null); setListId(""); await initialize(); setNotice("백업 자료를 복원했습니다."); }} />}
    {dialog?.kind === "new-list" && <ListDialog api={api} onClose={closeDialog} onSaved={listSaved} />}
    {dialog?.kind === "edit-list" && detail && <ListDialog list={detail.list} api={api} onClose={closeDialog} onSaved={listSaved} />}
    {dialog?.kind === "book" && detail && <BookDialog book={dialog.book} api={api} listId={listId} onClose={closeDialog} onSaved={async () => { await refresh(); setNotice(dialog.book ? "책 정보를 저장했습니다." : "새 책을 목록에 추가했습니다."); }} onDeleted={async book => { setDeleted({ listId, book }); await refresh(); }} />}
    {dialog?.kind === "isbn" && detail && <IsbnDialog api={api} keyConfigured={bootstrap?.settings.nl_api_key_configured} onSettings={settings => setBootstrap(previous => previous ? { ...previous, settings } : previous)} listId={listId} onClose={closeDialog} onAdded={refresh} onManual={() => setDialog({ kind: "book" })} />}
    {dialog?.kind === "import" && detail && <ImportDialog api={api} initialKind={dialog.importKind} listId={listId} onClose={closeDialog} onImported={async message => { await refresh(); setNotice(message); }} />}
    {dialog?.kind === "export" && detail && <ExportDialog api={api} detail={detail} school={school} onClose={closeDialog} />}
  </div>;
}
