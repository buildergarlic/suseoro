"""Signature- and content-based tabular format and role detection."""

from __future__ import annotations

import codecs
import csv
import io
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from python_calamine import CalamineError, CalamineWorkbook

from suseoro.ingestion.contracts import DocumentRole
from suseoro.ingestion.mapping import infer_mapping

OLE_SIGNATURE = bytes.fromhex("D0CF11E0A1B11AE1")
TEXT_PROBE_BYTES = 64 * 1024
TEXT_PROBE_LOOKAHEAD = 8
PDF_TAIL_PROBE_BYTES = 64 * 1024
PDF_XREF_PROBE_BYTES = 4 * 1024


@dataclass(frozen=True)
class FileDetection:
    format: str
    role: DocumentRole = DocumentRole.UNKNOWN
    role_confidence: float = 0.0
    header_row: int | None = None
    column_confidence: float = 0.0
    encoding: str | None = None
    delimiter: str | None = None
    header_sheet: str | None = None
    column_mapping: dict[str, str | None] = field(default_factory=dict)


def detect_encoding(contents: bytes) -> tuple[str, str]:
    if contents.startswith(codecs.BOM_UTF8):
        return "utf-8-sig", contents.decode("utf-8-sig")
    for encoding in ("utf-8", "cp949", "euc-kr"):
        try:
            return encoding, contents.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("text is not UTF-8, CP949, or EUC-KR")


def _detect_encoding_chunks(chunks: list[bytes], *, final: bool) -> tuple[str, str]:
    prefix = b"".join(chunks)[: len(codecs.BOM_UTF8)]
    encodings = (
        ("utf-8-sig",)
        if prefix.startswith(codecs.BOM_UTF8)
        else (
            "utf-8",
            "cp949",
            "euc-kr",
        )
    )
    for encoding in encodings:
        decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
        decoded: list[str] = []
        try:
            for index, chunk in enumerate(chunks):
                decoded.append(
                    decoder.decode(chunk, final=final and index == len(chunks) - 1)
                )
        except UnicodeDecodeError:
            continue
        return encoding, "".join(decoded)
    raise ValueError("text is not UTF-8, CP949, or EUC-KR")


def detect_delimiter(text: str) -> tuple[str, str]:
    sample = text[:64_000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
        delimiter = dialect.delimiter
    except csv.Error:
        counts = {
            candidate: sample.count(candidate) for candidate in (",", "\t", ";", "|")
        }
        delimiter = max(counts, key=counts.get)
        if counts[delimiter] == 0:
            delimiter = "\t" if "\t" in sample else ","
    format_name = "CSV" if delimiter == "," else "TSV" if delimiter == "\t" else "TXT"
    return delimiter, format_name


def _zip_format(path: Path) -> str | None:
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            if "mimetype" in names:
                mimetype = archive.read("mimetype")[:128]
                if mimetype == b"application/vnd.oasis.opendocument.spreadsheet":
                    return "ODS"
                if mimetype in {
                    b"application/hwp+zip",
                    b"application/vnd.hancom.hwpx",
                }:
                    return "HWPX"
            if "word/document.xml" in names and "[Content_Types].xml" in names:
                return "DOCX"
            if any(
                name.lower().startswith("contents/section")
                and name.lower().endswith(".xml")
                for name in names
            ):
                return "HWPX"
            if "xl/workbook.bin" in names:
                return "XLSB"
            if "xl/workbook.xml" in names:
                return "XLSX"
            if "[Content_Types].xml" in names:
                types = archive.read("[Content_Types].xml")[:256_000]
                if b"sheet.binary" in types or b"workbook.bin" in types:
                    return "XLSB"
                if b"spreadsheetml" in types:
                    return "XLSX"
    except (OSError, RuntimeError, zipfile.BadZipFile, KeyError):
        return None
    return None


def _content_detection(
    rows: list[list[object]],
    *,
    format_name: str,
    encoding: str | None = None,
    delimiter: str | None = None,
    sheet: str | None = None,
) -> FileDetection:
    best_index: int | None = None
    best_confidence = 0.0
    best_fields: set[str] = set()
    best_mapping: dict[str, str | None] = {}
    for index, row in enumerate(rows):
        inferred = infer_mapping([str(value) for value in row], [])
        fields = {field for field in inferred.mapping.values() if field}
        if len(fields) > len(best_fields):
            best_index = index
            best_confidence = inferred.confidence
            best_fields = fields
            best_mapping = inferred.mapping
    role = DocumentRole.UNKNOWN
    role_confidence = 0.0
    if {"registration_number", "call_number"} & best_fields:
        role, role_confidence = DocumentRole.INVENTORY, 0.9
    elif "unit_price" in best_fields:
        role, role_confidence = DocumentRole.VENDOR_QUOTE, 0.8
    elif {"isbn", "title"} <= best_fields:
        role, role_confidence = DocumentRole.PURCHASE_REQUEST, 0.75
    return FileDetection(
        format=format_name,
        role=role,
        role_confidence=role_confidence,
        header_row=None if best_index is None else best_index + 1,
        column_confidence=best_confidence,
        encoding=encoding,
        delimiter=delimiter,
        header_sheet=sheet,
        column_mapping=best_mapping,
    )


def _text_detection(contents: bytes) -> FileDetection:
    encoding, text = detect_encoding(contents)
    delimiter, format_name = detect_delimiter(text)
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))[:25]
    return _content_detection(
        rows,
        format_name=format_name,
        encoding=encoding,
        delimiter=delimiter,
    )


def _workbook_detection(
    path: Path, format_name: str, *, reject_on_error: bool = False
) -> FileDetection:
    try:
        workbook = CalamineWorkbook.from_path(path)
        try:
            best = FileDetection(format_name)
            best_score = 0
            for sheet_name in workbook.sheet_names:
                rows = workbook.get_sheet_by_name(sheet_name).to_python(nrows=25)
                detected = _content_detection(
                    rows,
                    format_name=format_name,
                    sheet=sheet_name,
                )
                score = sum(
                    value is not None for value in detected.column_mapping.values()
                )
                if score > best_score:
                    best, best_score = detected, score
            return best
        finally:
            workbook.close()
    except (CalamineError, OSError, ValueError):
        return FileDetection("UNKNOWN" if reject_on_error else format_name)


def _text_file_detection(path: Path) -> FileDetection:
    with path.open("rb") as source:
        first = source.read(TEXT_PROBE_BYTES)
        lookahead = source.read(TEXT_PROBE_LOOKAHEAD)
        final = len(lookahead) < TEXT_PROBE_LOOKAHEAD
    encoding, text = _detect_encoding_chunks([first, lookahead], final=final)
    delimiter, format_name = detect_delimiter(text)
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))[:25]
    return _content_detection(
        rows,
        format_name=format_name,
        encoding=encoding,
        delimiter=delimiter,
    )


def _is_structural_pdf(path: Path, head: bytes) -> bool:
    if (
        len(head) < 8
        or head[:5] != b"%PDF-"
        or head[5:6] not in {b"1", b"2"}
        or head[6:7] != b"."
        or not head[7:8].isdigit()
    ):
        return False
    try:
        size = path.stat().st_size
        if size < 16:
            return False
        with path.open("rb") as source:
            source.seek(max(0, size - PDF_TAIL_PROBE_BYTES))
            tail = source.read(PDF_TAIL_PROBE_BYTES)
            eof = tail.rfind(b"%%EOF")
            if eof < 0 or tail[eof + len(b"%%EOF") :].strip():
                return False
            marker = tail.rfind(b"startxref", 0, eof)
            if marker < 0:
                return False
            tokens = tail[marker + len(b"startxref") : eof].split()
            if not tokens or not tokens[0].isdigit():
                return False
            xref_offset = int(tokens[0])
            if xref_offset <= 0 or xref_offset >= size:
                return False
            source.seek(xref_offset)
            xref = source.read(PDF_XREF_PROBE_BYTES).lstrip()
    except (OSError, ValueError):
        return False
    if xref.startswith(b"xref"):
        return True
    object_header = xref.split(maxsplit=3)
    return (
        len(object_header) >= 4
        and object_header[0].isdigit()
        and object_header[1].isdigit()
        and object_header[2] == b"obj"
        and b"/Type" in xref
        and b"/XRef" in xref
    )


def detect_file_type(path: Path) -> FileDetection:
    path = Path(path)
    with path.open("rb") as source:
        head = source.read(TEXT_PROBE_BYTES)
    if head.startswith(OLE_SIGNATURE):
        workbook = _workbook_detection(path, "XLS", reject_on_error=True)
        if workbook.format != "UNKNOWN":
            return workbook
        from suseoro.ingestion.parsers.hwp import is_hwp_compound_file

        if is_hwp_compound_file(path):
            return FileDetection("HWP")
        return FileDetection("UNKNOWN")
    if head.startswith(b"%PDF-"):
        return FileDetection("PDF" if _is_structural_pdf(path, head) else "UNKNOWN")
    if head.startswith(b"PK"):
        packaged = _zip_format(path)
        if packaged:
            if packaged in {"DOCX", "HWPX"}:
                return FileDetection(packaged)
            return _workbook_detection(path, packaged)
        return FileDetection("UNKNOWN")
    if len(head) >= 25 and head[:5].isdigit():
        length = int(head[:5])
        if 25 <= length <= 1024 * 1024:
            if len(head) < length:
                with path.open("rb") as source:
                    record = source.read(length)
            else:
                record = head[:length]
            if (
                len(record) == length
                and record[-1:] == b"\x1d"
                and record[10:12].isdigit()
                and record[12:17].isdigit()
                and 24 < int(record[12:17]) < length
                and record[int(record[12:17]) - 1 : int(record[12:17])] == b"\x1e"
            ):
                return FileDetection("MARC")
        if len(head) >= 24 and head[20:24] == b"4500":
            return FileDetection("UNKNOWN")
    if b"\x00" in head:
        return FileDetection("UNKNOWN")
    try:
        return _text_file_detection(path)
    except ValueError:
        return FileDetection("UNKNOWN")
