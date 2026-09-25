from __future__ import annotations

import csv
import zipfile
from pathlib import Path

import pytest
from openpyxl import Workbook
from pypdf import PdfWriter

from suseoro.simple.documents import parse_upload


@pytest.mark.parametrize(
    "headers",
    [
        ["도서명", "저자", "출판사", "정가", "수량"],
        ["도서 제목", "지은이", "발행처", "정가(원)", "신청수량(권)"],
        ["도서명(서명)", "저자/역자", "발행사", "가격", "권수"],
    ],
)
@pytest.mark.parametrize("isbn", [None, "", "  "])
def test_complete_korean_csv_bibliography_is_ready_without_isbn(tmp_path: Path, headers, isbn):
    path = tmp_path / "학교 추천.csv"
    headers = headers.copy()
    values = ["첫 번째 책", "김작가", "책출판", "12000", "2"]
    if isbn is not None:
        headers.append("ISBN(13자리)")
        values.append(isbn)
    with path.open("w", encoding="utf-8-sig", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(["2026년 학교도서관 추천 목록"] + [""] * (len(headers) - 1))
        writer.writerow(headers)
        writer.writerow(values)

    result = parse_upload(path, path.name)

    assert len(result["rows"]) == 1
    row = result["rows"][0]
    assert (row["title"], row["author"], row["publisher"]) == ("첫 번째 책", "김작가", "책출판")
    assert (row["isbn"], row["price"], row["quantity"]) == ("", 12000, 2)
    assert row["needs_review"] is False
    assert row["warnings"] == []
    assert row["provenance"]["row"] == 3
    assert any(item["kind"] == "preamble" and item["row"] == 1 for item in result["diagnostics"])


@pytest.mark.parametrize(
    ("title", "author", "publisher", "missing_label"),
    [("", "김작가", "책출판", "도서명"), ("책", "", "책출판", "저자"), ("책", "김작가", "", "출판사")],
)
def test_missing_isbn_requires_missing_bibliographic_fields(tmp_path: Path, title, author, publisher, missing_label):
    path = tmp_path / "추천.csv"
    path.write_text(f"도서명,저자,출판사,ISBN,정가\n{title},{author},{publisher},,12000\n", encoding="utf-8")

    row = parse_upload(path, path.name)["rows"][0]

    assert row["isbn"] == ""
    assert row["needs_review"] is True
    assert any(missing_label in warning for warning in row["warnings"])
    assert not any("ISBN" in warning for warning in row["warnings"])


@pytest.mark.parametrize("isbn", ["9788937464011", "확인 필요", "9.78894E+12"])
def test_nonempty_invalid_isbn_is_retained_and_blocks_ready_status(tmp_path: Path, isbn):
    path = tmp_path / "추천.csv"
    path.write_text(f"도서명,저자,출판사,ISBN,정가\n책,김작가,책출판,{isbn},12000\n", encoding="utf-8")

    row = parse_upload(path, path.name)["rows"][0]

    assert row["isbn"] == isbn
    assert row["raw_values"]["ISBN"] == isbn
    assert row["needs_review"] is True
    assert any("ISBN" in warning for warning in row["warnings"])


@pytest.mark.parametrize("isbn13", ["", "9788937464010"])
def test_invalid_secondary_isbn_column_is_not_silently_ignored(tmp_path: Path, isbn13):
    path = tmp_path / "추천.csv"
    path.write_text(f"도서명,저자,출판사,ISBN10,ISBN13,정가\n책,김작가,책출판,확인 필요,{isbn13},12000\n", encoding="utf-8")

    row = parse_upload(path, path.name)["rows"][0]

    assert row["isbn"] == (isbn13 or "확인 필요")
    assert row["raw_values"]["ISBN10"] == "확인 필요"
    assert row["needs_review"] is True
    assert any("ISBN" in warning for warning in row["warnings"])


def test_valid_isbn_does_not_require_author_or_publisher(tmp_path: Path):
    path = tmp_path / "추천.csv"
    path.write_text("도서명,ISBN,정가\n책,9788937464010,12000\n", encoding="utf-8")

    row = parse_upload(path, path.name)["rows"][0]

    assert (row["author"], row["publisher"]) == ("", "")
    assert row["needs_review"] is False


@pytest.mark.parametrize(("price", "quantity", "warning_label"), [("", "1", "정가"), ("12000", "여러 권", "수량")])
def test_optional_isbn_does_not_suppress_price_or_quantity_review(tmp_path: Path, price, quantity, warning_label):
    path = tmp_path / "추천.csv"
    path.write_text(f"도서명,저자,출판사,정가,수량\n책,김작가,책출판,{price},{quantity}\n", encoding="utf-8")

    row = parse_upload(path, path.name)["rows"][0]

    assert row["needs_review"] is True
    assert any(warning_label in warning for warning in row["warnings"])
    assert not any("ISBN" in warning for warning in row["warnings"])


def test_optional_isbn_does_not_suppress_workbook_formula_review(tmp_path: Path):
    path = tmp_path / "추천.xlsx"
    workbook = Workbook()
    workbook.active.append(["도서명", "저자", "출판사", "정가", "비고"])
    workbook.active.append(["책", "김작가", "책출판", 12000, "=1+1"])
    workbook.save(path)

    row = parse_upload(path, path.name)["rows"][0]

    assert row["raw_values"]["비고"] == "=1+1"
    assert row["needs_review"] is True
    assert any("수식" in warning for warning in row["warnings"])
    assert not any("ISBN" in warning for warning in row["warnings"])


def test_optional_isbn_does_not_suppress_uncertain_prose_review(tmp_path: Path):
    path = tmp_path / "추천.txt"
    path.write_text("도서명: 책\n저자: 김작가\n출판사: 책출판\n정가: 12000\n", encoding="utf-8")

    row = parse_upload(path, path.name)["rows"][0]

    assert (row["title"], row["author"], row["publisher"], row["price"]) == ("책", "김작가", "책출판", 12000)
    assert row["needs_review"] is True
    assert any("원문" in warning for warning in row["warnings"])
    assert not any("ISBN" in warning for warning in row["warnings"])


def test_complete_ocr_bibliography_without_isbn_still_requires_review(tmp_path: Path, monkeypatch):
    path = tmp_path / "스캔.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.write(path)
    monkeypatch.setattr("suseoro.simple.documents._ocr", lambda *_: "도서명|저자|출판사|정가\n책|김작가|책출판|12000")

    row = parse_upload(path, path.name)["rows"][0]

    assert (row["title"], row["author"], row["publisher"], row["isbn"], row["price"]) == ("책", "김작가", "책출판", "", 12000)
    assert row["needs_review"] is True
    assert row["provenance"]["page"] == 1
    assert row["warnings"] and all("OCR" in warning for warning in row["warnings"])


@pytest.mark.parametrize("extension", ["docx", "hwpx"])
def test_structured_document_table_is_ready_without_isbn(tmp_path: Path, extension):
    path = tmp_path / f"추천.{extension}"
    rows = [["도서명", "저자", "출판사", "정가"], ["책", "김작가", "책출판", "12000"]]
    if extension == "docx":
        table = "".join("<w:tr>" + "".join(f"<w:tc><w:p><w:r><w:t>{cell}</w:t></w:r></w:p></w:tc>" for cell in row) + "</w:tr>" for row in rows)
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("word/document.xml", f'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:tbl>{table}</w:tbl></w:body></w:document>')
            archive.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
    else:
        table = "".join("<hp:tr>" + "".join(f"<hp:tc><hp:p><hp:run><hp:t>{cell}</hp:t></hp:run></hp:p></hp:tc>" for cell in row) + "</hp:tr>" for row in rows)
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("mimetype", "application/hwp+zip")
            archive.writestr("Contents/section0.xml", f'<root xmlns:hp="urn:hwp"><hp:tbl>{table}</hp:tbl></root>')

    result = parse_upload(path, path.name)

    assert len(result["rows"]) == 1
    row = result["rows"][0]
    assert (row["title"], row["author"], row["publisher"], row["isbn"], row["price"]) == ("책", "김작가", "책출판", "", 12000)
    assert row["needs_review"] is False
    assert row["warnings"] == []
