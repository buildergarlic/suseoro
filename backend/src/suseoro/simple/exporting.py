"""Readable Korean purchase files and conservative school-template filling."""

from __future__ import annotations

import csv
import html
import re
import zipfile
from copy import copy
from decimal import Decimal, ROUND_HALF_UP
from io import BytesIO, StringIO
from pathlib import Path
from typing import Any

from openpyxl import Workbook, load_workbook
from openpyxl.formula.tokenizer import Tokenizer
from openpyxl.formula.translate import Translator
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from suseoro.exports.safe_cells import escape_spreadsheet_cell
from suseoro.ingestion.safety import inspect_zip
from suseoro.simple.documents import canonical_header

_PLACEHOLDER = re.compile(r"\{\{\s*([\w가-힣]+)\s*\}\}")
_EXTRA_FIELDS = {
    "번호": "number", "순번": "number", "no": "number", "no.": "number", "number": "number", "sequence": "number",
    "공급가": "order_price", "구입단가": "order_price", "구매단가": "order_price", "할인단가": "order_price", "주문단가": "order_price", "order_price": "order_price", "unit_price": "order_price",
    "금액": "line_total", "합계금액": "line_total", "공급금액": "line_total", "구입금액": "line_total", "주문금액": "line_total", "line_total": "line_total", "line_total_won": "line_total",
    "할인율": "discount_percent", "discount_percent": "discount_percent",
}
_BOOK_FIELDS = {"number", "title", "author", "publisher", "isbn", "price", "quantity", "order_price", "line_total", "discount_percent", "category", "requester", "audience", "priority", "source", "note", "published_date"}
_COLUMNS = [
    ("number", "번호", 7), ("title", "도서명", 36), ("author", "지은이", 20),
    ("publisher", "출판사", 18), ("isbn", "ISBN", 20), ("price", "정가", 13),
    ("quantity", "수량", 8), ("order_price", "구입단가", 14), ("line_total", "금액", 15),
    ("category", "분류", 12), ("requester", "요청자", 12), ("source", "목록출처", 34), ("note", "비고", 30),
]
_MIME = {"xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "csv": "text/csv; charset=utf-8", "html": "text/html; charset=utf-8"}


def _field(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    token = value.strip()
    match = _PLACEHOLDER.fullmatch(token)
    if match:
        token = match.group(1)
    if token in _BOOK_FIELDS:
        return token
    return _EXTRA_FIELDS.get(token.lower()) or canonical_header(token)


def _workbook(path: Path):
    if path.suffix.lower() != ".xlsx":
        raise ValueError("학교 양식은 매크로가 없는 XLSX 파일을 사용해 주세요.")
    inspect_zip(path)
    with zipfile.ZipFile(path) as archive:
        members = [name.lower() for name in archive.namelist()]
        if any("vbaproject" in name or "externallinks/" in name or "activex/" in name for name in members):
            raise ValueError("매크로나 외부 연결이 있는 양식은 사용할 수 없습니다. 일반 XLSX로 저장해 주세요.")
    workbook = load_workbook(path, data_only=False, keep_links=False)
    if any(sheet.max_row > 200_000 or sheet.max_column > 200 for sheet in workbook):
        workbook.close()
        raise ValueError("양식의 행 또는 열 범위가 너무 큽니다. 빈 범위를 지우고 다시 저장해 주세요.")
    for sheet in workbook:
        for row in sheet:
            for cell in row:
                if cell.data_type == "f" and re.search(r"(?:\[|https?://|file:|\\\\|\||(?:WEBSERVICE|HYPERLINK|CALL|EXEC|RTD)\s*\()", str(cell.value), re.IGNORECASE):
                    workbook.close()
                    raise ValueError("외부 파일·사이트·프로그램을 호출하는 수식이 포함된 양식은 사용할 수 없습니다.")
    return workbook


def _find_table(workbook) -> dict:
    candidates = []
    for sheet in workbook:
        for row in sheet.iter_rows(max_row=min(sheet.max_row, 100)):
            placeholders = {cell.column: _field(cell.value) for cell in row if isinstance(cell.value, str) and _PLACEHOLDER.fullmatch(cell.value.strip()) and _field(cell.value)}
            headers = {cell.column: _field(cell.value) for cell in row if _field(cell.value)}
            for mode, found in (("placeholder", placeholders), ("header", headers)):
                if not ({"title", "isbn"} & set(found.values())):
                    continue
                score = len(set(found.values())) + (1000 if mode == "placeholder" else 0)
                candidates.append((score, -row[0].row, sheet.title, mode, found, row[0].row))
    if not candidates:
        raise ValueError("도서명 또는 ISBN 열을 찾지 못했습니다. 열 제목을 넣거나 {{title}}, {{isbn}}, {{price}}, {{quantity}} 표시를 사용해 주세요.")
    _, _, sheet_name, mode, mapping, header_row = max(candidates, key=lambda item: (item[0], item[1]))
    sheet = workbook[sheet_name]
    data_row = header_row if mode == "placeholder" else header_row + 1
    if any(merged.min_row <= data_row <= merged.max_row and merged.max_col > merged.min_col for merged in sheet.merged_cells.ranges):
        raise ValueError("도서 입력 행에 가로로 병합된 셀이 있습니다. 도서 한 권에 한 행이 되도록 양식을 조정해 주세요.")
    return {"sheet": sheet_name, "header_row": header_row, "data_row": data_row, "mode": mode, "field_columns": mapping}


def inspect_template(path: Path) -> dict:
    """Inspect without modifying the template or executing any formulas."""
    workbook = _workbook(Path(path))
    try:
        layout = _find_table(workbook)
        sheet = workbook[layout["sheet"]]
        columns = [str(sheet.cell(layout["header_row"], column).value or field) for column, field in layout["field_columns"].items()]
        warnings = []
        if "price" not in layout["field_columns"].values() and "order_price" not in layout["field_columns"].values():
            warnings.append("양식에서 가격 열을 찾지 못했습니다. 출력 후 학교 양식을 확인해 주세요.")
        if any(cell.data_type == "f" for row in sheet for cell in row):
            warnings.append("기존 수식은 보존합니다. Excel에서 열 때 계산되므로 출력 후 합계를 확인해 주세요.")
        return {"columns": columns, "headers": columns, "warnings": warnings, **{key: value for key, value in layout.items() if key != "field_columns"}, "mapping": {field: get_column_letter(column) for column, field in layout["field_columns"].items()}}
    finally:
        workbook.close()


def _prepared(books: list[dict], list_info: dict) -> tuple[list[dict], dict]:
    discount = Decimal(str(list_info.get("discount_percent", 0)))
    rows = []
    for number, book in enumerate((book for book in books if book.get("selected", True)), 1):
        price = book.get("price")
        quantity = book.get("quantity", 1)
        if isinstance(price, bool) or not isinstance(price, int) or price < 0:
            raise ValueError("정가가 확인되지 않은 책이 있습니다.")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
            raise ValueError("수량을 확인해 주세요.")
        unit = int((Decimal(price) * (100 - discount) / 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        rows.append({**book, "number": number, "order_price": unit, "line_total": unit * quantity, "discount_percent": float(discount)})
    totals = {
        "order_total": sum(row["line_total"] for row in rows),
        "list_total": sum(row["price"] * row["quantity"] for row in rows),
        "total_quantity": sum(row["quantity"] for row in rows),
        "book_count": len(rows), "discount_percent": float(discount),
        "budget": list_info.get("budget", 15_000_000),
        "year": list_info.get("year", ""), "list_name": list_info.get("name", "도서 구입 목록"),
    }
    totals["remaining"] = totals["budget"] - totals["order_total"]
    return rows, totals


def _write(cell, value: Any, field: str | None = None) -> None:
    cell.value = escape_spreadsheet_cell(value)
    if field == "isbn":
        cell.value = escape_spreadsheet_cell(str(value or ""))
        cell.number_format = "@"
    elif field in {"price", "order_price", "line_total"}:
        cell.number_format = '#,##0"원"'


def _standard(rows: list[dict], totals: dict, school_name: str):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "도서구입목록"
    last_column = len(_COLUMNS)
    sheet.merge_cells(start_row=1, start_column=1, end_row=1, end_column=last_column)
    _write(sheet.cell(1, 1), totals["list_name"])
    sheet.cell(1, 1).font = Font(name="맑은 고딕", size=19, bold=True, color="163A36")
    sheet.row_dimensions[1].height = 34
    _write(sheet.cell(2, 1), school_name)
    sheet.merge_cells(start_row=2, start_column=1, end_row=2, end_column=5)
    _write(sheet.cell(3, 1), f"{totals['year']}년 · {totals['book_count']:,}종 / {totals['total_quantity']:,}권 · 할인율 {totals['discount_percent']:g}%")
    sheet.merge_cells(start_row=3, start_column=1, end_row=3, end_column=last_column)
    _write(sheet.cell(4, 1), f"예산 {totals['budget']:,}원  |  구입 금액 {totals['order_total']:,}원  |  잔액 {totals['remaining']:,}원")
    sheet.merge_cells(start_row=4, start_column=1, end_row=4, end_column=last_column)
    for column, (_, title, width) in enumerate(_COLUMNS, 1):
        cell = sheet.cell(6, column, title)
        cell.font = Font(name="맑은 고딕", bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="245B50")
        cell.alignment = Alignment(horizontal="center", vertical="center")
        sheet.column_dimensions[get_column_letter(column)].width = width
    for row_number, book in enumerate(rows, 7):
        for column, (field, _, _) in enumerate(_COLUMNS, 1):
            cell = sheet.cell(row_number, column)
            _write(cell, book.get(field, ""), field)
            cell.font = Font(name="맑은 고딕", size=10)
            cell.alignment = Alignment(vertical="center", wrap_text=field in {"title", "source", "note"})
            cell.border = Border(bottom=Side(style="hair", color="DDE5DF"))
            if row_number % 2 == 0:
                cell.fill = PatternFill("solid", fgColor="F1F6F2")
        sheet.row_dimensions[row_number].height = 34
    footer = len(rows) + 7
    sheet.cell(footer, 2, "합계")
    sheet.cell(footer, 7, totals["total_quantity"])
    _write(sheet.cell(footer, 9), totals["order_total"], "line_total")
    for cell in sheet[footer]:
        cell.font = Font(name="맑은 고딕", bold=True)
        cell.fill = PatternFill("solid", fgColor="E1EBDD")
    sheet.freeze_panes = "C7"
    sheet.auto_filter.ref = f"A6:{get_column_letter(last_column)}{max(6, footer - 1)}"
    sheet.print_title_rows = "1:6"
    sheet.print_area = f"A1:{get_column_letter(last_column)}{footer}"
    sheet.sheet_properties.pageSetUpPr.fitToPage = True
    sheet.page_setup.orientation = "landscape"
    sheet.page_setup.paperSize = sheet.PAPERSIZE_A4
    sheet.page_setup.fitToWidth = 1
    sheet.page_setup.fitToHeight = 0
    return workbook


def _insert_preserving_footer(sheet, at: int, count: int) -> None:
    if count <= 0:
        return
    merges = [copy(merged) for merged in sheet.merged_cells.ranges]
    for merged in list(sheet.merged_cells.ranges):
        sheet.unmerge_cells(str(merged))
    dimensions = {index: copy(dimension) for index, dimension in sheet.row_dimensions.items() if index >= at}
    formulas = [(cell.row, cell.column, cell.value) for row in sheet for cell in row if cell.data_type == "f"]
    sheet.insert_rows(at, count)
    for index in sorted(dimensions, reverse=True):
        dimension = dimensions[index]
        dimension.index = index + count
        sheet.row_dimensions[index + count] = dimension
    for merged in merges:
        if merged.min_row >= at:
            merged.shift(row_shift=count)
        elif merged.max_row >= at:
            merged.max_row += count
        sheet.merge_cells(str(merged))
    for row, column, formula in formulas:
        destination = sheet.cell(row + count if row >= at else row, column)
        destination.value = _expand_formula(formula, sheet.title, at, count)


def _expand_formula(formula: str, sheet_name: str, at: int, count: int) -> str:
    """Apply insertion semantics rather than copying a footer formula downward."""
    reference_pattern = re.compile(r"(\$?[A-Za-z]{1,3})(\$?)(\d+)$")
    tokens = Tokenizer(formula).items
    for token in tokens:
        if token.type != "OPERAND" or token.subtype != "RANGE":
            continue
        prefix, separator, reference = token.value.rpartition("!")
        if separator and prefix.strip("'").replace("''", "'") != sheet_name:
            continue
        if not separator:
            reference = token.value
        parts = reference.split(":")
        matches = [reference_pattern.fullmatch(part) for part in parts]
        if len(parts) > 2 or not all(matches):
            continue
        adjusted = []
        for index, match in enumerate(matches):
            number = int(match.group(3))
            if number >= at or (len(matches) == 2 and index == 1 and number == at - 1):
                number += count
            adjusted.append(f"{match.group(1)}{match.group(2)}{number}")
        token.value = (prefix + separator if separator else "") + ":".join(adjusted)
    return "=" + "".join(token.value for token in tokens)


def _custom(rows: list[dict], totals: dict, school_name: str, path: Path):
    workbook = _workbook(path)
    layout = _find_table(workbook)
    sheet = workbook[layout["sheet"]]
    start = layout["data_row"]
    pattern = [copy(cell) for cell in sheet[start]]
    pattern_height = sheet.row_dimensions[start].height
    _insert_preserving_footer(sheet, start + 1, max(0, len(rows) - 1))
    for offset, book in enumerate(rows):
        row_number = start + offset
        sheet.row_dimensions[row_number].height = pattern_height
        for original in pattern:
            target = sheet.cell(row_number, original.column)
            if original.has_style:
                target._style = copy(original._style)
            target.number_format = original.number_format
            target.alignment = copy(original.alignment)
            target.protection = copy(original.protection)
            if original.data_type == "f":
                target.value = Translator(original.value, origin=original.coordinate).translate_formula(target.coordinate)
            elif layout["mode"] == "placeholder":
                target.value = original.value
            else:
                # Preserve fixed labels but do not copy sample book data into
                # unknown columns. Blank numeric data rows stay blank.
                target.value = None
        for column, field in layout["field_columns"].items():
            _write(sheet.cell(row_number, column), book.get(field, ""), field)
    summaries = {**totals, "school_name": school_name, "school": school_name, "name": totals["list_name"]}
    for page in workbook:
        for row in page:
            for cell in row:
                if not isinstance(cell.value, str) or cell.data_type == "f":
                    continue
                matches = list(_PLACEHOLDER.finditer(cell.value))
                if not matches:
                    continue
                if len(matches) == 1 and matches[0].span() == (0, len(cell.value)) and matches[0].group(1) in summaries:
                    _write(cell, summaries[matches[0].group(1)])
                else:
                    _write(cell, _PLACEHOLDER.sub(lambda match: str(summaries.get(match.group(1), match.group(0))), cell.value))
    sheet.print_area = f"A1:{get_column_letter(sheet.max_column)}{sheet.max_row}"
    return workbook


def _html(rows: list[dict], totals: dict, school_name: str) -> bytes:
    def esc(value):
        return html.escape(str(value if value is not None else ""))
    numeric = {"price", "order_price", "line_total", "quantity", "number"}
    headings = "".join(f"<th>{esc(title)}</th>" for _, title, _ in _COLUMNS)
    body = "".join("<tr>" + "".join(f"<td class='{'num' if field in numeric else ''}'>{esc(f'{book.get(field, 0):,}' if field in numeric else book.get(field, ''))}</td>" for field, _, _ in _COLUMNS) + "</tr>" for book in rows)
    document = f"""<!doctype html><html lang="ko"><meta charset="utf-8"><title>{esc(totals['list_name'])}</title>
<style>body{{font-family:'Malgun Gothic',sans-serif;margin:28px;color:#173930}}h1{{font-size:24px}}p{{line-height:1.7}}table{{width:100%;border-collapse:collapse;font-size:11px}}th{{background:#e7efe9}}th,td{{border:1px solid #c5d2c8;padding:7px;text-align:left;overflow-wrap:anywhere}}.num{{text-align:right;white-space:nowrap}}tfoot{{font-weight:bold}}@page{{size:A4 landscape;margin:12mm}}@media print{{body{{margin:0}}thead{{display:table-header-group}}tr{{break-inside:avoid}}}}</style>
<h1>{esc(totals['list_name'])}</h1><p>{esc(school_name)} · {esc(totals['year'])}년<br>{totals['book_count']:,}종 / {totals['total_quantity']:,}권 · 할인율 {totals['discount_percent']:g}% · 구입 금액 {totals['order_total']:,}원<br>예산 {totals['budget']:,}원 · 잔액 {totals['remaining']:,}원</p>
<table><thead><tr>{headings}</tr></thead><tbody>{body}</tbody></table></html>"""
    return document.encode("utf-8")


def export_books(books: list[dict], list_info: dict, school_name: str, format: str = "xlsx", template_path: Path | None = None) -> tuple[bytes, str, str]:
    """Return download bytes, safe Korean filename, and MIME type.

    The API validates unresolved book decisions, ISBNs and budget before calling.
    Prices are independently checked here to prevent a missing price becoming 0.
    """
    if format not in _MIME:
        raise ValueError("XLSX, CSV 또는 HTML 형식을 선택해 주세요.")
    if template_path is not None and format != "xlsx":
        raise ValueError("학교 양식은 XLSX 출력에서 사용할 수 있습니다.")
    rows, totals = _prepared(books, list_info)
    basename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(list_info.get("name") or "도서구입목록")).strip(" .")[:90] or "도서구입목록"
    filename = f"{basename}_발주서.{format}"
    if format == "html":
        return _html(rows, totals, school_name), filename, _MIME[format]
    if format == "csv":
        output = StringIO(newline="")
        writer = csv.writer(output, lineterminator="\r\n")
        writer.writerow([title for _, title, _ in _COLUMNS])
        for book in rows:
            writer.writerow([escape_spreadsheet_cell(book.get(field, "")) for field, _, _ in _COLUMNS])
        return output.getvalue().encode("utf-8-sig"), filename, _MIME[format]
    workbook = _custom(rows, totals, school_name, Path(template_path)) if template_path else _standard(rows, totals, school_name)
    output = BytesIO()
    try:
        workbook.save(output)
    finally:
        workbook.close()
    return output.getvalue(), filename, _MIME[format]
