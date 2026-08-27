"""Bounded in-process reader for OLE HWP 5.x paragraph records."""

from __future__ import annotations

import re
import struct
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from suseoro.ingestion.contracts import (
    DocumentRole,
    FieldWarning,
    ParsedField,
    ParsedRow,
    ParseResult,
    Provenance,
    RowStatus,
)
from suseoro.ingestion.file_store import StoredFile
from suseoro.ingestion.parsers.tabular import _source_path_and_sha256

PARSER_VERSION = "hwp-v1"
OLE_SIGNATURE = bytes.fromhex("D0CF11E0A1B11AE1")
FREE_SECTOR = 0xFFFFFFFF
END_OF_CHAIN = 0xFFFFFFFE
FAT_SECTOR = 0xFFFFFFFD
DI_FAT_SECTOR = 0xFFFFFFFC
NO_STREAM = 0xFFFFFFFF
MAX_STREAM_BYTES = 64 * 1024 * 1024
MAX_DECOMPRESSED_SECTION_BYTES = 64 * 1024 * 1024
MAX_DIRECTORY_BYTES = 16 * 1024 * 1024
MAX_CHAIN_SECTORS = 131_072

HWPTAG_PARA_HEADER = 66
HWPTAG_PARA_TEXT = 67
HWPTAG_CTRL_HEADER = 71
HWPTAG_LIST_HEADER = 72
HWPTAG_TABLE = 77
_SUPPORTED_CONTROL_IDS = {b"tbl ", b"secd", b"cold", b"head", b"foot"}
_FORMULA_MARKERS = ("=", "+", "-", "@")


class HwpStructureError(ValueError):
    pass


@dataclass(frozen=True)
class _DirectoryEntry:
    name: str
    object_type: int
    left: int
    right: int
    child: int
    start_sector: int
    size: int


class _CompoundFile:
    """Read only the CFB sectors required by named streams."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.source = self.path.open("rb")
        self.file_size = self.path.stat().st_size
        header = self.source.read(512)
        if len(header) != 512 or header[:8] != OLE_SIGNATURE:
            raise HwpStructureError("invalid OLE compound-file signature or header")
        if struct.unpack_from("<H", header, 28)[0] != 0xFFFE:
            raise HwpStructureError("unsupported OLE byte order")
        sector_shift = struct.unpack_from("<H", header, 30)[0]
        mini_sector_shift = struct.unpack_from("<H", header, 32)[0]
        if sector_shift not in {9, 12} or mini_sector_shift != 6:
            raise HwpStructureError("unsupported OLE sector geometry")
        self.sector_size = 1 << sector_shift
        self.mini_sector_size = 1 << mini_sector_shift
        self.sector_count = max(0, self.file_size // self.sector_size - 1)
        self.first_directory_sector = struct.unpack_from("<I", header, 48)[0]
        self.mini_stream_cutoff = struct.unpack_from("<I", header, 56)[0]
        self.first_mini_fat_sector = struct.unpack_from("<I", header, 60)[0]
        self.number_of_mini_fat_sectors = struct.unpack_from("<I", header, 64)[0]
        first_difat_sector = struct.unpack_from("<I", header, 68)[0]
        number_of_difat_sectors = struct.unpack_from("<I", header, 72)[0]
        number_of_fat_sectors = struct.unpack_from("<I", header, 44)[0]
        if (
            number_of_fat_sectors > self.sector_count
            or number_of_difat_sectors > self.sector_count
            or self.number_of_mini_fat_sectors > self.sector_count
        ):
            raise HwpStructureError("OLE allocation-table count exceeds file bounds")

        fat_sectors = [
            value
            for value in struct.unpack_from("<109I", header, 76)
            if value not in {FREE_SECTOR, END_OF_CHAIN}
        ]
        next_difat = first_difat_sector
        for _ in range(number_of_difat_sectors):
            values = struct.unpack(
                "<" + "I" * (self.sector_size // 4), self._read_sector(next_difat)
            )
            fat_sectors.extend(
                value
                for value in values[:-1]
                if value not in {FREE_SECTOR, END_OF_CHAIN}
            )
            next_difat = values[-1]
            if next_difat == END_OF_CHAIN:
                break
        if len(fat_sectors) < number_of_fat_sectors:
            raise HwpStructureError("OLE FAT sector list is incomplete")
        fat_sectors = fat_sectors[:number_of_fat_sectors]
        fat_values: list[int] = []
        for sector in fat_sectors:
            fat_values.extend(
                struct.unpack(
                    "<" + "I" * (self.sector_size // 4), self._read_sector(sector)
                )
            )
        self.fat = fat_values
        directory_bytes = self._read_chain(
            self.first_directory_sector,
            self.fat,
            maximum_sectors=MAX_DIRECTORY_BYTES // self.sector_size,
        )
        self.entries = self._directory_entries(directory_bytes)
        if not self.entries or self.entries[0].object_type != 5:
            raise HwpStructureError("OLE root directory entry is missing")
        self.paths: dict[str, _DirectoryEntry] = {}
        self._walk_sibling_tree(self.entries[0].child, "", set())

        self.mini_fat: list[int] = []
        if self.number_of_mini_fat_sectors:
            mini_fat_bytes = self._read_chain(
                self.first_mini_fat_sector,
                self.fat,
                maximum_sectors=self.number_of_mini_fat_sectors,
            )
            self.mini_fat = list(
                struct.unpack("<" + "I" * (len(mini_fat_bytes) // 4), mini_fat_bytes)
            )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.source.close()

    def _read_sector(self, sector: int) -> bytes:
        if sector >= self.sector_count or sector < 0:
            raise HwpStructureError(f"OLE sector {sector} is outside the file")
        self.source.seek((sector + 1) * self.sector_size)
        value = self.source.read(self.sector_size)
        if len(value) != self.sector_size:
            raise HwpStructureError(f"OLE sector {sector} is truncated")
        return value

    def _chain(
        self, start: int, table: list[int], *, maximum_sectors: int = MAX_CHAIN_SECTORS
    ) -> list[int]:
        if start in {END_OF_CHAIN, FREE_SECTOR, NO_STREAM}:
            return []
        chain: list[int] = []
        seen: set[int] = set()
        current = start
        while current != END_OF_CHAIN:
            if current in seen or current >= len(table):
                raise HwpStructureError("OLE sector chain is cyclic or out of bounds")
            if len(chain) >= maximum_sectors:
                raise HwpStructureError("OLE sector chain exceeds safety limit")
            seen.add(current)
            chain.append(current)
            current = table[current]
            if current in {FREE_SECTOR, FAT_SECTOR, DI_FAT_SECTOR}:
                raise HwpStructureError("OLE stream chain points to a reserved sector")
        return chain

    def _read_chain(
        self, start: int, table: list[int], *, maximum_sectors: int = MAX_CHAIN_SECTORS
    ) -> bytes:
        return b"".join(
            self._read_sector(sector)
            for sector in self._chain(start, table, maximum_sectors=maximum_sectors)
        )

    @staticmethod
    def _directory_entries(contents: bytes) -> list[_DirectoryEntry]:
        entries: list[_DirectoryEntry] = []
        for offset in range(0, len(contents) - 127, 128):
            raw = contents[offset : offset + 128]
            name_length = struct.unpack_from("<H", raw, 64)[0]
            if name_length > 64 or name_length % 2:
                raise HwpStructureError("invalid OLE directory name length")
            name = (
                raw[: max(0, name_length - 2)].decode("utf-16le", errors="strict")
                if name_length >= 2
                else ""
            )
            left, right, child = struct.unpack_from("<III", raw, 68)
            entries.append(
                _DirectoryEntry(
                    name=name,
                    object_type=raw[66],
                    left=left,
                    right=right,
                    child=child,
                    start_sector=struct.unpack_from("<I", raw, 116)[0],
                    size=struct.unpack_from("<Q", raw, 120)[0],
                )
            )
        return entries

    def _walk_sibling_tree(self, entry_id: int, parent: str, active: set[int]) -> None:
        if entry_id == NO_STREAM:
            return
        if entry_id >= len(self.entries) or entry_id in active:
            raise HwpStructureError("invalid OLE directory tree")
        active.add(entry_id)
        entry = self.entries[entry_id]
        self._walk_sibling_tree(entry.left, parent, active)
        path = f"{parent}/{entry.name}" if parent else entry.name
        if entry.object_type in {1, 2}:
            self.paths[path] = entry
        if entry.object_type == 1:
            self._walk_sibling_tree(entry.child, path, active)
        self._walk_sibling_tree(entry.right, parent, active)
        active.remove(entry_id)

    def read_stream(self, name: str) -> bytes:
        entry = self.paths.get(name)
        if entry is None or entry.object_type != 2:
            raise HwpStructureError(f"required HWP stream is missing: {name}")
        if entry.size > MAX_STREAM_BYTES:
            raise HwpStructureError(f"HWP stream exceeds safety limit: {name}")
        if entry.size == 0:
            return b""
        if self.mini_stream_cutoff and entry.size < self.mini_stream_cutoff:
            if not self.mini_fat:
                raise HwpStructureError("OLE mini FAT is missing")
            root = self.entries[0]
            if root.size > MAX_STREAM_BYTES:
                raise HwpStructureError("OLE root mini stream exceeds safety limit")
            root_stream = self._read_chain(root.start_sector, self.fat)[: root.size]
            chunks: list[bytes] = []
            for mini_sector in self._chain(entry.start_sector, self.mini_fat):
                offset = mini_sector * self.mini_sector_size
                end = offset + self.mini_sector_size
                if end > len(root_stream):
                    raise HwpStructureError("OLE mini stream is out of bounds")
                chunks.append(root_stream[offset:end])
            return b"".join(chunks)[: entry.size]
        return self._read_chain(entry.start_sector, self.fat)[: entry.size]


def _error_row(
    *,
    digest: str,
    code: str,
    message: str,
    stream: str = "FileHeader",
    section: int | None = None,
    record: int = 0,
    object_id: str | None = None,
) -> ParsedRow:
    raw_values = {
        "stream": stream,
        "section": section,
        "record": record,
        "cell": None,
    }
    if object_id is not None:
        raw_values["object_id"] = object_id
    return ParsedRow(
        status=RowStatus.ROW_ERROR,
        provenance=Provenance(
            sheet=stream,
            source_row=record,
            source_file_sha256=digest,
        ),
        raw_values=raw_values,
        fields={},
        error_code=code,
        error_message=message,
    )


def _text_row(
    *, digest: str, stream: str, section: int, record: int, cell: int | None, text: str
) -> ParsedRow:
    warnings: tuple[FieldWarning, ...] = ()
    if text.startswith(_FORMULA_MARKERS):
        warnings = (
            FieldWarning(
                "FORMULA_LIKE_INPUT",
                f"Formula-like HWP text preserved without execution at {stream} record {record}",
            ),
        )
    return ParsedRow(
        status=RowStatus.SUCCESS,
        provenance=Provenance(
            sheet=stream,
            source_row=record,
            source_columns={"text": 1},
            source_file_sha256=digest,
        ),
        raw_values={
            "stream": stream,
            "section": section,
            "record": record,
            "cell": cell,
            "text": text,
        },
        fields={"text": ParsedField(text, text, warnings)},
        warnings=warnings,
    )


def _decode_para_text(payload: bytes) -> str:
    if len(payload) % 2:
        raise UnicodeDecodeError(
            "utf-16le", payload, len(payload) - 1, len(payload), "odd byte"
        )
    decoded = payload.decode("utf-16le", errors="strict")
    return "".join(
        character
        for character in decoded
        if ord(character) >= 32 or character in "\t\n"
    )


def _decompress_section(contents: bytes) -> bytes:
    decompressor = zlib.decompressobj(-15)
    expanded = decompressor.decompress(contents, MAX_DECOMPRESSED_SECTION_BYTES + 1)
    if len(expanded) > MAX_DECOMPRESSED_SECTION_BYTES or decompressor.unconsumed_tail:
        raise HwpStructureError("decompressed BodyText exceeds safety limit")
    expanded += decompressor.flush(MAX_DECOMPRESSED_SECTION_BYTES + 1 - len(expanded))
    if len(expanded) > MAX_DECOMPRESSED_SECTION_BYTES:
        raise HwpStructureError("decompressed BodyText exceeds safety limit")
    if not decompressor.eof or decompressor.unused_data:
        raise HwpStructureError("compressed BodyText stream is damaged")
    return expanded


def is_hwp_compound_file(path: Path) -> bool:
    """Probe required HWP streams through the bounded CFB reader."""
    try:
        with _CompoundFile(path) as compound:
            return "FileHeader" in compound.paths and any(
                re.fullmatch(r"BodyText/Section\d+", name) for name in compound.paths
            )
    except (OSError, HwpStructureError, struct.error, UnicodeError):
        return False


def _parse_section(
    contents: bytes, *, digest: str, stream: str, section: int
) -> list[ParsedRow]:
    rows: list[ParsedRow] = []
    offset = 0
    record = 0
    in_table = False
    table_level = 0
    cell = 0
    while offset < len(contents):
        record += 1
        if len(contents) - offset < 4:
            rows.append(
                _error_row(
                    digest=digest,
                    code="HWP_DAMAGED_RECORD",
                    message=f"Truncated HWP record header at byte {offset}; convert to HWPX and retry",
                    stream=stream,
                    section=section,
                    record=record,
                )
            )
            break
        header = struct.unpack_from("<I", contents, offset)[0]
        offset += 4
        tag = header & 0x3FF
        level = (header >> 10) & 0x3FF
        size = (header >> 20) & 0xFFF
        if size == 0xFFF:
            if len(contents) - offset < 4:
                rows.append(
                    _error_row(
                        digest=digest,
                        code="HWP_DAMAGED_RECORD",
                        message=f"Missing extended HWP record size at byte {offset}; convert to HWPX and retry",
                        stream=stream,
                        section=section,
                        record=record,
                    )
                )
                break
            size = struct.unpack_from("<I", contents, offset)[0]
            offset += 4
        if size > len(contents) - offset:
            rows.append(
                _error_row(
                    digest=digest,
                    code="HWP_DAMAGED_RECORD",
                    message=f"HWP record payload exceeds {stream} at record {record}; convert to HWPX and retry",
                    stream=stream,
                    section=section,
                    record=record,
                )
            )
            break
        payload = contents[offset : offset + size]
        offset += size
        if (
            in_table
            and level <= table_level
            and tag in {HWPTAG_PARA_HEADER, HWPTAG_PARA_TEXT}
        ):
            in_table = False
            cell = 0
        if tag == HWPTAG_TABLE:
            in_table = True
            table_level = level
            cell = 0
        elif tag == HWPTAG_LIST_HEADER and in_table:
            cell += 1
        elif tag == HWPTAG_PARA_TEXT:
            try:
                text = _decode_para_text(payload)
            except UnicodeDecodeError as error:
                rows.append(
                    _error_row(
                        digest=digest,
                        code="HWP_DAMAGED_RECORD",
                        message=f"Invalid paragraph text at record {record}: {error}; convert to HWPX and retry",
                        stream=stream,
                        section=section,
                        record=record,
                    )
                )
            else:
                rows.append(
                    _text_row(
                        digest=digest,
                        stream=stream,
                        section=section,
                        record=record,
                        cell=cell if in_table and cell else None,
                        text=text,
                    )
                )
        elif tag == HWPTAG_CTRL_HEADER:
            control_id = payload[:4]
            if control_id not in _SUPPORTED_CONTROL_IDS:
                object_id = control_id.decode("ascii", errors="replace")
                rows.append(
                    _error_row(
                        digest=digest,
                        code="HWP_UNSUPPORTED_OBJECT",
                        message=f"Unsupported HWP object {object_id!r} at {stream} record {record}; convert to HWPX and retry",
                        stream=stream,
                        section=section,
                        record=record,
                        object_id=object_id,
                    )
                )
    return rows


def parse_hwp(
    source: Path | StoredFile,
    *,
    role: DocumentRole,
    sha256: str | None = None,
) -> ParseResult:
    started = time.perf_counter()
    path, digest = _source_path_and_sha256(source, sha256)
    rows: list[ParsedRow] = []
    try:
        with _CompoundFile(path) as compound:
            header = compound.read_stream("FileHeader")
            if len(header) < 40 or not header.startswith(b"HWP Document File"):
                raise HwpStructureError("HWP FileHeader signature is invalid")
            flags = struct.unpack_from("<I", header, 36)[0]
            compressed = bool(flags & 0x1)
            if flags & 0x6:
                rows.append(
                    _error_row(
                        digest=digest,
                        code="HWP_ENCRYPTED",
                        message="Encrypted or distribution-protected HWP cannot be read safely; decrypt it or convert it to HWPX and retry",
                    )
                )
            else:
                section_streams = sorted(
                    (
                        (int(match.group(1)), name)
                        for name in compound.paths
                        if (match := re.fullmatch(r"BodyText/Section(\d+)", name))
                    ),
                    key=lambda item: item[0],
                )
                if not section_streams:
                    raise HwpStructureError("HWP BodyText Section stream is missing")
                for section, stream in section_streams:
                    contents = compound.read_stream(stream)
                    if compressed:
                        try:
                            contents = _decompress_section(contents)
                        except (HwpStructureError, zlib.error) as error:
                            rows.append(
                                _error_row(
                                    digest=digest,
                                    code="HWP_DAMAGED",
                                    message=f"Could not decompress {stream}: {error}; convert to HWPX and retry",
                                    stream=stream,
                                    section=section,
                                )
                            )
                            continue
                    rows.extend(
                        _parse_section(
                            contents,
                            digest=digest,
                            stream=stream,
                            section=section,
                        )
                    )
    except (OSError, HwpStructureError, struct.error, UnicodeError) as error:
        if not rows:
            rows.append(
                _error_row(
                    digest=digest,
                    code="HWP_DAMAGED",
                    message=f"{error}; recover the file or convert it to HWPX and retry",
                )
            )
    return ParseResult(
        role=role,
        detected_format="HWP",
        parser_version=PARSER_VERSION,
        parser_backend="in-process-cfb-hwp5",
        rows=rows,
        elapsed_seconds=max(time.perf_counter() - started, 0.000001),
    )
