import { useMemo, useState } from "react";

import { ModalDialog } from "../../components/ModalDialog";

interface MappingDialogProps {
  previewText: string;
  onApply: (mapping: Record<string, string>, remember: boolean) => Promise<void>;
  onClose: () => void;
}

function rowsFrom(text: string): string[][] {
  return text
    .split(/\r?\n/u)
    .filter((line) => line.trim().length > 0)
    .slice(0, 21)
    .map((line) => line.split(/[\t,]/u).map((cell) => cell.trim()));
}

function likelyHeader(headers: string[], choices: string[]): string {
  return (
    headers.find((header) => choices.some((choice) => header.includes(choice))) ??
    headers[0] ??
    ""
  );
}

export function MappingDialog({
  previewText,
  onApply,
  onClose,
}: MappingDialogProps) {
  const rows = useMemo(() => rowsFrom(previewText), [previewText]);
  const headers = rows[0] ?? [];
  const [titleColumn, setTitleColumn] = useState(() =>
    likelyHeader(headers, ["책이름", "제목", "도서명"]),
  );
  const [authorColumn, setAuthorColumn] = useState(() =>
    likelyHeader(headers, ["쓴이", "저자", "지은이"]),
  );
  const [remember, setRemember] = useState(true);
  const [applying, setApplying] = useState(false);

  async function apply() {
    setApplying(true);
    try {
      await onApply(
        {
          [titleColumn]: "title",
          [authorColumn]: "author",
        },
        remember,
      );
    } finally {
      setApplying(false);
    }
  }

  return (
    <ModalDialog
      labelledBy="mapping-dialog-title"
      onClose={onClose}
    >
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
            {rows.slice(1, 21).map((row, rowIndex) => (
              <tr key={`${row.join("-")}-${rowIndex}`}>
                {headers.map((header, columnIndex) => (
                  <td key={`${header}-${columnIndex}`}>{row[columnIndex] ?? ""}</td>
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
