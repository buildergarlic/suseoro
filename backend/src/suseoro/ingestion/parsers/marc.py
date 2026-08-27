"""Streaming ISO 2709 / KORMARC validation and holdings-change mapping."""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from itertools import pairwise
from pathlib import Path
from typing import Any

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

PARSER_VERSION = "marc-v1"
MAX_RECORD_BYTES = 1024 * 1024
_FORMULA_MARKERS = ("=", "+", "-", "@")


class MarcRecordError(ValueError):
    pass


class MarcIdentityError(MarcRecordError):
    pass


class MarcFieldGrammarError(MarcRecordError):
    pass


@dataclass(frozen=True)
class MarcParseResult(ParseResult):
    activation_allowed: bool = True


def _decode(value: bytes) -> str:
    for encoding in ("utf-8", "cp949", "euc-kr"):
        try:
            return value.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise MarcRecordError("field text is not UTF-8, CP949, or EUC-KR")


def _validate_data_field(tag: str, value: bytes) -> None:
    if len(value) < 4:
        raise MarcFieldGrammarError(
            f"data field {tag} lacks indicators or a complete subfield"
        )
    data = value[2:]
    if not data.startswith(b"\x1f"):
        raise MarcFieldGrammarError(
            f"data field {tag} does not begin with a subfield delimiter"
        )
    for subfield in data[1:].split(b"\x1f"):
        if len(subfield) < 2:
            raise MarcFieldGrammarError(
                f"data field {tag} has an empty or truncated subfield"
            )
        code = subfield[0]
        if not (
            ord("0") <= code <= ord("9")
            or ord("A") <= code <= ord("Z")
            or ord("a") <= code <= ord("z")
        ):
            raise MarcFieldGrammarError(
                f"data field {tag} has an invalid subfield code"
            )


def _fields(record: bytes) -> dict[str, list[bytes]]:
    if len(record) < 25 or record[-1:] != b"\x1d":
        raise MarcRecordError("record terminator is missing")
    if not record[:5].isdigit() or int(record[:5]) != len(record):
        raise MarcRecordError("leader record length does not match the bounded record")
    if not record[10:12].isdigit() or not record[12:17].isdigit():
        raise MarcRecordError(
            "leader indicator/subfield sizes or base address are invalid"
        )
    indicator_count = int(record[10:11])
    subfield_identifier_count = int(record[11:12])
    base_address = int(record[12:17])
    if indicator_count != 2 or subfield_identifier_count != 2:
        raise MarcRecordError("unsupported indicator or subfield identifier width")
    if record[20:24] != b"4500":
        raise MarcRecordError("unsupported ISO 2709 directory map")
    if base_address <= 24 or base_address >= len(record):
        raise MarcRecordError("field base address is outside the record")
    if record[base_address - 1 : base_address] != b"\x1e":
        raise MarcRecordError("directory terminator is missing")
    directory = record[24 : base_address - 1]
    if len(directory) % 12:
        raise MarcRecordError("directory length is not a multiple of 12")
    parsed: dict[str, list[bytes]] = {}
    ranges: list[tuple[int, int]] = []
    for offset in range(0, len(directory), 12):
        entry = directory[offset : offset + 12]
        tag_bytes, length_bytes, start_bytes = entry[:3], entry[3:7], entry[7:12]
        if (
            not tag_bytes.isdigit()
            or not length_bytes.isdigit()
            or not start_bytes.isdigit()
        ):
            raise MarcRecordError("directory entry contains nonnumeric boundaries")
        length = int(length_bytes)
        start = int(start_bytes)
        absolute_start = base_address + start
        absolute_end = absolute_start + length
        if (
            length < 1
            or absolute_start < base_address
            or absolute_end > len(record) - 1
        ):
            raise MarcRecordError("directory field boundary is outside the record")
        if record[absolute_end - 1 : absolute_end] != b"\x1e":
            raise MarcRecordError("field terminator is missing")
        ranges.append((absolute_start, absolute_end))
        tag = tag_bytes.decode("ascii")
        field_value = record[absolute_start : absolute_end - 1]
        if tag >= "010":
            _validate_data_field(tag, field_value)
        parsed.setdefault(tag, []).append(field_value)
    ordered = sorted(ranges)
    if any(previous_end > start for (_, previous_end), (start, _) in pairwise(ordered)):
        raise MarcRecordError("directory fields overlap")
    if ordered and (
        ordered[0][0] != base_address
        or ordered[-1][1] != len(record) - 1
        or any(
            previous_end != start for (_, previous_end), (start, _) in pairwise(ordered)
        )
    ):
        raise MarcRecordError("directory fields do not cover a contiguous data area")
    return parsed


def _control(fields: dict[str, list[bytes]], tag: str) -> list[str]:
    values = [_decode(value).strip(" \x00") for value in fields.get(tag, [])]
    return [value for value in values if value]


def _subfields(fields: dict[str, list[bytes]], tag: str, codes: set[str]) -> list[str]:
    values: list[str] = []
    for field in fields.get(tag, []):
        if len(field) < 2:
            raise MarcRecordError(f"data field {tag} has no indicators")
        for item in field[2:].split(b"\x1f"):
            if not item:
                continue
            if len(item) < 2:
                raise MarcRecordError(f"data field {tag} has a truncated subfield")
            code = chr(item[0])
            if code in codes:
                values.append(_decode(item[1:]).strip(" /:;,"))
    return [value for value in values if value]


def _field(value: Any, raw_value: Any | None = None) -> ParsedField:
    raw = value if raw_value is None else raw_value
    warnings: tuple[FieldWarning, ...] = ()
    if isinstance(value, str) and value.startswith(_FORMULA_MARKERS):
        warnings = (
            FieldWarning(
                "FORMULA_LIKE_INPUT",
                "Formula-like MARC text preserved without execution",
            ),
        )
    return ParsedField(value=value, raw_value=raw, warnings=warnings)


def _mapped_row(
    record: bytes,
    *,
    digest: str,
    record_number: int,
    byte_offset: int,
    incremental: bool,
) -> ParsedRow:
    fields = _fields(record)
    if len(fields.get("001", [])) > 1:
        raise MarcIdentityError("MARC record contains multiple 001 control fields")
    identifiers = _control(fields, "001")
    updated = _control(fields, "005")
    fixed = _control(fields, "008")
    isbns = _subfields(fields, "020", {"a"})
    title_parts = _subfields(fields, "245", {"a", "b", "n", "p"})
    publishers = _subfields(fields, "260", {"b"}) + _subfields(fields, "264", {"b"})
    publication_dates = _subfields(fields, "260", {"c"}) + _subfields(
        fields, "264", {"c"}
    )
    authors = (
        _subfields(fields, "100", {"a"})
        + _subfields(fields, "110", {"a"})
        + _subfields(fields, "700", {"a"})
    )
    registrations = _subfields(fields, "049", {"l"})
    copies = _subfields(fields, "049", {"c"})
    classifications = _subfields(fields, "056", {"a", "b"})
    registration_dates = [value[:6] for value in fixed if len(value) >= 6]
    registration_dates.extend(publication_dates)
    # 049$l identifies individual holdings and can repeat.  Only the record's
    # 001 is a stable item identity suitable for incremental replay.
    source_item_id = next(iter(identifiers), None)
    title = " ".join(title_parts)
    author = "; ".join(authors)
    call_number = " ".join(classifications)
    raw_values: dict[str, Any] = {
        "byte_offset": byte_offset,
        "record_length": len(record),
        "source_item_id": source_item_id,
        "identifiers": identifiers,
        "isbns": isbns,
        "title_parts": title_parts,
        "authors": authors,
        "publishers": publishers,
        "publication_dates": publication_dates,
        "registration_numbers": registrations,
        "copy_numbers": copies,
        "classifications": classifications,
        "registration_date_candidates": registration_dates,
        "update_date_candidates": updated,
    }
    mapped: dict[str, ParsedField] = {}
    for name, value, raw in (
        ("source_item_id", source_item_id, source_item_id),
        ("isbn", next(iter(isbns), None), isbns),
        ("title", title or None, title_parts),
        ("author", author or None, authors),
        ("publisher", next(iter(publishers), None), publishers),
        ("registration_number", next(iter(registrations), None), registrations),
        ("quantity", len(registrations) or None, registrations),
        ("call_number", call_number or None, classifications),
        (
            "registered_at_candidate",
            next(iter(registration_dates), None),
            registration_dates,
        ),
        ("updated_at_candidate", next(iter(updated), None), updated),
    ):
        if value is not None:
            mapped[name] = _field(value, raw)
    warnings = tuple(warning for field in mapped.values() for warning in field.warnings)
    missing_stable_id = source_item_id is None
    return ParsedRow(
        status=RowStatus.ROW_ERROR if missing_stable_id else RowStatus.SUCCESS,
        provenance=Provenance(
            sheet="record",
            source_row=record_number,
            source_columns={name: record_number for name in mapped},
            source_file_sha256=digest,
        ),
        raw_values=raw_values,
        fields=mapped,
        warnings=warnings,
        error_code="MARC_STABLE_ID_REQUIRED" if missing_stable_id else None,
        error_message=(
            "Incremental MARC activation requires a stable 001 source item ID"
            if missing_stable_id
            else None
        ),
    )


def _error_row(
    *, digest: str, record_number: int, byte_offset: int, code: str, message: str
) -> ParsedRow:
    return ParsedRow(
        status=RowStatus.ROW_ERROR,
        provenance=Provenance(
            sheet="record",
            source_row=record_number,
            source_file_sha256=digest,
        ),
        raw_values={"byte_offset": byte_offset, "record": record_number},
        fields={},
        error_code=code,
        error_message=message,
    )


def parse_marc(
    source: Path | StoredFile,
    *,
    role: DocumentRole,
    sha256: str | None = None,
    incremental: bool = True,
) -> MarcParseResult:
    """Read one bounded ISO 2709 record at a time; never materialize the file."""
    started = time.perf_counter()
    path, digest = _source_path_and_sha256(source, sha256)
    rows: list[ParsedRow] = []
    activation_allowed = True
    record_number = 0
    byte_offset = 0
    seen_source_ids: set[str] = set()
    with path.open("rb") as source_file:
        while True:
            prefix = source_file.read(5)
            if not prefix:
                break
            record_number += 1
            if len(prefix) != 5 or not prefix.isdigit():
                rows.append(
                    _error_row(
                        digest=digest,
                        record_number=record_number,
                        byte_offset=byte_offset,
                        code="MARC_FILE_STRUCTURE_UNTRUSTWORTHY",
                        message="Cannot determine the next ISO 2709 record boundary",
                    )
                )
                activation_allowed = False
                break
            record_length = int(prefix)
            if record_length < 25 or record_length > MAX_RECORD_BYTES:
                rows.append(
                    _error_row(
                        digest=digest,
                        record_number=record_number,
                        byte_offset=byte_offset,
                        code="MARC_FILE_STRUCTURE_UNTRUSTWORTHY",
                        message=f"Unsafe ISO 2709 record length {record_length}",
                    )
                )
                activation_allowed = False
                break
            remainder = source_file.read(record_length - 5)
            if len(remainder) != record_length - 5:
                rows.append(
                    _error_row(
                        digest=digest,
                        record_number=record_number,
                        byte_offset=byte_offset,
                        code="MARC_FILE_STRUCTURE_UNTRUSTWORTHY",
                        message="ISO 2709 record is truncated, so following boundaries are untrusted",
                    )
                )
                activation_allowed = False
                break
            record = prefix + remainder
            try:
                row = _mapped_row(
                    record,
                    digest=digest,
                    record_number=record_number,
                    byte_offset=byte_offset,
                    incremental=incremental,
                )
            except MarcIdentityError as error:
                row = _error_row(
                    digest=digest,
                    record_number=record_number,
                    byte_offset=byte_offset,
                    code="MARC_IDENTITY_INVALID",
                    message=str(error),
                )
                activation_allowed = False
            except MarcFieldGrammarError as error:
                row = _error_row(
                    digest=digest,
                    record_number=record_number,
                    byte_offset=byte_offset,
                    code="MARC_FIELD_GRAMMAR_INVALID",
                    message=str(error),
                )
                activation_allowed = False
            except (MarcRecordError, UnicodeError, ValueError) as error:
                row = _error_row(
                    digest=digest,
                    record_number=record_number,
                    byte_offset=byte_offset,
                    code="MARC_RECORD_DAMAGED",
                    message=str(error),
                )
            rows.append(row)
            if row.error_code == "MARC_STABLE_ID_REQUIRED":
                activation_allowed = False
            elif incremental and row.status == RowStatus.SUCCESS:
                source_item_id = str(row.fields["source_item_id"].value)
                if source_item_id in seen_source_ids:
                    rows[-1] = replace(
                        row,
                        status=RowStatus.ROW_ERROR,
                        error_code="MARC_DUPLICATE_SOURCE_ITEM_ID",
                        error_message=(
                            f"Duplicate stable MARC 001 source item ID: {source_item_id}"
                        ),
                    )
                    activation_allowed = False
                else:
                    seen_source_ids.add(source_item_id)
            byte_offset += record_length
    if record_number == 0:
        rows.append(
            _error_row(
                digest=digest,
                record_number=1,
                byte_offset=0,
                code="MARC_FILE_STRUCTURE_UNTRUSTWORTHY",
                message="MARC source contains no ISO 2709 records",
            )
        )
        activation_allowed = False
    return MarcParseResult(
        role=role,
        detected_format="MARC",
        parser_version=PARSER_VERSION,
        parser_backend="iso2709-stream",
        rows=rows,
        elapsed_seconds=max(time.perf_counter() - started, 0.000001),
        activation_allowed=activation_allowed,
    )
