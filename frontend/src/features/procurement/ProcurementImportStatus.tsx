import { useState } from "react";

import { MappingDialog } from "../ingestion/MappingDialog";
import {
  procurementImportMessage,
  type ProcurementImport,
} from "./procurementImport";

interface ProcurementImportStatusProps {
  busy: boolean;
  imports: ProcurementImport[];
  kind: "QUOTE" | "DELIVERY";
  canMutate: boolean;
  onCompose: (item: ProcurementImport) => void;
  onMap: (
    item: ProcurementImport,
    mapping: Record<string, string>,
    remember: boolean,
  ) => Promise<void>;
}

export function ProcurementImportStatus({
  busy,
  imports,
  kind,
  canMutate,
  onCompose,
  onMap,
}: ProcurementImportStatusProps) {
  const [mappingItem, setMappingItem] = useState<ProcurementImport | null>(null);
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
          {item.status === "MAPPING_REQUIRED" && item.mapping_required && canMutate ? (
            <button className="button button-secondary" disabled={busy} onClick={() => setMappingItem(item)} type="button">
              {item.filename} 열 연결하기
            </button>
          ) : item.status === "READY" && canMutate ? (
            <button className="button button-secondary" disabled={busy} onClick={() => onCompose(item)} type="button">
              서버 처리 결과 반영하기
            </button>
          ) : null}
        </article>
      ))}
      {mappingItem?.mapping_required ? (
        <MappingDialog
          mappingRequired={mappingItem.mapping_required}
          onApply={async (mapping, remember) => {
            await onMap(mappingItem, mapping, remember);
            setMappingItem(null);
          }}
          onClose={() => setMappingItem(null)}
        />
      ) : null}
    </section>
  );
}
