from __future__ import annotations

import hashlib
import importlib
import importlib.util
from pathlib import Path

import pytest

from suseoro.ingestion.contracts import DocumentRole, RowStatus
from suseoro.ingestion.detection import detect_file_type


def _module(name: str):
    assert importlib.util.find_spec(name) is not None, f"missing parser module: {name}"
    return importlib.import_module(name)


def _control(value: str) -> bytes:
    return value.encode("utf-8")


def _data(indicators: str, subfields: list[tuple[str, str]]) -> bytes:
    return indicators.encode("ascii") + b"".join(
        b"\x1f" + code.encode("ascii") + value.encode("utf-8")
        for code, value in subfields
    )


def _record(fields: list[tuple[str, bytes]]) -> bytes:
    directory = bytearray()
    data = bytearray()
    for tag, value in fields:
        field = value + b"\x1e"
        directory.extend(
            tag.encode("ascii")
            + f"{len(field):04d}".encode("ascii")
            + f"{len(data):05d}".encode("ascii")
        )
        data.extend(field)
    base_address = 24 + len(directory) + 1
    record_length = base_address + len(data) + 1
    leader = bytearray(b"00000nam a2200000 i 4500")
    leader[:5] = f"{record_length:05d}".encode("ascii")
    leader[10:12] = b"22"
    leader[12:17] = f"{base_address:05d}".encode("ascii")
    leader[20:24] = b"4500"
    return bytes(leader) + bytes(directory) + b"\x1e" + bytes(data) + b"\x1d"


def _catalog_record(identifier: str | None = "BIB-001") -> bytes:
    fields: list[tuple[str, bytes]] = []
    if identifier is not None:
        fields.append(("001", _control(identifier)))
    fields.extend(
        [
            ("005", _control("20260828112233.0")),
            ("008", _control("260828s2026    ko            000 0 kor  ")),
            ("020", _data("  ", [("a", "978-89-1234-567-8")])),
            (
                "245",
                _data(
                    "10",
                    [("a", "Main title"), ("b", "subtitle"), ("n", "1"), ("p", "Part")],
                ),
            ),
            ("264", _data(" 1", [("b", "Publisher"), ("c", "2026")])),
            ("100", _data("1 ", [("a", "Primary Author")])),
            ("700", _data("1 ", [("a", "Added Author")])),
            (
                "049",
                _data("  ", [("l", "REG-001"), ("c", "2"), ("l", "REG-002")]),
            ),
            ("056", _data("  ", [("a", "813.7"), ("b", "26")])),
        ]
    )
    return _record(fields)


def _write(path: Path, contents: bytes) -> str:
    path.write_bytes(contents)
    return hashlib.sha256(contents).hexdigest()


def test_kormarc_stream_maps_bibliographic_and_holding_fields_to_one_change_per_record(
    tmp_path: Path,
) -> None:
    """Dropping required KORMARC fields or expanding one record into unaccounted rows must fail."""
    marc = _module("suseoro.ingestion.parsers.marc")
    target = tmp_path / "catalog.bin"
    contents = _catalog_record()
    digest = _write(target, contents)

    result = marc.parse_marc(
        target, role=DocumentRole.INVENTORY, sha256=digest, incremental=True
    )

    assert result.activation_allowed is True
    assert len(result.rows) == 1
    row = result.rows[0]
    assert row.status == RowStatus.SUCCESS
    assert row.fields["source_item_id"].value == "BIB-001"
    assert row.fields["isbn"].value == "978-89-1234-567-8"
    assert row.fields["title"].value == "Main title subtitle 1 Part"
    assert row.fields["author"].value == "Primary Author; Added Author"
    assert row.fields["publisher"].value == "Publisher"
    assert row.fields["registration_number"].value == "REG-001"
    assert row.fields["quantity"].value == 2
    assert row.fields["call_number"].value == "813.7 26"
    assert row.fields["updated_at_candidate"].value == "20260828112233.0"
    assert "260828" in row.raw_values["registration_date_candidates"]
    assert row.raw_values["registration_numbers"] == ["REG-001", "REG-002"]
    assert row.provenance.source_file_sha256 == digest
    assert row.raw_values["byte_offset"] == 0


def test_marc_valid_record_boundaries_allow_later_records_after_one_directory_error(
    tmp_path: Path,
) -> None:
    """Stopping after a bounded corrupt record or losing its byte position must fail."""
    marc = _module("suseoro.ingestion.parsers.marc")
    first = _catalog_record("ONE")
    damaged = bytearray(_catalog_record("BROKEN"))
    base_address = int(damaged[12:17])
    first_field_length = int(damaged[27:31])
    damaged[base_address + first_field_length - 1] = ord("X")
    third = _catalog_record("THREE")
    target = tmp_path / "three.mrc"
    digest = _write(target, first + bytes(damaged) + third)

    result = marc.parse_marc(target, role=DocumentRole.INVENTORY, sha256=digest)

    assert len(result.rows) == 3
    assert [row.status for row in result.rows] == [
        RowStatus.SUCCESS,
        RowStatus.ROW_ERROR,
        RowStatus.SUCCESS,
    ]
    assert result.rows[1].error_code == "MARC_RECORD_DAMAGED"
    assert result.rows[1].raw_values["byte_offset"] == len(first)
    assert result.rows[2].fields["source_item_id"].value == "THREE"
    assert result.activation_allowed is True


def test_incremental_marc_rejects_record_without_stable_source_item_id(
    tmp_path: Path,
) -> None:
    """Inventing an unstable offset/hash ID for incremental activation must fail."""
    marc = _module("suseoro.ingestion.parsers.marc")
    target = tmp_path / "no-id.mrc"
    digest = _write(target, _catalog_record(identifier=None))

    result = marc.parse_marc(
        target, role=DocumentRole.INVENTORY, sha256=digest, incremental=True
    )

    assert len(result.rows) == 1
    assert result.rows[0].status == RowStatus.ROW_ERROR
    assert result.rows[0].error_code == "MARC_STABLE_ID_REQUIRED"
    assert result.activation_allowed is False


def test_incremental_marc_rejects_blank_001_and_warns_on_formula_like_text(
    tmp_path: Path,
) -> None:
    """Treating blank 001 as stable or silently accepting formula-like text must fail."""
    marc = _module("suseoro.ingestion.parsers.marc")
    blank_id = tmp_path / "blank-id.mrc"
    blank_digest = _write(blank_id, _catalog_record(identifier="   "))
    formula = tmp_path / "formula.mrc"
    formula_record = _record(
        [
            ("001", _control("FORMULA-1")),
            ("245", _data("10", [("a", "=external command")])),
        ]
    )
    formula_digest = _write(formula, formula_record)

    blank_result = marc.parse_marc(
        blank_id, role=DocumentRole.INVENTORY, sha256=blank_digest
    )
    formula_result = marc.parse_marc(
        formula, role=DocumentRole.INVENTORY, sha256=formula_digest
    )

    assert blank_result.rows[0].error_code == "MARC_STABLE_ID_REQUIRED"
    assert blank_result.activation_allowed is False
    assert formula_result.rows[0].fields["title"].value == "=external command"
    assert "FORMULA_LIKE_INPUT" in {
        warning.code for warning in formula_result.rows[0].warnings
    }


def test_marc_requires_one_nonblank_001_even_without_incremental_mode(
    tmp_path: Path,
) -> None:
    """A blank record identity must block activation in every import mode."""
    marc = _module("suseoro.ingestion.parsers.marc")
    target = tmp_path / "blank-id-non-incremental.mrc"
    digest = _write(target, _catalog_record(identifier="   "))

    result = marc.parse_marc(
        target,
        role=DocumentRole.INVENTORY,
        sha256=digest,
        incremental=False,
    )

    assert len(result.rows) == 1
    assert result.rows[0].status == RowStatus.ROW_ERROR
    assert result.rows[0].error_code == "MARC_STABLE_ID_REQUIRED"
    assert result.rows[0].provenance.source_file_sha256 == digest
    assert result.activation_allowed is False


@pytest.mark.parametrize(
    ("identifier", "error_code"),
    [
        pytest.param("\t", "MARC_FIELD_GRAMMAR_INVALID", id="tab-control"),
        pytest.param("\u00a0", "MARC_STABLE_ID_REQUIRED", id="no-break-space"),
        pytest.param("\u2003", "MARC_STABLE_ID_REQUIRED", id="em-space"),
    ],
)
def test_marc_rejects_whitespace_only_001_and_continues_at_trusted_boundary(
    tmp_path: Path,
    identifier: str,
    error_code: str,
) -> None:
    """Non-ASCII whitespace and control-only identities must never activate."""
    marc = _module("suseoro.ingestion.parsers.marc")
    invalid = _catalog_record(identifier)
    later = _catalog_record("AFTER-WHITESPACE")
    target = tmp_path / "whitespace-identity.mrc"
    digest = _write(target, invalid + later)

    result = marc.parse_marc(
        target,
        role=DocumentRole.INVENTORY,
        sha256=digest,
        incremental=False,
    )

    assert len(result.rows) == 2
    assert result.rows[0].status == RowStatus.ROW_ERROR
    assert result.rows[0].error_code == error_code
    assert result.rows[0].raw_values["byte_offset"] == 0
    assert result.rows[0].provenance.source_file_sha256 == digest
    assert result.rows[1].fields["source_item_id"].value == "AFTER-WHITESPACE"
    assert result.activation_allowed is False


@pytest.mark.parametrize(
    ("tag", "value"),
    [
        pytest.param(
            "245",
            b"10\x1faBroken\x1eInjected",
            id="embedded-field-terminator",
        ),
        pytest.param(
            "008",
            b"260828s2026\x1dko            000 0 kor  ",
            id="embedded-record-terminator",
        ),
        pytest.param(
            "005",
            b"20260828\n112233.0",
            id="embedded-control-byte",
        ),
    ],
)
def test_marc_rejects_structural_controls_inside_fields_and_continues_to_legal_text(
    tmp_path: Path,
    tag: str,
    value: bytes,
) -> None:
    """Structural controls inside a declared field must not become mapped text."""
    marc = _module("suseoro.ingestion.parsers.marc")
    invalid = _record(
        [
            ("001", _control("ILLEGAL-CONTROL")),
            (tag, value),
        ]
    )
    later = _record(
        [
            ("001", _control("AFTER-CONTROL")),
            ("245", _data("10", [("a", "정상 제목")])),
        ]
    )
    target = tmp_path / "embedded-control.mrc"
    digest = _write(target, invalid + later)

    result = marc.parse_marc(
        target,
        role=DocumentRole.INVENTORY,
        sha256=digest,
        incremental=False,
    )

    assert len(result.rows) == 2
    assert result.rows[0].status == RowStatus.ROW_ERROR
    assert result.rows[0].error_code == "MARC_FIELD_GRAMMAR_INVALID"
    assert result.rows[0].raw_values["byte_offset"] == 0
    assert result.rows[0].provenance.source_file_sha256 == digest
    assert result.rows[1].fields["source_item_id"].value == "AFTER-CONTROL"
    assert result.rows[1].fields["title"].value == "정상 제목"
    assert result.activation_allowed is False


def test_incremental_marc_rejects_duplicate_ids_and_empty_files(tmp_path: Path) -> None:
    """Allowing ambiguous duplicate changes or activating an empty source must fail."""
    marc = _module("suseoro.ingestion.parsers.marc")
    duplicates = tmp_path / "duplicates.mrc"
    duplicate_digest = _write(
        duplicates, _catalog_record("SAME") + _catalog_record("SAME")
    )
    empty = tmp_path / "empty.mrc"
    empty_digest = _write(empty, b"")

    duplicate_result = marc.parse_marc(
        duplicates, role=DocumentRole.INVENTORY, sha256=duplicate_digest
    )
    empty_result = marc.parse_marc(
        empty, role=DocumentRole.INVENTORY, sha256=empty_digest
    )

    assert len(duplicate_result.rows) == 2
    assert duplicate_result.rows[1].error_code == "MARC_DUPLICATE_SOURCE_ITEM_ID"
    assert duplicate_result.activation_allowed is False
    assert len(empty_result.rows) == 1
    assert empty_result.rows[0].error_code == "MARC_FILE_STRUCTURE_UNTRUSTWORTHY"
    assert empty_result.activation_allowed is False


def test_marc_rejects_multiple_001_fields_but_continues_at_trusted_boundary(
    tmp_path: Path,
) -> None:
    """Selecting the first of multiple record identities or stopping afterward must fail."""
    marc = _module("suseoro.ingestion.parsers.marc")
    invalid = _record(
        [
            ("001", _control("FIRST")),
            ("001", _control("SECOND")),
            ("245", _data("10", [("a", "Ambiguous identity")])),
        ]
    )
    later = _catalog_record("LATER")
    target = tmp_path / "double-001.mrc"
    digest = _write(target, invalid + later)

    result = marc.parse_marc(target, role=DocumentRole.INVENTORY, sha256=digest)

    assert len(result.rows) == 2
    assert result.rows[0].status == RowStatus.ROW_ERROR
    assert result.rows[0].error_code == "MARC_IDENTITY_INVALID"
    assert result.rows[0].raw_values["byte_offset"] == 0
    assert result.rows[0].provenance.source_file_sha256 == digest
    assert result.rows[1].fields["source_item_id"].value == "LATER"
    assert result.activation_allowed is False


def test_marc_rejects_data_field_without_initial_subfield_delimiter_and_continues(
    tmp_path: Path,
) -> None:
    """Accepting indicator-following text without a subfield delimiter must fail."""
    marc = _module("suseoro.ingestion.parsers.marc")
    invalid = _record(
        [
            ("001", _control("GRAMMAR")),
            ("245", b"10aMissing delimiter"),
        ]
    )
    later = _catalog_record("AFTER-GRAMMAR")
    target = tmp_path / "missing-delimiter.mrc"
    digest = _write(target, invalid + later)

    result = marc.parse_marc(target, role=DocumentRole.INVENTORY, sha256=digest)

    assert len(result.rows) == 2
    assert result.rows[0].status == RowStatus.ROW_ERROR
    assert result.rows[0].error_code == "MARC_FIELD_GRAMMAR_INVALID"
    assert result.rows[0].raw_values["byte_offset"] == 0
    assert result.rows[0].provenance.source_file_sha256 == digest
    assert result.rows[1].fields["source_item_id"].value == "AFTER-GRAMMAR"
    assert result.activation_allowed is False


def test_untrustworthy_marc_length_fails_activation_with_positioned_error(
    tmp_path: Path,
) -> None:
    """Continuing after an unknown boundary or allowing activation must fail."""
    marc = _module("suseoro.ingestion.parsers.marc")
    first = _catalog_record("ONE")
    target = tmp_path / "untrusted.mrc"
    digest = _write(target, first + b"abcdeunrecoverable")

    result = marc.parse_marc(target, role=DocumentRole.INVENTORY, sha256=digest)

    assert len(result.rows) == 2
    assert result.rows[1].status == RowStatus.ROW_ERROR
    assert result.rows[1].error_code == "MARC_FILE_STRUCTURE_UNTRUSTWORTHY"
    assert result.rows[1].raw_values["byte_offset"] == len(first)
    assert result.activation_allowed is False


def test_marc_detection_uses_iso2709_leader_and_terminators(tmp_path: Path) -> None:
    """Depending on .mrc extension or accepting a leader-only spoof must fail."""
    valid = tmp_path / "catalog.data"
    valid.write_bytes(_catalog_record())
    spoof = tmp_path / "spoof.mrc"
    spoof.write_bytes(b"00025nam a2200000 i 4500X")

    assert detect_file_type(valid).format == "MARC"
    assert detect_file_type(spoof).format == "UNKNOWN"
