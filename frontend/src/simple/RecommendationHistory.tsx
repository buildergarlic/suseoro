import { useState } from "react";
import type { Book } from "./types";

const labels: Record<string, string> = { title: "제목", author: "저자", publisher: "출판사", isbn: "ISBN", price: "정가", quantity: "수량", published_date: "발행일", note: "메모", warnings: "확인 사유" };

export function RecommendationHistory({ book }: { book: Book }) {
  const [page, setPage] = useState(0);
  const contributions = book.contributions ?? [];
  if (!contributions.length) return null;
  const pageCount = Math.max(1, Math.ceil(contributions.length / 20));
  return <details className="recommendation-history soft-note">
    <summary>추천 출처와 원문 {book.recommendation_count ?? contributions.length}건</summary>
    <p className="field-help">통합 전 자료를 보존합니다. 추천 횟수는 구입 수량에 더하지 않습니다.</p>
    {contributions.slice(page * 20, (page + 1) * 20).map((item, index) => <div key={page * 20 + index} className="source-location">
      <strong>{item.source || item.filename || "직접 입력"}</strong>
      {item.raw_text ? <pre>{item.raw_text}</pre> : item.values ? <pre>{Object.entries(item.values).filter(([key, value]) => key in labels && value !== "" && value !== null).map(([key, value]) => `${labels[key]}: ${Array.isArray(value) ? value.filter((item): item is string => typeof item === "string").join(" · ") : typeof value === "string" ? value : typeof value === "number" || typeof value === "boolean" ? String(value) : "원문 확인"}`).join("\n")}</pre> : null}
    </div>)}
    {pageCount > 1 && <div className="bulk-review-controls"><button type="button" className="text-button" disabled={!page} onClick={() => setPage(page - 1)}>이전 출처</button><span>{page + 1} / {pageCount}</span><button type="button" className="text-button" disabled={page + 1 >= pageCount} onClick={() => setPage(page + 1)}>다음 출처</button></div>}
  </details>;
}
