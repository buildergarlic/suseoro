from __future__ import annotations

import hashlib
import importlib
import importlib.util
import io
import zipfile
from pathlib import Path

from suseoro.ingestion.contracts import DocumentRole, RowStatus
from suseoro.ingestion.detection import detect_file_type
from suseoro.ingestion.file_store import StoredFile


def _module(name: str):
    assert importlib.util.find_spec(name) is not None, f"missing parser module: {name}"
    return importlib.import_module(name)


def _zip(entries: list[tuple[str, bytes]]) -> bytes:
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in entries:
            archive.writestr(name, value)
    return target.getvalue()


def _write(path: Path, contents: bytes) -> str:
    path.write_bytes(contents)
    return hashlib.sha256(contents).hexdigest()


def _docx_bytes() -> bytes:
    document = b"""<?xml version="1.0" encoding="UTF-8"?>
    <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
      <w:body>
        <w:p><w:r><w:t>First paragraph</w:t></w:r></w:p>
        <w:tbl><w:tr>
          <w:tc><w:p><w:r><w:t>=cell formula text</w:t></w:r></w:p></w:tc>
          <w:tc><w:p><w:r><w:t>Second cell</w:t></w:r></w:p></w:tc>
        </w:tr></w:tbl>
        <w:sdt><w:sdtContent><w:p><w:r><w:t>Nested paragraph</w:t></w:r></w:p></w:sdtContent></w:sdt>
        <w:p><w:r><w:br w:type="page"/><w:t>Second page</w:t></w:r></w:p>
      </w:body>
    </w:document>"""
    return _zip(
        [
            (
                "[Content_Types].xml",
                b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
            ),
            ("word/document.xml", document),
        ]
    )


def _hwpx_bytes(*, entity_payload: bool = False) -> bytes:
    section0 = (
        b'<!DOCTYPE hp:sec [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
        b'<hp:sec xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph"><hp:p><hp:run><hp:t>&x;</hp:t></hp:run></hp:p></hp:sec>'
        if entity_payload
        else b"""<hp:sec xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph">
          <hp:p><hp:run><hp:t>Section zero</hp:t></hp:run></hp:p>
          <hp:tbl><hp:tr>
            <hp:tc><hp:subList><hp:p><hp:run><hp:t>Cell one</hp:t></hp:run></hp:p></hp:subList></hp:tc>
            <hp:tc><hp:subList><hp:p><hp:run><hp:t>Cell two</hp:t></hp:run></hp:p></hp:subList></hp:tc>
          </hp:tr></hp:tbl>
        </hp:sec>"""
    )
    return _zip(
        [
            ("mimetype", b"application/hwp+zip"),
            ("Contents/section0.xml", section0),
            (
                "Contents/section1.xml",
                b'<hp:sec xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph"><hp:p><hp:run><hp:t>Section one</hp:t></hp:run></hp:p></hp:sec>',
            ),
        ]
    )


def test_docx_returns_paragraphs_and_cells_in_document_order_with_page_node_provenance(
    tmp_path: Path,
) -> None:
    """Flattening tables, losing page breaks, or omitting a logical node must fail."""
    docx = _module("suseoro.ingestion.parsers.docx")
    target = tmp_path / "renamed.bin"
    digest = _write(target, _docx_bytes())

    result = docx.parse_docx(target, role=DocumentRole.PURCHASE_REQUEST, sha256=digest)

    assert result.detected_format == "DOCX"
    assert [row.fields["text"].value for row in result.rows] == [
        "First paragraph",
        "=cell formula text",
        "Second cell",
        "Nested paragraph",
        "Second page",
    ]
    assert [row.raw_values["kind"] for row in result.rows] == [
        "paragraph",
        "table_cell",
        "table_cell",
        "paragraph",
        "paragraph",
    ]
    assert [row.raw_values["page"] for row in result.rows] == [1, 1, 1, 1, 2]
    assert all(
        "word/document.xml#" in (row.provenance.sheet or "") for row in result.rows
    )
    assert all(row.provenance.source_file_sha256 == digest for row in result.rows)
    assert len(result.rows) == sum(row.status in RowStatus for row in result.rows) == 5
    assert "FORMULA_LIKE_INPUT" in {warning.code for warning in result.rows[1].warnings}


def test_hwpx_returns_sections_and_table_cells_in_package_order(tmp_path: Path) -> None:
    """Sorting sections lexically, duplicating cell paragraphs, or losing nodes must fail."""
    hwpx = _module("suseoro.ingestion.parsers.hwpx")
    target = tmp_path / "document.zip"
    digest = _write(target, _hwpx_bytes())
    stored = StoredFile(
        sha256=digest,
        size=target.stat().st_size,
        path=target,
        detected_format="HWPX",
        created=True,
        original_filename="document.hwpx",
    )

    result = hwpx.parse_hwpx(stored, role=DocumentRole.UNKNOWN)

    assert [row.fields["text"].value for row in result.rows] == [
        "Section zero",
        "Cell one",
        "Cell two",
        "Section one",
    ]
    assert [row.raw_values["section"] for row in result.rows] == [0, 0, 0, 1]
    assert [row.provenance.source_row for row in result.rows] == [1, 2, 3, 1]
    assert all(row.status == RowStatus.SUCCESS for row in result.rows)
    assert len(result.rows) == 4


def test_document_parsers_turn_unsafe_xml_and_broken_archives_into_accounted_errors(
    tmp_path: Path,
) -> None:
    """Resolving XML entities or raising an opaque ZIP/XML exception must fail."""
    docx = _module("suseoro.ingestion.parsers.docx")
    hwpx = _module("suseoro.ingestion.parsers.hwpx")
    broken_docx = tmp_path / "broken.docx"
    broken_digest = _write(broken_docx, b"PK\x03\x04broken")
    entity_hwpx = tmp_path / "entity.hwpx"
    entity_digest = _write(entity_hwpx, _hwpx_bytes(entity_payload=True))

    docx_result = docx.parse_docx(
        broken_docx, role=DocumentRole.UNKNOWN, sha256=broken_digest
    )
    hwpx_result = hwpx.parse_hwpx(
        entity_hwpx, role=DocumentRole.UNKNOWN, sha256=entity_digest
    )

    assert len(docx_result.rows) == 1
    assert docx_result.rows[0].status == RowStatus.ROW_ERROR
    assert docx_result.rows[0].error_code == "DOCUMENT_ARCHIVE_ERROR"
    assert len(hwpx_result.rows) == 2
    assert hwpx_result.rows[0].status == RowStatus.ROW_ERROR
    assert hwpx_result.rows[0].error_code == "DOCUMENT_XML_ERROR"
    assert hwpx_result.rows[0].raw_values["section"] == 0
    assert hwpx_result.rows[1].status == RowStatus.SUCCESS


def test_detection_identifies_docx_and_hwpx_by_package_content_not_suffix(
    tmp_path: Path,
) -> None:
    """Trusting file extensions or classifying document packages as unknown must fail."""
    docx = tmp_path / "doc.zip"
    hwpx = tmp_path / "sheet.docx"
    docx.write_bytes(_docx_bytes())
    hwpx.write_bytes(_hwpx_bytes())

    assert detect_file_type(docx).format == "DOCX"
    assert detect_file_type(hwpx).format == "HWPX"
