"""Safe default and remembered-template order workbooks."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from io import BytesIO
from pathlib import Path
from typing import Any

from openpyxl import Workbook

from suseoro.exports.safe_cells import (
    StoredArtifact,
    escape_spreadsheet_cell,
    store_immutable_bytes,
)

DEFAULT_ORDER_COLUMNS = (
    ("isbn", "ISBN"),
    ("title", "자료명"),
    ("author", "저자"),
    ("publisher", "출판사"),
    ("quantity", "수량"),
    ("unit_price", "단가"),
    ("line_total_won", "금액"),
)


def order_xlsx(
    rows: Iterable[dict[str, Any]],
    *,
    columns: Sequence[tuple[str, str]] = DEFAULT_ORDER_COLUMNS,
) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "발주서"
    sheet.append([escape_spreadsheet_cell(label) for _, label in columns])
    for original in rows:
        row = dict(original)
        quantity = row.get("quantity") or 0
        unit_price = row.get("unit_price") or 0
        row.setdefault("line_total_won", quantity * unit_price)
        sheet.append(
            [
                escape_spreadsheet_cell(row.get(field, ""))
                if isinstance(row.get(field, ""), str)
                else row.get(field, "")
                for field, _ in columns
            ]
        )
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def store_order_artifact(
    root: Path,
    rows: Iterable[dict[str, Any]],
    *,
    columns: Sequence[tuple[str, str]] = DEFAULT_ORDER_COLUMNS,
) -> StoredArtifact:
    return store_immutable_bytes(
        root,
        category="orders",
        content=order_xlsx(rows, columns=columns),
        suffix=".xlsx",
    )
