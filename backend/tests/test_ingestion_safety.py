from __future__ import annotations

import hashlib
import io
import sqlite3
import struct
import zipfile
from pathlib import Path

import pytest
import xlwt
from openpyxl import Workbook

from suseoro.config import Settings
from suseoro.db.connection import connect
from suseoro.db.migrations import apply_migrations
from suseoro.ingestion.contracts import DocumentRole
from suseoro.ingestion.detection import detect_file_type
from suseoro.ingestion.file_store import (
    FileTooLarge,
    ImmutableFileStore,
    UnsupportedFileType,
)
from suseoro.ingestion.parsers.tabular import parse_tabular
from suseoro.ingestion.safety import (
    ArchiveSafetyError,
    XmlSafetyError,
    inspect_zip,
    safe_xml_from_bytes,
)
from suseoro.ingestion.templates import ParserCache

SOURCE_ID = "550e8400-e29b-41d4-a716-446655440350"
NOW = "2026-08-28T12:34:56Z"


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, contents in entries.items():
            archive.writestr(name, contents)
    return target.getvalue()


def _set_encrypted_flag(archive: bytes) -> bytes:
    """Set ZIP encryption flags without needing an encryption-writing dependency."""
    value = bytearray(archive)
    local = value.index(b"PK\x03\x04")
    central = value.index(b"PK\x01\x02")
    struct.pack_into(
        "<H", value, local + 6, struct.unpack_from("<H", value, local + 6)[0] | 1
    )
    struct.pack_into(
        "<H", value, central + 8, struct.unpack_from("<H", value, central + 8)[0] | 1
    )
    return bytes(value)


def test_streaming_store_hashes_atomically_and_reuses_immutable_original(
    data_dir: Path,
) -> None:
    """Buffering, replacing, or duplicating an identical original must fail this test."""
    store = ImmutableFileStore(Settings(data_dir=data_dir).sources_dir, max_bytes=64)
    chunks = [b"ISBN,", b"title\n", b"9781,", b"Book\n"]

    first = store.store(chunks, filename="misleading.xlsx")
    second = store.store(iter(chunks), filename="again.csv")

    expected = hashlib.sha256(b"".join(chunks)).hexdigest()
    assert first.sha256 == expected
    assert first.size == len(b"".join(chunks))
    assert first.path.read_bytes() == b"".join(chunks)
    assert second.path == first.path
    assert second.created is False
    assert not list(store.root.rglob("*.tmp"))


def test_streaming_store_rejects_limit_without_leaving_partial_file(
    data_dir: Path,
) -> None:
    """Writing any bytes past the configured ceiling or retaining a partial must fail."""
    store = ImmutableFileStore(Settings(data_dir=data_dir).sources_dir, max_bytes=8)

    with pytest.raises(FileTooLarge):
        store.store([b"1234", b"56789"], filename="large.csv")

    assert not [path for path in store.root.rglob("*") if path.is_file()]


def test_streaming_store_rejects_unknown_binary_signature(data_dir: Path) -> None:
    """Publishing bytes outside the explicit allowlist must fail this test."""
    store = ImmutableFileStore(Settings(data_dir=data_dir).sources_dir)

    with pytest.raises(UnsupportedFileType):
        store.store([b"\x00\x01\x02\x03not-tabular"], filename="payload.csv")

    assert not [path for path in store.root.rglob("*") if path.is_file()]


def test_detection_uses_signature_and_content_instead_of_extension(
    tmp_path: Path,
) -> None:
    """Trusting a spoofed suffix rather than bytes must fail this test."""
    csv_file = tmp_path / "books.xlsx"
    csv_file.write_bytes("ISBN,제목\n978123,책\n".encode())
    xlsx_file = tmp_path / "books.txt"
    xlsx_file.write_bytes(
        _zip_bytes(
            {
                "[Content_Types].xml": b"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
                "xl/workbook.xml": b"<workbook/>",
            }
        )
    )
    xlsb_file = tmp_path / "books.bin"
    xlsb_file.write_bytes(
        _zip_bytes(
            {
                "[Content_Types].xml": b"application/vnd.ms-excel.sheet.binary.macroEnabled.main",
                "xl/workbook.bin": b"\x00\x01",
            }
        )
    )
    ods_file = tmp_path / "books.zip"
    ods_file.write_bytes(
        _zip_bytes({"mimetype": b"application/vnd.oasis.opendocument.spreadsheet"})
    )
    xls_file = tmp_path / "legacy.any"
    xls_file.write_bytes(bytes.fromhex("D0CF11E0A1B11AE1") + b"\x00" * 32)

    assert detect_file_type(csv_file).format == "CSV"
    assert detect_file_type(xlsx_file).format == "XLSX"
    assert detect_file_type(xlsb_file).format == "XLSB"
    assert detect_file_type(ods_file).format == "ODS"
    assert detect_file_type(xls_file).format == "UNKNOWN"


@pytest.mark.parametrize(
    "payload",
    [
        bytes.fromhex("D0CF11E0A1B11AE1") + b"\x00" * 504,
        bytes.fromhex("D0CF11E0A1B11AE1") + b"WordDocument" + b"\x00" * 492,
        bytes.fromhex("D0CF11E0A1B11AE1") + b"PowerPoint Document" + b"\x00" * 484,
    ],
)
def test_ole_signature_without_a_structural_xls_workbook_is_rejected(
    data_dir: Path, payload: bytes
) -> None:
    """Treating arbitrary CFB/DOC/PPT-like bytes as XLS must fail this test."""
    target = data_dir / "spoofed.xls"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)

    assert detect_file_type(target).format == "UNKNOWN"
    store = ImmutableFileStore(Settings(data_dir=data_dir).sources_dir)
    with pytest.raises(UnsupportedFileType):
        store.store([payload], filename="spoofed.xls")


def test_real_xls_is_structurally_recognized_and_allowed(data_dir: Path) -> None:
    """Rejecting a real CFB workbook while tightening OLE checks must fail."""
    target = data_dir / "real.xls"
    target.parent.mkdir(parents=True, exist_ok=True)
    workbook = xlwt.Workbook()
    sheet = workbook.add_sheet("Books")
    sheet.write(0, 0, "ISBN")
    sheet.write(0, 1, "제목")
    sheet.write(1, 0, "00123")
    sheet.write(1, 1, "책")
    workbook.save(str(target))
    payload = target.read_bytes()

    assert detect_file_type(target).format == "XLS"
    stored = ImmutableFileStore(Settings(data_dir=data_dir).sources_dir).store(
        [payload], filename="renamed.doc"
    )
    assert stored.detected_format == "XLS"


def test_workbook_detection_probes_content_for_role_header_and_column_confidence(
    tmp_path: Path,
) -> None:
    """Returning signature only for a workbook must fail this test."""
    target = tmp_path / "quote.bin"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["2026년 공급 견적"])
    sheet.append([])
    sheet.append(["ISBN", "도서명", "수량", "단가"])
    sheet.append(["00123", "책", 2, 1000])
    workbook.save(target)

    detected = detect_file_type(target)

    assert detected.format == "XLSX"
    assert detected.role == DocumentRole.VENDOR_QUOTE
    assert detected.header_row == 3
    assert detected.column_confidence == 1.0
    assert detected.column_mapping == {
        "ISBN": "isbn",
        "도서명": "title",
        "수량": "quantity",
        "단가": "unit_price",
    }


@pytest.mark.parametrize(
    "unsafe_name", ["../escape.xml", "/absolute.xml", "C:/drive.xml"]
)
def test_zip_path_traversal_is_rejected(tmp_path: Path, unsafe_name: str) -> None:
    """Accepting an archive member outside its virtual root must fail this test."""
    target = tmp_path / "unsafe.zip"
    target.write_bytes(_zip_bytes({unsafe_name: b"x"}))

    with pytest.raises(ArchiveSafetyError, match="path"):
        inspect_zip(target)


def test_encrypted_zip_flag_is_rejected(tmp_path: Path) -> None:
    """Deferring encrypted content to a workbook reader must fail this test."""
    target = tmp_path / "encrypted.xlsx"
    target.write_bytes(_set_encrypted_flag(_zip_bytes({"xl/workbook.xml": b"<x/>"})))

    with pytest.raises(ArchiveSafetyError, match="encrypted"):
        inspect_zip(target)


def test_store_rejects_unsafe_zip_even_when_package_type_is_unknown(
    data_dir: Path,
) -> None:
    """Letting content detection bypass archive safety before publication must fail."""
    store = ImmutableFileStore(Settings(data_dir=data_dir).sources_dir)
    payload = _set_encrypted_flag(_zip_bytes({"unknown.bin": b"payload"}))

    with pytest.raises(ArchiveSafetyError, match="encrypted"):
        store.store([payload], filename="unknown.xlsx")

    assert not [path for path in store.root.rglob("*") if path.is_file()]


def test_zip_bomb_metadata_and_entry_limits_are_rejected(tmp_path: Path) -> None:
    """Ignoring ratio, expanded-size, or entry-count ceilings must fail this test."""
    ratio_bomb = tmp_path / "ratio.zip"
    ratio_bomb.write_bytes(_zip_bytes({"huge.xml": b"0" * 100_000}))
    many = tmp_path / "many.zip"
    many.write_bytes(_zip_bytes({f"{number}.xml": b"x" for number in range(4)}))

    with pytest.raises(ArchiveSafetyError, match="ratio"):
        inspect_zip(ratio_bomb, max_compression_ratio=10)
    with pytest.raises(ArchiveSafetyError, match="expanded"):
        inspect_zip(ratio_bomb, max_expanded_bytes=10_000, max_compression_ratio=10_000)
    with pytest.raises(ArchiveSafetyError, match="entries"):
        inspect_zip(many, max_entries=3)


def test_xml_parser_forbids_dtd_and_external_entities() -> None:
    """Resolving or accepting a DTD/entity payload must fail this test."""
    payload = (
        b'<!DOCTYPE x [<!ENTITY probe SYSTEM "file:///etc/passwd">]><x>&probe;</x>'
    )

    with pytest.raises(XmlSafetyError):
        safe_xml_from_bytes(payload)


def test_ingestion_migration_has_cache_provenance_indexes_and_validates_ids(
    data_dir: Path,
) -> None:
    """Missing tables/indexes or accepting noncanonical identifiers must fail."""
    settings = Settings(data_dir=data_dir)
    with connect(settings.database_path) as connection:
        apply_migrations(connection)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        with pytest.raises(sqlite3.IntegrityError, match="identifier"):
            connection.execute(
                """
                INSERT INTO source_files (
                    id, sha256, size_bytes, storage_path, detected_format, created_at
                ) VALUES ('not-a-uuid', ?, 1, 'x', 'CSV', ?)
                """,
                ("0" * 64, "2026-08-28T12:00:00Z"),
            )

    assert {
        "source_files",
        "source_documents",
        "source_rows",
        "parser_runs",
        "mapping_templates",
    } <= tables
    assert {
        "idx_parser_runs_cache",
        "idx_source_rows_provenance",
        "idx_source_documents_unique_parse",
        "idx_source_rows_unique_provenance",
    } <= indexes


def test_forward_ingestion_integrity_migration_enforces_source_and_provenance(
    data_dir: Path,
) -> None:
    """Orphan parser runs or duplicate logical provenance must fail in SQLite."""
    settings = Settings(data_dir=data_dir)
    with connect(settings.database_path) as connection:
        apply_migrations(connection)
        with pytest.raises(sqlite3.IntegrityError, match="source file"):
            connection.execute(
                """
                INSERT INTO parser_runs (
                    id, source_file_sha256, parser_version, role, status, created_at
                ) VALUES (?, ?, 'v1', 'UNKNOWN', 'PENDING', ?)
                """,
                ("550e8400-e29b-41d4-a716-446655440351", "a" * 64, NOW),
            )

        connection.execute(
            """
            INSERT INTO source_files (
                id, sha256, size_bytes, storage_path, detected_format, created_at
            ) VALUES (?, ?, 1, 'stored', 'CSV', ?)
            """,
            (SOURCE_ID, "a" * 64, NOW),
        )
        connection.execute(
            """
            INSERT INTO parser_runs (
                id, source_file_sha256, parser_version, role, status, created_at
            ) VALUES (?, ?, 'v1', 'UNKNOWN', 'PENDING', ?)
            """,
            ("550e8400-e29b-41d4-a716-446655440355", "a" * 64, NOW),
        )
        with pytest.raises(sqlite3.IntegrityError, match="parser runs"):
            connection.execute("DELETE FROM source_files WHERE id = ?", (SOURCE_ID,))
        with pytest.raises(sqlite3.IntegrityError, match="parser runs"):
            connection.execute(
                "UPDATE source_files SET sha256 = ? WHERE id = ?",
                ("b" * 64, SOURCE_ID),
            )
        document_id = "550e8400-e29b-41d4-a716-446655440352"
        connection.execute(
            """
            INSERT INTO source_documents (
                id, source_file_id, role, parser_version, status,
                detected_format, created_at
            ) VALUES (?, ?, 'UNKNOWN', 'v1', 'SUCCESS', 'CSV', ?)
            """,
            (document_id, SOURCE_ID, NOW),
        )
        connection.execute(
            """
            INSERT INTO source_rows (
                id, source_document_id, sheet_name, source_row, status,
                raw_json, fields_json, created_at
            ) VALUES (?, ?, 'Sheet1', 2, 'SUCCESS', '{}', '{}', ?)
            """,
            ("550e8400-e29b-41d4-a716-446655440353", document_id, NOW),
        )
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            connection.execute(
                """
                INSERT INTO source_rows (
                    id, source_document_id, sheet_name, source_row, status,
                    raw_json, fields_json, created_at
                ) VALUES (?, ?, 'Sheet1', 2, 'ROW_ERROR', '{}', '{}', ?)
                """,
                ("550e8400-e29b-41d4-a716-446655440354", document_id, NOW),
            )


def test_rejected_second_upload_cannot_damage_committed_first_source(
    data_dir: Path,
) -> None:
    """Rolling back or deleting prior source/cache state on later rejection must fail."""
    settings = Settings(data_dir=data_dir)
    store = ImmutableFileStore(settings.sources_dir)
    first = store.store(["ISBN,제목\n00123,첫 책\n".encode()], filename="first.csv")
    with connect(settings.database_path) as connection:
        apply_migrations(connection)
        connection.execute(
            """
            INSERT INTO source_files (
                id, sha256, size_bytes, storage_path, original_filename,
                detected_format, created_at
            ) VALUES (?, ?, ?, ?, 'first.csv', 'CSV', ?)
            """,
            (SOURCE_ID, first.sha256, first.size, str(first.path), NOW),
        )
        parsed = parse_tabular(first, role=DocumentRole.PURCHASE_REQUEST)
        ParserCache(connection).get_or_parse(
            sha256=first.sha256,
            parser_version="tabular-v1",
            role=DocumentRole.PURCHASE_REQUEST,
            parse=lambda: {"rows": len(parsed.rows)},
        )
        connection.commit()

        with pytest.raises(UnsupportedFileType):
            store.store([b"\x00bogus-second"], filename="second.csv")

        cached = connection.execute(
            "SELECT status, result_json FROM parser_runs WHERE source_file_sha256 = ?",
            (first.sha256,),
        ).fetchone()

    assert first.path.read_bytes() == "ISBN,제목\n00123,첫 책\n".encode()
    assert parsed.rows[0].fields["isbn"].value == "00123"
    assert parsed.rows[0].provenance.source_file_sha256 == first.sha256
    assert cached["status"] == "SUCCESS"
