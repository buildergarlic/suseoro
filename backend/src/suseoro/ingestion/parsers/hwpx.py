"""Safe HWPX section and table-cell extraction."""

from __future__ import annotations

import re
import time
import zipfile
from collections.abc import Iterator
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
from suseoro.ingestion.safety import XmlSafetyError, inspect_zip, safe_xml_from_bytes

PARSER_VERSION = "hwpx-v1"
_SECTION = re.compile(r"^Contents/section(\d+)\.xml$", re.IGNORECASE)
_FORMULA_MARKERS = ("=", "+", "-", "@")


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _text(element: Any) -> str:
    return "".join(
        descendant.text or ""
        for descendant in element.iter()
        if _local_name(descendant.tag) == "t"
    )


def _logical_nodes(root: Any) -> Iterator[tuple[str, str]]:
    paragraph_number = 0
    table_number = 0

    def walk(container: Any) -> Iterator[tuple[str, str]]:
        nonlocal paragraph_number, table_number
        for child in container:
            name = _local_name(child.tag).lower()
            if name == "p":
                paragraph_number += 1
                yield f"paragraph[{paragraph_number}]", _text(child)
            elif name in {"tbl", "table"}:
                table_number += 1
                for row_number, table_row in enumerate(
                    (
                        item
                        for item in child.iter()
                        if _local_name(item.tag).lower() in {"tr", "row"}
                    ),
                    start=1,
                ):
                    cells = [
                        item
                        for item in table_row
                        if _local_name(item.tag).lower() in {"tc", "cell"}
                    ]
                    for cell_number, cell in enumerate(cells, start=1):
                        yield (
                            f"table[{table_number}]/row[{row_number}]/cell[{cell_number}]",
                            _text(cell),
                        )
            else:
                yield from walk(child)

    yield from walk(root)


def _row(
    *,
    section: int,
    source_row: int,
    node: str,
    text: str,
    digest: str,
) -> ParsedRow:
    kind = "table_cell" if node.startswith("table") else "paragraph"
    warnings: tuple[FieldWarning, ...] = ()
    if text.startswith(_FORMULA_MARKERS):
        warnings = (
            FieldWarning(
                "FORMULA_LIKE_INPUT",
                f"Formula-like source text preserved without execution at section {section} {node}",
            ),
        )
    part = f"Contents/section{section}.xml#{node}"
    return ParsedRow(
        status=RowStatus.SUCCESS,
        provenance=Provenance(
            sheet=part,
            source_row=source_row,
            source_columns={"text": 1},
            source_file_sha256=digest,
        ),
        raw_values={
            "text": text,
            "kind": kind,
            "section": section,
            "node": node,
        },
        fields={"text": ParsedField(text, text, warnings)},
        warnings=warnings,
    )


def _error(*, section: int | None, code: str, message: str, digest: str) -> ParsedRow:
    part = None if section is None else f"Contents/section{section}.xml"
    return ParsedRow(
        status=RowStatus.ROW_ERROR,
        provenance=Provenance(
            sheet=part,
            source_row=0 if section is None else 1,
            source_file_sha256=digest,
        ),
        raw_values={"section": section, "node": part},
        fields={},
        error_code=code,
        error_message=message,
    )


def parse_hwpx(
    source: Path | StoredFile,
    *,
    role: DocumentRole,
    sha256: str | None = None,
) -> ParseResult:
    started = time.perf_counter()
    path, digest = _source_path_and_sha256(source, sha256)
    rows: list[ParsedRow] = []
    try:
        inspect_zip(path)
        with zipfile.ZipFile(path) as archive:
            sections = sorted(
                (
                    (int(match.group(1)), name)
                    for name in archive.namelist()
                    if (match := _SECTION.fullmatch(name))
                ),
                key=lambda item: item[0],
            )
            if not sections:
                raise ValueError("HWPX has no section XML parts")
            for section, name in sections:
                try:
                    root = safe_xml_from_bytes(archive.read(name))
                except (KeyError, OSError, XmlSafetyError) as error:
                    rows.append(
                        _error(
                            section=section,
                            code="DOCUMENT_XML_ERROR",
                            message=str(error),
                            digest=digest,
                        )
                    )
                    continue
                for index, (node, text) in enumerate(_logical_nodes(root), start=1):
                    rows.append(
                        _row(
                            section=section,
                            source_row=index,
                            node=node,
                            text=text,
                            digest=digest,
                        )
                    )
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        if not rows:
            rows.append(
                _error(
                    section=None,
                    code="DOCUMENT_ARCHIVE_ERROR",
                    message=str(error),
                    digest=digest,
                )
            )
    return ParseResult(
        role=role,
        detected_format="HWPX",
        parser_version=PARSER_VERSION,
        parser_backend="zip-defusedxml",
        rows=rows,
        elapsed_seconds=max(time.perf_counter() - started, 0.000001),
    )
