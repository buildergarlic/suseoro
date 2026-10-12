import { useEffect, useRef, useState } from "react";
import type { LibraryApi } from "./api";
import { errorMessage, type HoldingsEnrichment, type HoldingsStatistics } from "./types";
import "./library-dashboard.css";

type DashboardProps = { api: LibraryApi; schoolName: string; refreshKey: number; onImport: () => void };
const colors = ["#729d8c", "#a5a1d1", "#ddb077", "#8daec4", "#d88e9b", "#a7b96d", "#bc9e7a", "#8893c0", "#cfadbb", "#9cbcb4", "#d4d7d1"];
const number = (value: number) => value.toLocaleString("ko-KR");
const percent = (count: number, total: number) => total > 0 ? Math.round(count / total * 100) : 0;

function BookSprout() {
  return <svg className="library-mascot" viewBox="0 0 180 150" aria-hidden="true">
    <ellipse cx="90" cy="137" rx="60" ry="7" fill="#d9e5ce" />
    <path d="M89 43c-5-18-21-21-27-20 1 16 15 24 27 20Z" fill="#8ebba0" />
    <path d="M90 43c3-22 22-26 32-24-2 18-15 27-32 24Z" fill="#a8c68d" />
    <path d="M90 53V37" stroke="#638568" strokeWidth="4" strokeLinecap="round" />
    <path d="M26 59q36-10 64 6 28-16 64-6v62q-34-9-64 5-30-14-64-5Z" fill="#789f88" stroke="#53745f" strokeWidth="2" strokeLinejoin="round" />
    <path d="M33 53q30-9 57 8 27-17 57-8v62q-29-8-57 6-28-14-57-6Z" fill="#fffdf2" stroke="#53745f" strokeWidth="2" strokeLinejoin="round" />
    <path d="M90 62v58M42 66q20-3 34 3M42 75q20-3 34 3M104 68q18-6 33-3M104 77q18-6 33-3" fill="none" stroke="#cdd9c4" strokeWidth="3" strokeLinecap="round" />
    <ellipse cx="62" cy="89" rx="3" ry="4" fill="#435647" /><ellipse cx="118" cy="89" rx="3" ry="4" fill="#435647" />
    <ellipse cx="53" cy="97" rx="7" ry="4" fill="#eec6b7" /><ellipse cx="127" cy="97" rx="7" ry="4" fill="#eec6b7" />
    <path d="M82 99q8 9 16 0" fill="none" stroke="#435647" strokeWidth="2.5" strokeLinecap="round" />
    <path d="M17 76l-5 7 7 2M161 88l7-4 3 8" fill="none" stroke="#e0b26d" strokeWidth="2.5" strokeLinecap="round" />
  </svg>;
}

function Bars({ items, tone, total }: { items: { label: string; count: number }[]; tone: "mint" | "lavender"; total: number }) {
  const maximum = Math.max(1, ...items.map(item => item.count));
  return <ol className={`library-bars ${tone}`}>{items.map(item => <li key={item.label}>
    <div><span>{item.label}</span><strong>{number(item.count)}<small>건</small></strong></div>
    <div className="library-bar-track" aria-hidden="true"><span style={{ width: `${item.count / maximum * 100}%` }} /></div>
    <span className="sr-only">전체 등록 자료의 {percent(item.count, total)}%</span>
  </li>)}</ol>;
}

export function LibraryDashboard({ api, schoolName, refreshKey, onImport }: DashboardProps) {
  const [retryKey, setRetryKey] = useState(0);
  const [result, setResult] = useState<{ key: string; data: HoldingsStatistics } | null>(null);
  const [failure, setFailure] = useState<{ key: string; message: string } | null>(null);
  const [enrichment, setEnrichment] = useState<HoldingsEnrichment | null>(null);
  const [statusError, setStatusError] = useState("");
  const [starting, setStarting] = useState(false);
  const [stopping, setStopping] = useState(false);
  const enrichmentRevision = useRef(0);
  const loadKey = `${refreshKey}/${retryKey}`;
  const stats = result?.data;
  const error = failure?.key === loadKey ? failure.message : "";
  const loading = result?.key !== loadKey && !error;

  useEffect(() => {
    let active = true;
    const revision = enrichmentRevision.current;
    void Promise.allSettled([api.holdingsStatistics(), api.holdingsEnrichment()]).then(([statistics, state]) => {
      if (!active) return;
      if (statistics.status === "fulfilled") { setResult({ key: loadKey, data: statistics.value }); setFailure(null); }
      else setFailure({ key: loadKey, message: errorMessage(statistics.reason) });
      if (revision === enrichmentRevision.current) {
        if (state.status === "fulfilled") { setEnrichment(state.value); setStatusError(""); }
        else setStatusError(errorMessage(state.reason));
      }
    });
    return () => { active = false; };
  }, [api, loadKey]);

  useEffect(() => {
    if (enrichment?.phase !== "running") return;
    let active = true;
    let timer: number;
    async function poll() {
      const revision = enrichmentRevision.current;
      try {
        const state = await api.holdingsEnrichment();
        if (!active) return;
        if (revision === enrichmentRevision.current) {
          setEnrichment(state);
          setStatusError("");
          if (state.phase !== "running") { setRetryKey(value => value + 1); return; }
        }
      } catch (caught) { if (!active) return; if (revision === enrichmentRevision.current) setStatusError(errorMessage(caught)); }
      if (active) timer = window.setTimeout(() => { void poll(); }, 2_000);
    }
    timer = window.setTimeout(() => { void poll(); }, 2_000);
    return () => { active = false; window.clearTimeout(timer); };
  }, [api, enrichment?.phase]);

  async function enrich() {
    enrichmentRevision.current += 1;
    setStarting(true); setStatusError("");
    try { const state = await api.enrichHoldings(); setEnrichment(state); if (state.phase !== "running") setRetryKey(value => value + 1); }
    catch (caught) { setStatusError(errorMessage(caught)); }
    finally { setStarting(false); }
  }

  async function stop() {
    enrichmentRevision.current += 1;
    setStopping(true); setStatusError("");
    try { setEnrichment(await api.stopHoldingsEnrichment()); setRetryKey(value => value + 1); }
    catch (caught) { setStatusError(errorMessage(caught)); }
    finally { setStopping(false); }
  }

  const running = enrichment?.phase === "running";
  const categories = stats?.category_counts.filter(item => item.count > 0) ?? [];
  const categoryTotal = categories.reduce((sum, item) => sum + item.count, 0);
  const circumference = 2 * Math.PI * 65;
  const leading = categories.filter(item => item.key !== "unknown" && item.label !== "미분류").sort((a, b) => b.count - a.count)[0];
  const missingCategory = stats?.coverage.missing_category ?? 0;

  return <section className="library-dashboard" aria-label="우리 학교 도서관 통계" aria-busy={loading}>
    <header className="library-hero">
      <div className="library-hero-copy"><span className="library-eyebrow"><span aria-hidden="true">✦</span> 우리 학교 책숲</span><h2>{schoolName || "우리 학교"}의<br />책 이야기를 만나 보세요</h2><p>소장목록에서 발견한 분야와 소재를<br className="library-wide-break" /> 한눈에 살펴보는 작은 도서관 지도예요.</p></div>
      <BookSprout />
      <div className="library-hero-actions"><button type="button" className="button secondary" onClick={onImport}>소장목록 가져오기</button><button type="button" className="library-refresh" onClick={() => setRetryKey(value => value + 1)} disabled={loading}>새로고침 <span aria-hidden="true">↻</span></button></div>
    </header>
    {error && <div className="library-load-error" role="alert"><div><p>{error}</p>{stats && <p>마지막으로 불러온 통계를 보여 드리고 있어요.</p>}</div><button type="button" className="button secondary" onClick={() => setRetryKey(value => value + 1)}>통계 다시 불러오기</button></div>}
    {loading && <p className="library-loading" role="status">{stats ? "등록 자료를 다시 살펴보고 있어요…" : "우리 학교 책숲을 살펴보고 있어요…"}</p>}
    {!stats && !error && <div className="library-skeleton" aria-hidden="true"><div /><div /><div /><div /></div>}
    {stats && !stats.total_records && <div className="library-empty"><span aria-hidden="true">🌱</span><h3>첫 책부터 책숲을 채워 볼까요?</h3><p>학교 소장목록을 가져오면 분야, 소재, 출판연도 통계를 보여 드려요.<br />분류번호와 ISBN이 있는 목록이면 더 풍성해집니다.</p><button type="button" className="button primary" onClick={onImport}>학교 소장목록 가져오기</button></div>}
    {stats && stats.total_records > 0 && <>
      <div className="library-metrics">
        <article className="library-metric mint"><span className="library-metric-icon" aria-hidden="true">▤</span><span>소장 수량</span><strong>{number(stats.total_copies)}<small>권</small></strong><p>등록된 자료의 수량 합계</p></article>
        <article className="library-metric lavender"><span className="library-metric-icon" aria-hidden="true">▦</span><span>등록 자료</span><strong>{number(stats.total_records)}<small>건</small></strong><p>같은 ISBN도 별도 등록 가능</p></article>
        <article className="library-metric peach"><span className="library-metric-icon" aria-hidden="true">⌁</span><span>서로 다른 ISBN</span><strong>{number(stats.unique_isbns)}<small>종</small></strong><p>유효한 ISBN 기준</p></article>
        <article className="library-metric butter"><span className="library-metric-icon" aria-hidden="true">✧</span><span>분야를 아는 자료</span><strong>{percent(stats.classified_records, stats.total_records)}<small>%</small></strong><p>{number(stats.classified_records)}건의 분류정보 확인</p></article>
      </div>
      <div className="library-primary-grid">
        <article className="library-card library-category-card"><div className="library-card-heading"><div><span className="library-kicker">BOOK MIX</span><h3>어떤 분야가 자라고 있나요?</h3></div><span className="library-sticker" aria-hidden="true">🌷</span></div><p className="library-card-description">분류번호 · ISBN 도서정보의 분야 기준</p>
          {categories.length ? <div className="library-category-content"><div className="library-donut"><svg viewBox="0 0 180 180" role="img" aria-label={`분야별 등록 자료 분포. ${categories.map(item => `${item.label} ${number(item.count)}건`).join(", ")}`}><circle cx="90" cy="90" r="65" fill="none" stroke="#eef0e9" strokeWidth="24" />{categories.map((item, index) => {
            const before = categories.slice(0, index).reduce((sum, entry) => sum + entry.count, 0);
            return <circle key={item.key} cx="90" cy="90" r="65" fill="none" stroke={colors[index % colors.length]} strokeWidth="24" strokeDasharray={`${item.count / categoryTotal * circumference} ${circumference}`} strokeDashoffset={-before / categoryTotal * circumference} transform="rotate(-90 90 90)" />;
          })}</svg><div className="library-donut-label" aria-hidden="true"><small>등록 자료</small><strong>{number(categoryTotal)}</strong><span>건의 책 이야기</span></div></div><ul className="library-category-legend">{categories.map((item, index) => <li key={item.key}><span className="library-legend-dot" style={{ backgroundColor: colors[index % colors.length] }} aria-hidden="true" /><span>{item.label}</span><strong>{number(item.count)}<small>건</small></strong><small>{percent(item.count, stats.total_records)}%</small></li>)}</ul></div> : <div className="library-no-data">아직 분류정보가 없어요.<br />분류번호가 있는 소장목록을 가져오거나 ISBN 도서정보를 보완해 주세요.</div>}
          {leading && <p className="library-insight"><span aria-hidden="true">🍃</span> <strong>{leading.label}</strong> 분야가 {number(leading.count)}건으로 가장 큰 책숲이에요.</p>}
        </article>
        <article className="library-card library-topic-card"><div className="library-card-heading"><div><span className="library-kicker">STORY SEEDS</span><h3>책 속 소재의 씨앗</h3></div><span className="library-sticker" aria-hidden="true">🌰</span></div><p className="library-card-description">제목과 도서 주제어에서 찾은 소재 · 추정치</p><div className="library-topic-garden">{stats.topics.length ? stats.topics.map((topic, index) => <div className={`library-topic topic-${index % 4}`} key={topic.label}><span>{topic.label}</span><strong>{number(topic.count)}<small>건</small></strong></div>) : <p className="library-no-data">아직 발견한 소재가 없어요.<br />ISBN 도서정보를 보완하면 주제어도 함께 살펴볼 수 있어요.</p>}</div><p className="library-method">{stats.topic_method || "책 제목과 제공된 주제어의 키워드로 소재를 추정합니다."} 한 자료가 여러 소재에 포함될 수 있으며, 책 본문을 분석한 결과는 아니에요.</p></article>
      </div>
      <div className="library-secondary-grid">
        <article className="library-card"><div className="library-card-heading"><div><span className="library-kicker">BOOK AGE</span><h3>책이 태어난 해</h3></div><span className="library-sticker" aria-hidden="true">☀</span></div><p className="library-card-description">출판연도별 등록 자료</p>{stats.publication_years.length ? <Bars items={stats.publication_years} tone="mint" total={stats.total_records} /> : <p className="library-no-data">출판연도가 등록된 자료가 없어요.</p>}{stats.coverage.missing_year > 0 && <p className="library-caption">출판연도 미확인 {number(stats.coverage.missing_year)}건은 제외했어요.</p>}</article>
        <article className="library-card"><div className="library-card-heading"><div><span className="library-kicker">BOOK MAKERS</span><h3>우리 책을 만든 출판사</h3></div><span className="library-sticker" aria-hidden="true">✉</span></div><p className="library-card-description">등록 자료가 많은 출판사</p>{stats.top_publishers.length ? <Bars items={stats.top_publishers} tone="lavender" total={stats.total_records} /> : <p className="library-no-data">출판사가 등록된 자료가 없어요.</p>}</article>
      </div>
      <article className="library-quality-card"><div className="library-quality-heading"><span className="library-quality-icon" aria-hidden="true">🌿</span><div><h3>책숲을 더 또렷하게</h3><p>통계는 등록된 자료를 기준으로 해요. 비어 있는 정보부터 채워 주세요.</p></div><div className="library-enrichment-actions"><button type="button" className="button primary" onClick={() => { void enrich(); }} disabled={starting || stopping || running || stats.valid_isbn_records === 0}>{starting ? "시작하는 중…" : running ? "ISBN 정보 보완 중…" : "ISBN 도서정보 보완"}</button>{running && <button type="button" className="button secondary" onClick={() => { void stop(); }} disabled={stopping}>{stopping ? "중지하는 중…" : "보완 중지"}</button>}</div></div>
        <dl className="library-coverage"><div><dt>유효한 ISBN</dt><dd>{number(stats.valid_isbn_records)}<small> / {number(stats.total_records)}건</small></dd></div><div><dt>분류 미확인</dt><dd>{number(missingCategory)}<small>건</small></dd></div><div><dt>출판연도 미확인</dt><dd>{number(stats.coverage.missing_year)}<small>건</small></dd></div><div><dt>조회해 보완한 자료</dt><dd>{number(stats.metadata_records)}<small>건</small></dd></div></dl>
        {(missingCategory > 0 || stats.coverage.missing_isbn > 0) && <p className="library-quality-help">{stats.coverage.missing_isbn > 0 && <>ISBN이 없거나 유효하지 않은 자료 {number(stats.coverage.missing_isbn)}건은 ISBN 조회로 보완할 수 없어요. </>}분류번호(KDC)와 출판연도가 포함된 소장목록을 가져오면 빈 정보를 줄일 수 있어요.</p>}
        <p className="library-quality-help">외부 도서정보 서비스에 ISBN을 조회해요. 학교명이나 소장목록 파일 전체는 보내지 않아요. 잠시 화면을 옮겨도 계속되며, 조회되지 않는 책의 기존 정보는 그대로 두어요.</p>
        <p className="library-quality-help">국내 도서는 학교 설정에 국립중앙도서관 인증키를 등록하면 더 많은 정보를 찾을 수 있어요. 정보 제공 여부는 책과 제공처에 따라 달라요.</p>
        {enrichment && (enrichment.phase !== "idle" || enrichment.message) && <div className={`library-enrichment ${enrichment.phase}`} role={enrichment.phase === "error" ? "alert" : "status"}><p>{enrichment.message || (running ? "ISBN 도서정보를 살펴보고 있어요." : "ISBN 도서정보 보완을 마쳤어요.")}</p><span>{number(enrichment.processed)} / {number(enrichment.total)}종 확인 · {number(enrichment.found)}종 정보 발견</span>{running && <progress aria-label="ISBN 정보 보완 진행률" value={enrichment.processed} max={Math.max(1, enrichment.total)} />}</div>}
        {statusError && <div className="library-status-error" role="alert"><span>{statusError}</span><button type="button" className="library-refresh" onClick={() => setRetryKey(value => value + 1)}>상태 다시 확인</button></div>}
      </article>
      <p className="library-footnote">소장자료의 수량과 등록 건수를 구분해서 표시해요. 분야·소재 통계는 등록 건수 기준이며, 실제 대출 횟수나 이용률은 포함하지 않아요.</p>
    </>}
  </section>;
}
