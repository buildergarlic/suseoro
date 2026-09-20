import type { Book } from "./types";

export function HoldingsStatus({ book }: { book: Book }) {
  const status = book.held ? "held" : book.holdings_status ?? "unchecked";
  const label = { held: "소장 중 · 중복", not_held: "소장목록에 없음", unchecked: "소장 확인 전", uncheckable: "소장 대조 불가" }[status];
  return <div className={`holdings-status holdings-${status}`}>
    <span className="holdings-badge"><span aria-hidden="true">{status === "held" ? "●" : status === "not_held" ? "✓" : "?"}</span> {label}</span>
    {status === "held" && book.held_match && <small>{book.held_match === "isbn" ? "ISBN 일치" : "제목·저자 일치"}</small>}
    {status === "uncheckable" && <small>ISBN 또는 제목·저자 필요</small>}
  </div>;
}
