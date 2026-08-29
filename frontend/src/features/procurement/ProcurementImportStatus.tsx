import {
  procurementImportMessage,
  type ProcurementImport,
} from "./procurementImport";

interface ProcurementImportStatusProps {
  busy: boolean;
  imports: ProcurementImport[];
  kind: "QUOTE" | "DELIVERY";
  onCompose: (item: ProcurementImport) => void;
}

export function ProcurementImportStatus({
  busy,
  imports,
  kind,
  onCompose,
}: ProcurementImportStatusProps) {
  const relevant = imports.filter((item) => item.kind === kind);
  if (!relevant.length) return null;
  return (
    <section aria-label={kind === "QUOTE" ? "견적 파일 처리 내역" : "납품명세서 처리 내역"} className="import-status-list">
      {relevant.map((item) => (
        <article className={`import-status-card import-${item.status.toLowerCase()}`} key={item.import_id}>
          <div>
            <strong>{item.filename}</strong>
            <p>{procurementImportMessage(item)}</p>
            <p className="field-note">
              {item.detected_format} · {item.processed_rows}/{item.total_rows}행 처리
              {item.row_error_count > 0 ? ` · 오류 ${item.row_error_count}행` : ""}
              {item.count_confidence !== "EXACT" ? " · 행 수 확인 필요" : ""}
            </p>
            {item.mapping_required?.questions.length ? (
              <ul className="import-question-list">
                {item.mapping_required.questions.map((question) => <li key={question}>{question}</li>)}
              </ul>
            ) : null}
            {item.rows.some((row) => row.error) ? (
              <ul className="import-error-list">
                {item.rows.filter((row) => row.error).map((row) => (
                  <li key={row.source_row_id}>
                    {row.provenance.sheet ? `${row.provenance.sheet} · ` : ""}
                    {row.provenance.source_row}행: {row.error?.message}
                  </li>
                ))}
              </ul>
            ) : null}
          </div>
          {item.status === "READY" ? (
            <button className="button button-secondary" disabled={busy} onClick={() => onCompose(item)} type="button">
              서버 처리 결과 반영하기
            </button>
          ) : null}
        </article>
      ))}
    </section>
  );
}
