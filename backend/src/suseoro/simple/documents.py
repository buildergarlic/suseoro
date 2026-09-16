"""Conservative local document imports with editable, source-accounted previews."""

from __future__ import annotations

import csv
import hashlib
import io
import os
import re
import subprocess
import unicodedata
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from suseoro.catalog.normalization import canonical_isbn13
from suseoro.ingestion.contracts import DocumentRole, RowStatus
from suseoro.ingestion.detection import detect_delimiter, detect_encoding, detect_file_type
from suseoro.ingestion.parsers.docx import parse_docx
from suseoro.ingestion.parsers.hwp import parse_hwp
from suseoro.ingestion.parsers.hwpx import parse_hwpx
from suseoro.ingestion.parsers.tabular import (
    CalamineCompatibilityError, _load_with_calamine, _load_with_openpyxl,
    _xlsx_formula_cells,
)
from suseoro.ingestion.safety import inspect_zip

MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_ROWS = 200_000
MAX_PAGES = 1000
_ISBN_PATTERN = re.compile(r"(?<!\d)(?:97[89][\s-]?(?:\d[\s-]?){9}\d|(?:\d[\s-]?){9}[\dXx])(?!\d)")
_TABLE_NODE = re.compile(r"(?P<table>.*table\[\d+\])/row\[(?P<row>\d+)\]/cell\[(?P<cell>\d+)\]$")


def _key(value: object) -> str:
    return re.sub(r"[^a-z0-9가-힣]", "", unicodedata.normalize("NFKC", str(value or "")).casefold())


ALIASES = {
    "title": ("title", "제목", "도서명", "자료명", "서명", "책제목", "도서제목", "booktitle", "품명", "도서명서명"),
    "author": ("author", "authors", "저자", "지은이", "글쓴이", "작가", "저자명", "저자역자"),
    "publisher": ("publisher", "출판사", "발행처", "출판", "발행사"),
    "isbn": ("isbn", "isbn13", "isbn10", "국제표준도서번호", "도서번호"),
    "price": ("price", "listprice", "정가", "가격", "도서정가", "소비자가", "표시가격", "단가"),
    "quantity": ("quantity", "수량", "권수", "부수", "신청갯수", "신청개수", "주문수량", "신청수량", "신청권수", "구입권수"),
    "category": ("category", "분류", "주제", "KDC", "분류기호", "분야", "분야KDC"),
    "requester": ("requester", "요청자", "신청자", "신청자명", "추천자", "신청자ID"),
    "audience": ("audience", "대상", "대상학년", "권장학년", "학년", "학교급"),
    "priority": ("priority", "우선순위", "중요도"),
    "source": ("source", "출처", "목록출처", "추천기관", "기관", "추천목록", "목록명"),
    "note": ("note", "메모", "비고", "추천사유", "신청사유"),
    "published_date": ("publisheddate", "publicationdate", "발행일", "발행년", "출판일", "발행연도", "출간일", "출간년도", "출판년도"),
    "link": ("link", "url", "참고링크", "참고URL", "도서정보URL", "도서정보링크", "상세정보URL", "상품URL"),
}
_FIELDS = {_key(alias): name for name, aliases in ALIASES.items() for alias in aliases}


def canonical_header(value: object) -> str | None:
    """Shared import/export Korean column-name recognition."""
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    direct = _FIELDS.get(_key(text))
    if direct:
        return direct
    # Strip units/length hints, not meaningful qualifiers such as 할인율.
    text = re.sub(r"[\(\[]\s*(?:원|권|부|개|KRW|13자리|10자리|필수)\s*[\)\]]", "", text, flags=re.IGNORECASE)
    direct = _FIELDS.get(_key(text))
    if direct:
        return direct
    parts = {_FIELDS.get(_key(part)) for part in re.split(r"[\n/·]", text)} - {None}
    return next(iter(parts)) if len(parts) == 1 else None


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value).strip()


def _safe_raw(values: dict[str, Any]) -> dict[str, Any]:
    return {
        str(key): value if value is None or isinstance(value, (str, int, bool)) else _text(value)
        for key, value in values.items()
    }


def _integer(value: Any, *, minimum: int, quantity: bool = False) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    string = re.sub(r"(?:KRW|원|₩|,|\s)", "", _text(value), flags=re.IGNORECASE)
    if quantity:
        string = re.sub(r"(?:권|부|개)$", "", string)
    if not re.fullmatch(r"\d+(?:\.0+)?", string):
        return None
    try:
        decimal = Decimal(string)
        return int(decimal) if minimum <= decimal <= 2_000_000_000 else None
    except (InvalidOperation, ValueError):
        return None


def _mapping(headers: list[str], provided: dict[str, str] | None) -> dict[str, str]:
    result: dict[str, str] = {}
    for header in headers:
        field = canonical_header(header.split("__", 1)[0])
        if field and (field not in result or (field == "isbn" and "13" in _key(header) and "13" not in _key(result[field]))):
            result[field] = header
    if provided is not None:
        # Public shape is canonical field -> original header. Accepting the
        # reverse shape also keeps imported older mapping preferences usable.
        for field, header in provided.items():
            if field in ALIASES:
                if header == "":
                    result.pop(field, None)
                elif header in headers:
                    result[field] = header
            elif header in ALIASES and field in headers:
                result[header] = field
    return result


def _isbn(value: Any) -> str | None:
    text = _text(value)
    direct = canonical_isbn13(text)
    if direct:
        return direct
    # Decimal avoids rounding a textual identifier through binary floating point.
    if re.fullmatch(r"\d+(?:\.\d+)?(?:[eE]\+?\d{1,2})?", text):
        try:
            number = Decimal(text)
            if number == number.to_integral_value() and 0 <= number < 10**13:
                return canonical_isbn13(str(int(number)))
        except (InvalidOperation, ValueError):
            pass
    return None


def _book(
    values: dict[str, Any], *, filename: str, sheet: str, row: int,
    mapping: dict[str, str] | None = None, warnings: list[str] | None = None,
    page: int | None = None, raw_text: str = "", uncertain: bool = False,
) -> dict[str, Any]:
    raw = _safe_raw(values)
    resolved = _mapping(list(raw), mapping)
    fields = {field: raw.get(header) for field, header in resolved.items()}
    notices = list(warnings or [])
    if any("\ufffd" in _text(value) for value in raw.values()):
        notices.append("읽을 수 없는 문자가 있습니다. 파일 인코딩과 원문을 확인해 주세요.")
    title = _text(fields.get("title"))
    isbn_input = _text(fields.get("isbn"))
    isbn = _isbn(isbn_input) if isbn_input else None
    isbn_columns = [header for header in raw if canonical_header(header.split("__", 1)[0]) == "isbn"]
    explicit_isbn = mapping is not None and (mapping.get("isbn") == "" or mapping.get("isbn") in raw or any(value == "isbn" and key in raw for key, value in mapping.items()))
    if not explicit_isbn:
        candidates = [(header, _isbn(raw[header])) for header in isbn_columns if _isbn(raw[header])]
        if candidates:
            candidates.sort(key=lambda item: "13" not in _key(item[0]))
            chosen_header, isbn = candidates[0]
            isbn_input = _text(raw[chosen_header])
            if len({value for _, value in candidates}) > 1:
                notices.append("ISBN 열의 값이 서로 다릅니다. 해당 도서의 ISBN을 원본에서 확인해 주세요.")
    if isbn and canonical_isbn13(isbn_input) is None:
        notices.append("숫자 표기의 ISBN을 변환했습니다. 원본의 자릿수를 확인해 주세요.")
    price = _integer(fields.get("price"), minimum=0)
    quantity_input = fields.get("quantity")
    quantity = 1 if quantity_input in (None, "") else _integer(quantity_input, minimum=1, quantity=True)
    if not title:
        notices.append("도서명을 확인해 주세요.")
    if not isbn:
        notices.append("ISBN이 없거나 체크숫자가 맞지 않습니다. 정확한 ISBN을 확인해 주세요.")
    if price is None:
        notices.append("정가를 확인해 주세요. 가격이 없으면 발주 금액에 포함되지 않습니다.")
    if quantity is None:
        notices.append("수량을 읽지 못해 1권으로 표시했습니다. 원본 수량을 확인해 주세요.")
        quantity = 1
    if any(isinstance(value, str) and value.lstrip().startswith(("=", "+", "@")) for value in raw.values()):
        notices.append("수식 형태의 원문은 실행하지 않았습니다. 값을 확인해 주세요.")
    if uncertain:
        notices.append("자동으로 서지를 확정할 수 없습니다. 원문과 비교해 주세요.")
    origin = f"{filename} · {sheet} · {row}행"
    if page is not None:
        origin = f"{filename} · {page}쪽 · {row}행"
    institution = _text(fields.get("source"))
    source = f"{institution} · {origin}" if institution else origin
    priority = _text(fields.get("priority")).lower()
    priority = {"상": "high", "높음": "high", "중": "normal", "보통": "normal", "하": "low", "낮음": "low"}.get(priority, priority)
    provenance: dict[str, Any] = {"filename": filename, "sheet": sheet, "row": row}
    if page is not None:
        provenance["page"] = page
    return {
        "title": title, "author": _text(fields.get("author")),
        "publisher": _text(fields.get("publisher")), "isbn": isbn or isbn_input,
        "price": price, "quantity": quantity, "selected": True,
        "category": _text(fields.get("category")), "requester": _text(fields.get("requester")),
        "audience": _text(fields.get("audience")), "priority": priority if priority in {"high", "normal", "low"} else "normal",
        "source": source, "note": _text(fields.get("note")),
        "published_date": _text(fields.get("published_date")), "link": _text(fields.get("link")),
        "needs_review": bool(notices), "warnings": list(dict.fromkeys(notices)),
        "raw_values": raw, "raw_text": raw_text or " | ".join(_text(value) for value in raw.values()),
        "provenance": provenance,
    }


def _unique_headers(values: list[Any]) -> list[str]:
    result: list[str] = []
    for index, value in enumerate(values, 1):
        base = _text(value) or f"열 {index}"
        header = base
        suffix = 2
        while header in result:
            header = f"{base}__{suffix}"
            suffix += 1
        result.append(header)
    return result


def _matrix(
    values: list[list[Any]], *, filename: str, sheet: str,
    mapping: dict[str, str] | None = None, page: int | None = None,
    diagnostics: list[dict] | None = None,
) -> tuple[list[dict], list[str]]:
    nonempty = [(index, row) for index, row in enumerate(values, 1) if any(_text(cell) for cell in row)]
    if not nonempty:
        return [], []
    def record(kind: str, number: int, row: list[Any], message: str) -> None:
        if diagnostics is not None:
            diagnostics.append({"kind": kind, "sheet": sheet, "row": number, "page": page, "raw_text": " | ".join(_text(value) for value in row), "message": message})

    def header_fields(row: list[Any]) -> set[str]:
        # A book with an ISBN or price is not a header even if its title is 저자.
        if any(_isbn(cell) or _integer(cell, minimum=0) is not None for cell in row if _text(cell)):
            return set()
        return {field for cell in row if (field := canonical_header(cell))}

    scores = [(len(header_fields(row)), index, row) for index, row in nonempty[:100]]
    score, header_number, header_values = max(scores, key=lambda item: (item[0], -item[1]))
    header_end = header_number
    if not score:
        header_number, header_values = nonempty[0]
        if max(len(row) for _, row in nonempty) == 1:
            # Include blank lines to retain physical line numbers and book blocks.
            return _lines("\n".join(_text(row[0]) if row else "" for row in values), filename=filename, sheet=sheet, page=page), []
        provided_headers = set(mapping or {}) | set((mapping or {}).values())
        explicit_header = any(_text(cell) in provided_headers for cell in header_values)
        alphabet_header = all(re.fullmatch(r"[A-Z]", _text(cell)) for cell in header_values)
        if not (explicit_header or alphabet_header):
            # Later rows having numbers does not prove the first row is a header:
            # its book may simply have no ISBN or price. Retain it for review.
            header_number = 0
            header_values = [f"열 {index}" for index in range(1, max(len(row) for _, row in nonempty) + 1)]
        header_end = header_number
    else:
        # Combine up to three adjacent semantic header rows. Vertical blanks
        # retain the upper label; grouped lower labels replace group captions.
        start = next(index for index, (number, _) in enumerate(nonempty) if number == header_number)
        first, last = start, start
        while first > 0 and start - first < 2 and header_fields(nonempty[first - 1][1]) and nonempty[first][0] - nonempty[first - 1][0] == 1:
            first -= 1
        while last + 1 < len(nonempty) and last - first < 2 and header_fields(nonempty[last + 1][1]) and nonempty[last + 1][0] - nonempty[last][0] == 1:
            last += 1
        header_number, header_end = nonempty[first][0], nonempty[last][0]
        width = max(len(row) for _, row in nonempty[first:last + 1])
        combined: list[Any] = [None] * width
        for _, row in nonempty[first:last + 1]:
            for index, value in enumerate(row):
                if canonical_header(value) or (not _text(combined[index]) and _text(value)):
                    combined[index] = value
        header_values = combined
    headers = _unique_headers(header_values)
    rows = []
    for source_row, values_row in nonempty:
        if header_number <= source_row <= header_end and header_number:
            record("header", source_row, values_row, "열 제목으로 사용했습니다.")
            continue
        if source_row < header_number:
            if any(_isbn(value) or _ISBN_PATTERN.search(_text(value)) for value in values_row):
                rows.extend(_lines(" | ".join(_text(value) for value in values_row), filename=filename, sheet=sheet, page=page, start_row=source_row))
            else:
                record("preamble", source_row, values_row, "표 앞의 안내 문구입니다. 원문을 보존했습니다.")
            continue
        recognized = header_fields(values_row)
        if len(recognized) >= 2 and all(not _text(cell) or canonical_header(cell) for cell in values_row):
            record("repeated_header", source_row, values_row, "반복된 열 제목입니다.")
            continue
        if any(_key(value) in {"합계", "총계", "소계", "총합계"} for value in values_row) and not any(_isbn(value) for value in values_row):
            record("summary", source_row, values_row, "합계 행입니다. 원문을 보존했습니다.")
            continue
        extended = headers + [f"열 {index}" for index in range(len(headers) + 1, len(values_row) + 1)]
        raw = {header: values_row[index] if index < len(values_row) else None for index, header in enumerate(extended)}
        rows.append(_book(raw, filename=filename, sheet=sheet, row=source_row, mapping=mapping, page=page, uncertain=not bool(_mapping(extended, mapping))))
    return rows, headers


def _lines(text: str, *, filename: str, sheet: str, page: int | None = None, start_row: int = 1) -> list[dict]:
    result = []
    block: dict[str, str] = {}
    block_lines: list[str] = []
    block_start = start_row

    def flush() -> None:
        if block:
            result.append(_book(dict(block), filename=filename, sheet=sheet, row=block_start, page=page, raw_text="\n".join(block_lines), uncertain=True))
            block.clear()
            block_lines.clear()

    for index, line in enumerate(text.splitlines(), start_row):
        line = line.strip()
        if not line:
            flush()
            continue
        label = re.match(r"^([^:：]{1,30})\s*[:：]\s*(.+)$", line)
        field = canonical_header(label.group(1)) if label else None
        if field:
            if field in block:
                flush()
            if not block:
                block_start = index
            block[field] = label.group(2).strip()
            block_lines.append(line)
            continue
        flush()
        matches = list(_ISBN_PATTERN.finditer(line))
        valid = [(match.group(), canonical_isbn13(match.group())) for match in matches]
        valid = [(raw, isbn) for raw, isbn in valid if isbn]
        if valid:
            for _, isbn in valid:
                result.append(_book({"isbn": isbn, "note": line}, filename=filename, sheet=sheet, row=index, page=page, raw_text=line, uncertain=True))
        else:
            result.append(_book({"note": line}, filename=filename, sheet=sheet, row=index, page=page, raw_text=line, uncertain=True))
    flush()
    return result


def _text_matrix(path: Path) -> list[list[Any]]:
    contents = path.read_bytes()
    try:
        _, text = detect_encoding(contents)
    except (ValueError, UnicodeDecodeError):
        detected = detect_file_type(path)
        text = contents.decode(detected.encoding or "utf-8", errors="replace")
    lines = text.splitlines()
    labeled = any((match := re.match(r"^([^:：]{1,30})\s*[:：]", line)) and canonical_header(match.group(1)) for line in lines)
    if labeled:
        return [[line] for line in lines]
    try:
        delimiter, _ = detect_delimiter(text)
        rows = list(csv.reader(io.StringIO(text, newline=""), delimiter=delimiter))
        # A price comma in prose is not evidence of a two-column CSV table.
        meaningful = [row for row in rows if any(_text(cell) for cell in row)]
        if meaningful and all(any(_ISBN_PATTERN.search(_text(cell)) and not _isbn(cell) for cell in row) for row in meaningful):
            return [[line] for line in lines]
        return rows
    except csv.Error:
        return [[line] for line in lines]


def _tabular(path: Path, filename: str, mapping: dict[str, str] | None, digest: str) -> dict:
    kind = detect_file_type(path).format
    rows: list[dict] = []
    headers: list[str] = []
    diagnostics: list[dict] = []
    if kind in {"CSV", "TSV", "TXT"}:
        matrices = [("텍스트", _text_matrix(path))]
    else:
        try:
            matrices = _load_with_calamine(path)
        except CalamineCompatibilityError:
            if kind != "XLSX":
                raise ValueError("표를 읽지 못했습니다. 원본을 Excel 또는 CSV로 다시 저장해 주세요.") from None
            matrices = _load_with_openpyxl(path, read_only=True, data_only=False)
        if kind == "XLSX":
            # Preserve formulas, including cells outside a cropped used range.
            for (sheet_number, row_number, column_number), formula in _xlsx_formula_cells(path).items():
                values = matrices[sheet_number - 1][1]
                while len(values) < row_number:
                    values.append([])
                while len(values[row_number - 1]) < column_number:
                    values[row_number - 1].append(None)
                values[row_number - 1][column_number - 1] = formula
    for name, values in matrices:
        converted, sheet_headers = _matrix(values, filename=filename, sheet=name, mapping=mapping, diagnostics=diagnostics)
        rows.extend(converted)
        headers.extend(header for header in sheet_headers if header not in headers)
    return {"rows": rows, "headers": headers, "mapping": _mapping(headers, mapping), "warnings": [], "diagnostics": diagnostics}


def _document(path: Path, filename: str, kind: str, mapping: dict[str, str] | None, digest: str) -> dict:
    parsed = {"DOCX": parse_docx, "HWPX": parse_hwpx, "HWP": parse_hwp}[kind](path, role=DocumentRole.PURCHASE_REQUEST, sha256=digest)
    tables: dict[tuple[str, str], dict[int, dict[int, str]]] = defaultdict(lambda: defaultdict(dict))
    hwp_cells: dict[tuple[str, str], list[Any]] = defaultdict(list)
    other: list[Any] = []
    for row in parsed.rows:
        match = _TABLE_NODE.match(str(row.raw_values.get("node", "")))
        sheet = (row.provenance.sheet or kind).split("#", 1)[0]
        if row.status == RowStatus.ROW_ERROR:
            other.append(row)
        elif match:
            tables[(sheet, match.group("table"))][int(match.group("row"))][int(match.group("cell"))] = _text(row.fields["text"].value)
        elif kind == "HWP" and row.raw_values.get("cell") is not None:
            hwp_cells[(sheet, str(row.raw_values.get("table", 1)))].append(row)
        else:
            other.append(row)
    rows, headers, warnings, diagnostics = [], [], [], []
    for (sheet, table), entries in tables.items():
        width = max(column for cells in entries.values() for column in cells)
        matrix = [[cells.get(column, "") for column in range(1, width + 1)] for _, cells in sorted(entries.items())]
        converted, found_headers = _matrix(matrix, filename=filename, sheet=f"{sheet} {table}", mapping=mapping, diagnostics=diagnostics)
        rows.extend(converted)
        headers.extend(header for header in found_headers if header not in headers)
    for (sheet, table), cells in hwp_cells.items():
        cells.sort(key=lambda row: int(row.raw_values.get("cell") or 0))
        texts = [_text(row.fields.get("text").value) for row in cells]
        candidates = [(sum(canonical_header(value) is not None for value in texts[:width]), width) for width in range(2, min(30, len(texts) // 2) + 1) if len(texts) % width == 0]
        score, width = max(candidates, key=lambda item: (item[0], -item[1]), default=(0, 0))
        if score >= 2:
            converted, found_headers = _matrix([texts[offset:offset + width] for offset in range(0, len(texts), width)], filename=filename, sheet=f"{sheet} 표 {table}", mapping=mapping, diagnostics=diagnostics)
            # HWP v5 exposes sequential cells; retain the uncertainty from
            # inferring the number of columns instead of inventing exact layout.
            for book in converted:
                book["needs_review"] = True
                book["warnings"].append("HWP 표의 열 구조를 추정했습니다. 원본 표와 비교해 주세요.")
            rows.extend(converted)
            headers.extend(header for header in found_headers if header not in headers)
        else:
            rows.extend(_lines("\n".join(texts), filename=filename, sheet=f"{sheet} 표 {table}"))
    paragraphs: list[tuple[int, str]] = []
    paragraph_sheet = ""

    def flush_paragraphs() -> None:
        if paragraphs:
            text = "\n".join(value for _, value in paragraphs)
            rows.extend(_lines(text, filename=filename, sheet=paragraph_sheet, start_row=paragraphs[0][0]))
            paragraphs.clear()

    for row in other:
        text = _text(row.fields["text"].value) if "text" in row.fields else _text(row.raw_values.get("text"))
        sheet = row.provenance.sheet or kind
        if row.error_message:
            flush_paragraphs()
            message = f"{sheet}: {row.error_message}"
            warnings.append(message)
            rows.append(_book({"note": text}, filename=filename, sheet=sheet, row=row.provenance.source_row, warnings=[message], uncertain=True))
        else:
            base_sheet = sheet.split("#", 1)[0]
            if paragraphs and (base_sheet != paragraph_sheet or row.provenance.source_row != paragraphs[-1][0] + 1):
                flush_paragraphs()
            paragraph_sheet = base_sheet
            paragraphs.append((row.provenance.source_row, text))
    flush_paragraphs()
    return {"rows": rows, "headers": headers, "mapping": _mapping(headers, mapping), "warnings": warnings, "diagnostics": diagnostics}


def _ocr(path: Path, page_number: int) -> str:
    executable = os.environ.get("SUSEORO_TESSERACT", "").strip()
    if not executable or not Path(executable).is_file():
        raise RuntimeError("OCR을 사용할 수 없습니다. 한국어 OCR이 포함된 설치본을 사용하거나 텍스트 PDF/Excel로 다시 저장해 주세요.")
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(str(path))
    try:
        page = document[page_number - 1]
        try:
            bitmap = page.render(scale=2)
            try:
                from io import BytesIO

                output = BytesIO()
                bitmap.to_pil().save(output, format="PNG")
                image = output.getvalue()
            finally:
                bitmap.close()
        finally:
            page.close()
    finally:
        document.close()
    options: dict[str, Any] = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    try:
        completed = subprocess.run([executable, "stdin", "stdout", "-l", "kor+eng", "--psm", "6"], input=image, capture_output=True, timeout=45, check=True, shell=False, **options)
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError("OCR을 완료하지 못했습니다. 한국어·영어 언어 자료와 원본 페이지를 확인해 주세요.") from error
    return completed.stdout.decode("utf-8", errors="replace")


def _pdf(path: Path, filename: str, mapping: dict[str, str] | None) -> dict:
    import pdfplumber

    rows, warnings, headers, diagnostics = [], [], [], []
    with pdfplumber.open(path) as document:
        if len(document.pages) > MAX_PAGES:
            raise ValueError(f"PDF는 {MAX_PAGES:,}쪽 이하로 나누어 주세요.")
        for number, page in enumerate(document.pages, 1):
            page_start = len(rows)
            used_ocr = False
            try:
                tables = page.find_tables()
                for table_index, table in enumerate(tables, 1):
                    values = table.extract()
                    converted, found_headers = _matrix(values, filename=filename, sheet=f"표 {table_index}", mapping=mapping, page=number, diagnostics=diagnostics)
                    rows.extend(converted)
                    headers.extend(header for header in found_headers if header not in headers)
                boxes = [table.bbox for table in tables]
                def outside_tables(obj):
                    x = (obj.get("x0", 0) + obj.get("x1", 0)) / 2
                    y = (obj.get("top", 0) + obj.get("bottom", 0)) / 2
                    return not any(left <= x <= right and top <= y <= bottom for left, top, right, bottom in boxes)
                remaining = page.filter(outside_tables).extract_text() or ""
                if not tables and len(remaining.strip()) < 12:
                    remaining = _ocr(path, number)
                    used_ocr = True
                    if not remaining.strip():
                        raise RuntimeError("OCR에서 글자를 읽지 못했습니다. 원본 페이지를 확인해 주세요.")
                    warnings.append(f"{number}쪽: OCR로 읽었습니다. ISBN과 가격을 원본과 비교해 주세요.")
                if remaining.strip():
                    nonempty = [line for line in remaining.splitlines() if line.strip()]
                    table_values = None
                    for delimiter in ("\t", "|", ";", ","):
                        candidate = list(csv.reader(nonempty, delimiter=delimiter))
                        if any(sum(canonical_header(cell) is not None for cell in line) >= 2 for line in candidate[:25]):
                            table_values = candidate
                            break
                    if table_values:
                        converted, found_headers = _matrix(table_values, filename=filename, sheet="본문", mapping=mapping, page=number, diagnostics=diagnostics)
                        rows.extend(converted)
                        headers.extend(header for header in found_headers if header not in headers)
                    else:
                        rows.extend(_lines(remaining, filename=filename, sheet="본문", page=number))
                if used_ocr:
                    for book in rows[page_start:]:
                        book["needs_review"] = True
                        book["warnings"].append("OCR로 읽은 ISBN과 가격을 원본과 비교해 주세요.")
            except Exception as error:
                # One corrupt or unreadable page never erases other pages.
                message = f"{number}쪽: {error}"
                warnings.append(message)
                rows.append(_book({"note": message}, filename=filename, sheet="PDF", row=1, page=number, warnings=[message], uncertain=True))
    return {"rows": rows, "headers": headers, "mapping": _mapping(headers, mapping), "warnings": warnings, "diagnostics": diagnostics}


def parse_upload(path: Path, filename: str, mapping: dict[str, str] | None = None) -> dict:
    """Return editable Book rows and canonical-field -> source-header mapping.

    Files are never modified. Failed rows/pages are retained as review records;
    file-level safety violations raise ValueError for a visible upload error.
    """
    path = Path(path)
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("파일은 100MB 이하로 나누어 주세요.")
    with path.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    detected = detect_file_type(path)
    kind = detected.format
    if kind in {"XLSX", "XLSB", "ODS", "HWPX", "DOCX"}:
        inspect_zip(path)
    if kind in {"XLSX", "XLS", "XLSB", "ODS", "CSV", "TSV", "TXT"}:
        result = _tabular(path, filename, mapping, digest)
    elif kind in {"DOCX", "HWPX", "HWP"}:
        result = _document(path, filename, kind, mapping, digest)
    elif kind == "PDF":
        result = _pdf(path, filename, mapping)
    else:
        raise ValueError("Excel, CSV, TXT, PDF, HWP, HWPX, DOCX 파일을 선택해 주세요.")
    if len(result["rows"]) > MAX_ROWS:
        raise ValueError(f"한 번에 {MAX_ROWS:,}행까지 가져올 수 있습니다. 파일을 나누어 주세요.")
    for row in result["rows"]:
        row["provenance"]["sha256"] = digest
    if not result["rows"]:
        result["warnings"].append("읽을 수 있는 도서 행이 없습니다. 파일 내용과 열 제목을 확인해 주세요.")
    result["warnings"] = list(dict.fromkeys(result["warnings"]))
    return result
