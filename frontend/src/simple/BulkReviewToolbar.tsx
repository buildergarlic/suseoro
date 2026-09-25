import { InlineError } from "./Modal";
import type { Book } from "./types";
import type { useBulkReview } from "./useBulkReview";
import "./bulk-review.css";

export function BulkReviewToolbar({ bulk, visible, pageBooks }: { bulk: ReturnType<typeof useBulkReview>; visible: Book[]; pageBooks: Book[] }) {
  const blocked = bulk.busy || bulk.refreshNeeded;
  return <details className="bulk-review" onKeyDown={event => {
    if (event.key !== "Escape") return;
    event.currentTarget.open = false;
    event.currentTarget.querySelector("summary")?.focus();
  }}>
    <summary><span>일괄 작업</span><span>{bulk.checked.size}건 선택</span>{bulk.error && <span className="bulk-review-alert">확인 필요</span>}</summary>
    <section className="bulk-review-panel" aria-label="도서 일괄 작업">
    <div className="bulk-review-controls">
      <strong>작업 대상 {bulk.checked.size}건</strong>
      <button className="text-button" disabled={bulk.busy || !pageBooks.length} onClick={() => bulk.setChecked([...bulk.checked, ...pageBooks.map(book => book.id)])}>현재 페이지 선택</button>
      <button className="text-button" disabled={bulk.busy || !visible.length} onClick={() => bulk.setChecked(visible.map(book => book.id))}>검색·필터 결과 {visible.length}건 전체 선택</button>
      <button className="text-button" disabled={bulk.busy || !bulk.checked.size} onClick={() => bulk.setChecked([])}>작업 선택 해제</button>
    </div>
    <div className="bulk-review-controls">
      <button className="button secondary small" aria-label="선택 도서 서지 확인 완료" disabled={blocked || !bulk.checked.size} onClick={() => { void bulk.run("confirm_metadata"); }}>서지 확인 완료</button>
      <button className="button secondary small" aria-label="선택 도서 구입 선택" disabled={blocked || !bulk.checked.size} onClick={() => { void bulk.run("select"); }}>구입 선택</button>
      <button className="button secondary small" aria-label="선택 도서 보류" disabled={blocked || !bulk.checked.size} onClick={() => { void bulk.run("hold"); }}>보류</button>
      {bulk.busy && <span role="status">일괄 작업을 저장하고 있습니다…</span>}
    </div>
    <p className="field-help">원문과 서지를 확인한 책은 ISBN 없이도 확인 완료할 수 있습니다. 가격 누락·잘못된 ISBN·불완전한 서지는 남겨 둡니다. 구입 여부는 별도로 유지됩니다.</p>
    {bulk.result && <div className="bulk-result" role="status">
      <strong>{bulk.result.label} {bulk.result.value.updated}건 완료{bulk.result.value.skipped.length ? ` · ${bulk.result.value.skipped.length}건은 확인 필요` : ""}</strong>
      {!!bulk.result.value.skipped.length && <details><summary>남겨 둔 사유 보기</summary><ul>{bulk.result.value.skipped.slice(0, 20).map(item => <li key={item.id}>{visible.find(book => book.id === item.id)?.title || "도서"}: {item.reason}</li>)}</ul>{bulk.result.value.skipped.length > 20 && <p>나머지 {bulk.result.value.skipped.length - 20}건도 작업 선택을 유지했습니다.</p>}</details>}
    </div>}
    {bulk.operation && <div className="bulk-review-controls"><span>최근 작업: {bulk.operation.label}</span><button className="text-button" aria-label="일괄 작업 되돌리기" disabled={blocked} onClick={() => { void bulk.undo(); }}>되돌리기</button><small>이후 수정한 도서는 덮어쓰지 않습니다.</small></div>}
    <InlineError message={bulk.error} />
    {bulk.refreshNeeded && <button className="button secondary small" disabled={bulk.busy} onClick={() => { void bulk.retryRefresh(); }}>목록 다시 불러오기</button>}
    </section>
  </details>;
}
