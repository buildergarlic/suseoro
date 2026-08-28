"""Exact DLS ISBN TXT and title workbook contracts."""

from __future__ import annotations

from collections.abc import Iterable
from io import BytesIO
from pathlib import Path
from typing import Any

from openpyxl import Workbook

from suseoro.catalog.normalization import canonical_isbn13
from suseoro.exports.safe_cells import (
    StoredArtifact,
    escape_spreadsheet_cell,
    store_immutable_bytes,
)

DLS_TITLE_COLUMNS = (
    "자료명",
    "저자",
    "출판사",
    "자료유형",
    "신청갯수",
    "신청자ID",
    "ISBN",
)


def dls_isbn_txt(values: Iterable[object]) -> bytes:
    seen: set[str] = set()
    valid: list[str] = []
    for value in values:
        isbn = canonical_isbn13(value)
        if isbn is not None and isbn not in seen:
            seen.add(isbn)
            valid.append(isbn)
    return "\r\n".join(valid).encode("utf-8")


def dls_title_xlsx(rows: Iterable[dict[str, Any]]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Sheet1"
    sheet.append(DLS_TITLE_COLUMNS)
    for row in rows:
        isbn = canonical_isbn13(row.get("isbn")) or "0"
        quantity = row.get("quantity", 1)
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
            quantity = 1
        sheet.append(
            (
                escape_spreadsheet_cell(str(row.get("title") or "")),
                escape_spreadsheet_cell(str(row.get("author") or "미상")),
                escape_spreadsheet_cell(str(row.get("publisher") or "미상")),
                escape_spreadsheet_cell(str(row.get("material_type") or "단행본")),
                quantity,
                escape_spreadsheet_cell(str(row.get("requester_id") or "suseoro")),
                isbn,
            )
        )
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def store_dls_isbn_artifact(root: Path, values: Iterable[object]) -> StoredArtifact:
    return store_immutable_bytes(
        root, category="dls-isbn", content=dls_isbn_txt(values), suffix=".txt"
    )


def store_dls_title_artifact(
    root: Path, rows: Iterable[dict[str, Any]]
) -> StoredArtifact:
    return store_immutable_bytes(
        root, category="dls-title", content=dls_title_xlsx(rows), suffix=".xlsx"
    )
