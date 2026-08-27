from __future__ import annotations

import hashlib
import importlib
import importlib.util
import struct
import zlib
from pathlib import Path

import pytest

from suseoro.ingestion.contracts import DocumentRole, RowStatus
from suseoro.ingestion.detection import detect_file_type

FREE = 0xFFFFFFFF
END = 0xFFFFFFFE
FAT = 0xFFFFFFFD
NO_STREAM = 0xFFFFFFFF


def _module(name: str):
    assert importlib.util.find_spec(name) is not None, f"missing parser module: {name}"
    return importlib.import_module(name)


def _record(tag: int, payload: bytes, *, level: int = 0) -> bytes:
    if len(payload) < 0xFFF:
        return struct.pack("<I", tag | (level << 10) | (len(payload) << 20)) + payload
    return (
        struct.pack("<I", tag | (level << 10) | (0xFFF << 20))
        + struct.pack("<I", len(payload))
        + payload
    )


def _directory_entry(
    name: str,
    object_type: int,
    *,
    start_sector: int = END,
    size: int = 0,
    right: int = NO_STREAM,
    child: int = NO_STREAM,
) -> bytes:
    entry = bytearray(128)
    encoded_name = (name + "\x00").encode("utf-16le")
    entry[: len(encoded_name)] = encoded_name
    struct.pack_into("<H", entry, 64, len(encoded_name))
    entry[66] = object_type
    entry[67] = 1
    struct.pack_into("<III", entry, 68, NO_STREAM, right, child)
    struct.pack_into("<I", entry, 116, start_sector)
    struct.pack_into("<Q", entry, 120, size)
    return bytes(entry)


def _minimal_hwp(
    *, flags: int = 0, damaged: bool = False, unsupported: bool = True
) -> bytes:
    file_header = bytearray(256)
    file_header[:32] = b"HWP Document File".ljust(32, b"\x00")
    struct.pack_into("<I", file_header, 32, 0x05000300)
    struct.pack_into("<I", file_header, 36, flags)

    section = b"".join(
        [
            _record(67, "Opening paragraph".encode("utf-16le")),
            _record(77, b""),
            _record(72, b"", level=1),
            _record(67, "Cell A".encode("utf-16le"), level=2),
            _record(72, b"", level=1),
            _record(67, "Cell B".encode("utf-16le"), level=2),
            _record(71, b"eqed", level=1) if unsupported else b"",
        ]
    )
    if damaged:
        section += struct.pack("<I", 67 | (100 << 20)) + b"too short"
    if flags & 0x1:
        compressor = zlib.compressobj(level=6, wbits=-15)
        section = compressor.compress(section) + compressor.flush()

    sector_size = 512
    directory_sector = 0
    header_sector = 1
    section_sector = 2
    fat_sector = 3
    directory = b"".join(
        [
            _directory_entry("Root Entry", 5, child=1),
            _directory_entry(
                "FileHeader",
                2,
                start_sector=header_sector,
                size=len(file_header),
                right=2,
            ),
            _directory_entry("BodyText", 1, child=3),
            _directory_entry(
                "Section0", 2, start_sector=section_sector, size=len(section)
            ),
        ]
    )
    fat = [END, END, END, FAT] + [FREE] * (sector_size // 4 - 4)
    header = bytearray(512)
    header[:8] = bytes.fromhex("D0CF11E0A1B11AE1")
    header[8:24] = b"\x00" * 16
    struct.pack_into("<H", header, 24, 0x003E)
    struct.pack_into("<H", header, 26, 0x0003)
    struct.pack_into("<H", header, 28, 0xFFFE)
    struct.pack_into("<H", header, 30, 9)
    struct.pack_into("<H", header, 32, 6)
    struct.pack_into("<I", header, 40, 0)
    struct.pack_into("<I", header, 44, 1)
    struct.pack_into("<I", header, 48, directory_sector)
    struct.pack_into("<I", header, 56, 0)
    struct.pack_into("<I", header, 60, END)
    struct.pack_into("<I", header, 64, 0)
    struct.pack_into("<I", header, 68, END)
    struct.pack_into("<I", header, 72, 0)
    struct.pack_into("<I", header, 76, fat_sector)
    for offset in range(80, 512, 4):
        struct.pack_into("<I", header, offset, FREE)

    def _sector(value: bytes) -> bytes:
        return value.ljust(sector_size, b"\x00")

    return (
        bytes(header)
        + _sector(directory)
        + _sector(bytes(file_header))
        + _sector(section)
        + struct.pack("<" + "I" * len(fat), *fat)
    )


def _write(path: Path, contents: bytes) -> str:
    path.write_bytes(contents)
    return hashlib.sha256(contents).hexdigest()


def _later_section_failure_hwp() -> bytes:
    file_header = bytearray(256)
    file_header[:32] = b"HWP Document File".ljust(32, b"\x00")
    struct.pack_into("<I", file_header, 32, 0x05000300)
    section0 = _record(67, "Section zero survives".encode("utf-16le"))
    directory = b"".join(
        [
            _directory_entry("Root Entry", 5, child=1),
            _directory_entry(
                "FileHeader", 2, start_sector=2, size=len(file_header), right=2
            ),
            _directory_entry("BodyText", 1, child=3),
            _directory_entry(
                "Section0", 2, start_sector=3, size=len(section0), right=4
            ),
            _directory_entry("Section1", 2, start_sector=99, size=16),
        ]
    ).ljust(1024, b"\x00")
    fat = [1, END, END, END, FAT] + [FREE] * (512 // 4 - 5)
    header = bytearray(512)
    header[:8] = bytes.fromhex("D0CF11E0A1B11AE1")
    struct.pack_into("<H", header, 24, 0x003E)
    struct.pack_into("<H", header, 26, 0x0003)
    struct.pack_into("<H", header, 28, 0xFFFE)
    struct.pack_into("<H", header, 30, 9)
    struct.pack_into("<H", header, 32, 6)
    struct.pack_into("<I", header, 44, 1)
    struct.pack_into("<I", header, 48, 0)
    struct.pack_into("<I", header, 56, 0)
    struct.pack_into("<I", header, 60, END)
    struct.pack_into("<I", header, 68, END)
    struct.pack_into("<I", header, 76, 4)
    for offset in range(80, 512, 4):
        struct.pack_into("<I", header, offset, FREE)
    return (
        bytes(header)
        + directory
        + bytes(file_header).ljust(512, b"\x00")
        + section0.ljust(512, b"\x00")
        + struct.pack("<128I", *fat)
    )


def _deep_directory_hwp(node_count: int = 1100) -> bytes:
    file_header = bytearray(256)
    file_header[:32] = b"HWP Document File".ljust(32, b"\x00")
    struct.pack_into("<I", file_header, 32, 0x05000300)
    section = _record(67, "Never reached".encode("utf-16le"))
    directory_sectors = (node_count + 3) // 4
    file_header_sector = directory_sectors
    section_sector = directory_sectors + 1
    entries = [
        _directory_entry("Root Entry", 5, child=1),
        _directory_entry(
            "FileHeader",
            2,
            start_sector=file_header_sector,
            size=len(file_header),
            right=2,
        ),
        _directory_entry("BodyText", 1, child=3, right=4),
        _directory_entry("Section0", 2, start_sector=section_sector, size=len(section)),
    ]
    for entry_id in range(4, node_count):
        entries.append(
            _directory_entry(
                f"N{entry_id:04d}",
                1,
                right=entry_id + 1 if entry_id + 1 < node_count else NO_STREAM,
            )
        )
    directory = b"".join(entries).ljust(directory_sectors * 512, b"\x00")
    non_fat_sectors = directory_sectors + 2
    fat_sector_count = 1
    while non_fat_sectors + fat_sector_count > fat_sector_count * 128:
        fat_sector_count += 1
    fat_sector_ids = list(range(non_fat_sectors, non_fat_sectors + fat_sector_count))
    fat_entries = [FREE] * (fat_sector_count * 128)
    for sector in range(directory_sectors):
        fat_entries[sector] = sector + 1 if sector + 1 < directory_sectors else END
    fat_entries[file_header_sector] = END
    fat_entries[section_sector] = END
    for sector in fat_sector_ids:
        fat_entries[sector] = FAT
    header = bytearray(512)
    header[:8] = bytes.fromhex("D0CF11E0A1B11AE1")
    struct.pack_into("<H", header, 24, 0x003E)
    struct.pack_into("<H", header, 26, 0x0003)
    struct.pack_into("<H", header, 28, 0xFFFE)
    struct.pack_into("<H", header, 30, 9)
    struct.pack_into("<H", header, 32, 6)
    struct.pack_into("<I", header, 44, fat_sector_count)
    struct.pack_into("<I", header, 48, 0)
    struct.pack_into("<I", header, 56, 0)
    struct.pack_into("<I", header, 60, END)
    struct.pack_into("<I", header, 68, END)
    for index, sector in enumerate(fat_sector_ids):
        struct.pack_into("<I", header, 76 + index * 4, sector)
    for offset in range(76 + len(fat_sector_ids) * 4, 512, 4):
        struct.pack_into("<I", header, offset, FREE)
    return (
        bytes(header)
        + directory
        + bytes(file_header).ljust(512, b"\x00")
        + section.ljust(512, b"\x00")
        + struct.pack(f"<{len(fat_entries)}I", *fat_entries)
    )


def test_hwp_reads_ole_file_header_sections_records_and_table_cells_in_order(
    tmp_path: Path,
) -> None:
    """Ignoring CFB structure, record order, cell context, or unsupported objects must fail."""
    hwp = _module("suseoro.ingestion.parsers.hwp")
    target = tmp_path / "renamed.ole"
    digest = _write(target, _minimal_hwp())

    result = hwp.parse_hwp(target, role=DocumentRole.UNKNOWN, sha256=digest)

    assert result.detected_format == "HWP"
    assert [row.fields.get("text").value for row in result.rows[:3]] == [
        "Opening paragraph",
        "Cell A",
        "Cell B",
    ]
    assert [row.raw_values["cell"] for row in result.rows[:3]] == [None, 1, 2]
    assert result.rows[3].status == RowStatus.ROW_ERROR
    assert result.rows[3].error_code == "HWP_UNSUPPORTED_OBJECT"
    assert result.rows[3].raw_values["record"] == 7
    assert all(row.raw_values["stream"] == "BodyText/Section0" for row in result.rows)
    assert all(row.provenance.source_file_sha256 == digest for row in result.rows)
    assert len(result.rows) == 4


def test_hwp_supports_raw_deflate_sections_and_accounts_damaged_record(
    tmp_path: Path,
) -> None:
    """Skipping the compression flag or swallowing a truncated record must fail."""
    hwp = _module("suseoro.ingestion.parsers.hwp")
    compressed = tmp_path / "compressed.hwp"
    compressed_digest = _write(compressed, _minimal_hwp(flags=0x1, unsupported=False))
    damaged = tmp_path / "damaged.hwp"
    damaged_digest = _write(damaged, _minimal_hwp(damaged=True, unsupported=False))

    compressed_result = hwp.parse_hwp(
        compressed, role=DocumentRole.UNKNOWN, sha256=compressed_digest
    )
    damaged_result = hwp.parse_hwp(
        damaged, role=DocumentRole.UNKNOWN, sha256=damaged_digest
    )

    assert [row.fields["text"].value for row in compressed_result.rows] == [
        "Opening paragraph",
        "Cell A",
        "Cell B",
    ]
    assert len(damaged_result.rows) == 4
    assert damaged_result.rows[-1].status == RowStatus.ROW_ERROR
    assert damaged_result.rows[-1].error_code == "HWP_DAMAGED_RECORD"
    assert damaged_result.rows[-1].raw_values["record"] == 7


def test_hwp_later_section_read_failure_is_positioned_without_losing_prior_rows(
    tmp_path: Path,
) -> None:
    """Dropping a failed later section after a valid earlier section must fail."""
    hwp = _module("suseoro.ingestion.parsers.hwp")
    target = tmp_path / "later-section.hwp"
    digest = _write(target, _later_section_failure_hwp())

    result = hwp.parse_hwp(target, role=DocumentRole.UNKNOWN, sha256=digest)

    assert len(result.rows) == 2
    assert result.rows[0].fields["text"].value == "Section zero survives"
    assert result.rows[1].status == RowStatus.ROW_ERROR
    assert result.rows[1].error_code == "HWP_DAMAGED"
    assert result.rows[1].raw_values["stream"] == "BodyText/Section1"
    assert result.rows[1].raw_values["section"] == 1
    assert result.rows[1].provenance.source_file_sha256 == digest


def test_hwp_compressed_section_is_bounded_after_expansion(
    tmp_path: Path, monkeypatch
) -> None:
    """Allowing a small compressed BodyText stream to expand without a cap must fail."""
    hwp = _module("suseoro.ingestion.parsers.hwp")
    target = tmp_path / "expansion.hwp"
    digest = _write(target, _minimal_hwp(flags=0x1, unsupported=False))
    monkeypatch.setattr(hwp, "MAX_DECOMPRESSED_SECTION_BYTES", 64)

    result = hwp.parse_hwp(target, role=DocumentRole.UNKNOWN, sha256=digest)

    assert len(result.rows) == 1
    assert result.rows[0].status == RowStatus.ROW_ERROR
    assert result.rows[0].error_code == "HWP_DAMAGED"
    assert result.rows[0].raw_values["stream"] == "BodyText/Section0"


def test_hwp_rejects_encryption_and_invalid_compound_files_with_conversion_guidance(
    tmp_path: Path,
) -> None:
    """Attempting encrypted bytes or raising without structured conversion guidance must fail."""
    hwp = _module("suseoro.ingestion.parsers.hwp")
    encrypted = tmp_path / "encrypted.hwp"
    encrypted_digest = _write(encrypted, _minimal_hwp(flags=0x2))
    broken = tmp_path / "broken.hwp"
    broken_digest = _write(broken, bytes.fromhex("D0CF11E0A1B11AE1") + b"broken")

    encrypted_result = hwp.parse_hwp(
        encrypted, role=DocumentRole.UNKNOWN, sha256=encrypted_digest
    )
    broken_result = hwp.parse_hwp(
        broken, role=DocumentRole.UNKNOWN, sha256=broken_digest
    )

    assert encrypted_result.rows[0].error_code == "HWP_ENCRYPTED"
    assert "HWPX" in (encrypted_result.rows[0].error_message or "")
    assert broken_result.rows[0].error_code == "HWP_DAMAGED"
    assert "HWPX" in (broken_result.rows[0].error_message or "")


def test_hwp_detection_requires_compound_file_and_hwp_stream_names(
    tmp_path: Path,
) -> None:
    """Treating every OLE file as HWP or relying on its suffix must fail."""
    hwp_file = tmp_path / "document.bin"
    hwp_file.write_bytes(_minimal_hwp())
    spoof = tmp_path / "spoof.hwp"
    spoof.write_bytes(bytes.fromhex("D0CF11E0A1B11AE1") + b"\x00" * 600)

    assert detect_file_type(hwp_file).format == "HWP"
    assert detect_file_type(spoof).format == "UNKNOWN"


def test_hwp_rejects_deep_directory_tree_without_recursion_escape(
    tmp_path: Path,
) -> None:
    """Unbounded recursive directory traversal or accepting an oversized tree must fail."""
    hwp = _module("suseoro.ingestion.parsers.hwp")
    target = tmp_path / "deep-tree.hwp"
    digest = _write(target, _deep_directory_hwp())

    try:
        result = hwp.parse_hwp(target, role=DocumentRole.UNKNOWN, sha256=digest)
        detected = detect_file_type(target)
    except RecursionError as error:
        pytest.fail(f"attacker-controlled CFB traversal escaped: {error}")

    assert len(result.rows) == 1
    assert result.rows[0].status == RowStatus.ROW_ERROR
    assert result.rows[0].error_code == "HWP_DAMAGED"
    assert result.rows[0].provenance.source_file_sha256 == digest
    assert detected.format == "UNKNOWN"
