import { useMemo, useState } from "react";

import type { components } from "../../api/types";
import { ModalDialog } from "../../components/ModalDialog";

type MappingRequired = components["schemas"]["MappingRequired"];

interface MappingDialogProps {
  mappingRequired: MappingRequired;
  onApply: (mapping: Record<string, string>, remember: boolean) => Promise<void>;
  onClose: () => void;
}

function likelyHeader(
  headers: string[],
  suggested: Record<string, string | null>,
  canonicalField: string,
  choices: string[],
): string {
  return (
    headers.find((header) => suggested[header] === canonicalField) ??
    headers.find((header) => choices.some((choice) => header.includes(choice))) ??
    headers[0] ??
    ""
  );
}

export function MappingDialog({
  mappingRequired,
  onApply,
  onClose,
}: MappingDialogProps) {
  const headers = mappingRequired.headers;
  const [titleColumn, setTitleColumn] = useState(() =>
    likelyHeader(
      headers,
      mappingRequired.suggested_mapping,
      "title",
      ["책이름", "제목", "도서명"],
    ),
  );
  const [authorColumn, setAuthorColumn] = useState(() =>
    likelyHeader(
      headers,
      mappingRequired.suggested_mapping,
      "author",
      ["쓴이", "저자", "지은이"],
    ),
  );
  const [remember, setRemember] = useState(true);
  const [applying, setApplying] = useState(false);
  const mapping = useMemo(() => {
    const next: Record<string, string> = {};
    if (titleColumn) next[titleColumn] = "title";
    if (authorColumn && authorColumn !== titleColumn) next[authorColumn] = "author";
    return next;
  }, [authorColumn, titleColumn]);

  async function apply() {
    setApplying(true);
    try {
      await onApply(mapping, remember);
    } finally {
      setApplying(false);
    }
  }

  return (
    <ModalDialog labelledBy="mapping-dialog-title" onClose={onClose}>
      <div className="dialog-heading">
        <p className="eyebrow">한 번만 확인해 주세요</p>
        <h2 id="mapping-dialog-title">열 연결 확인</h2>
        <p>제목과 저자가 들어 있는 열을 확인하면 다음에도 같은 양식을 알아봅니다.</p>
      </div>

      <div className="mapping-fields">
        <label>
          제목 열
          <select
            onChange={(event) => setTitleColumn(event.target.value)}
            value={titleColumn}
          >
            {headers.map((header) => (
              <option key={header} value={header}>
                {header}
              </option>
            ))}
          </select>
        </label>
        <label>
          저자 열
          <select
            onChange={(event) => setAuthorColumn(event.target.value)}
            value={authorColumn}
          >
            {headers.map((header) => (
              <option key={header} value={header}>
                {header}
              </option>
            ))}
          </select>
        </label>
        <label className="check-field">
          <input
            checked={remember}
            onChange={(event) => setRemember(event.target.checked)}
            type="checkbox"
          />
          이 양식 기억
        </label>
      </div>

      <div className="table-scroll mapping-preview">
        <table aria-label="가져올 자료 미리보기">
          <thead>
            <tr>
              {headers.map((header) => (
                <th key={header} scope="col">
                  {header}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {mappingRequired.preview_rows.slice(0, 20).map((row, rowIndex) => (
              <tr key={rowIndex}>
                {headers.map((header, columnIndex) => (
                  <td key={`${header}-${columnIndex}`}>
                    {row[columnIndex] === null || row[columnIndex] === undefined
                      ? ""
                      : String(row[columnIndex])}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="dialog-actions">
        <button className="button button-quiet" onClick={onClose} type="button">
          돌아가기
        </button>
        <button
          className="button button-primary"
          disabled={applying || !titleColumn || !authorColumn}
          onClick={() => void apply()}
          type="button"
        >
          {applying ? "적용 중…" : "열 연결 적용"}
        </button>
      </div>
    </ModalDialog>
  );
}
