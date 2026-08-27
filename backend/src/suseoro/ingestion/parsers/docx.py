"""Safe, provenance-preserving DOCX text extraction."""

from __future__ import annotations

import time
import zipfile
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

PARSER_VERSION = "docx-v1"
DOCUMENT_XML = "word/document.xml"
_FORMULA_MARKERS = ("=", "+", "-", "@")


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _attribute(element: Any, name: str) -> str | None:
    return next(
        (
            value
            for key, value in element.attrib.items()
            if key == name or key.endswith("}" + name)
        ),
        None,
    )


def _contains_page_break(element: Any) -> bool:
    return any(
        _local_name(descendant.tag) == "br" and _attribute(descendant, "type") == "page"
        for descendant in element.iter()
    )


def _text(element: Any) -> str:
    chunks: list[str] = []
    for descendant in element.iter():
        name = _local_name(descendant.tag)
        if name == "t" and descendant.text:
            chunks.append(descendant.text)
        elif name == "tab":
            chunks.append("\t")
        elif name == "br" and _attribute(descendant, "type") != "page":
            chunks.append("\n")
    return "".join(chunks)


def _warning(text: str, node: str) -> tuple[FieldWarning, ...]:
    if text.startswith(_FORMULA_MARKERS):
        return (
            FieldWarning(
                "FORMULA_LIKE_INPUT",
                f"Formula-like source text preserved without execution at {node}",
            ),
        )
    return ()


def _row(
    *,
    text: str,
    kind: str,
    page: int,
    node: str,
    source_row: int,
    source_file_sha256: str,
) -> ParsedRow:
    warnings = _warning(text, node)
    return ParsedRow(
        status=RowStatus.SUCCESS,
        provenance=Provenance(
            sheet=f"{DOCUMENT_XML}#{node}",
            source_row=source_row,
            source_columns={"text": 1},
            source_file_sha256=source_file_sha256,
        ),
        raw_values={"text": text, "kind": kind, "page": page, "node": node},
        fields={"text": ParsedField(text, text, warnings)},
        warnings=warnings,
    )


def _error(
    *, code: str, message: str, source_file_sha256: str, node: str = DOCUMENT_XML
) -> ParsedRow:
    return ParsedRow(
        status=RowStatus.ROW_ERROR,
        provenance=Provenance(
            sheet=node,
            source_row=0,
            source_file_sha256=source_file_sha256,
        ),
        raw_values={"node": node},
        fields={},
        error_code=code,
        error_message=message,
    )


def parse_docx(
    source: Path | StoredFile,
    *,
    role: DocumentRole,
    sha256: str | None = None,
) -> ParseResult:
    """Return each body paragraph or table cell exactly once in body order."""
    started = time.perf_counter()
    path, digest = _source_path_and_sha256(source, sha256)
    try:
        inspect_zip(path)
        with zipfile.ZipFile(path) as archive:
            root = safe_xml_from_bytes(archive.read(DOCUMENT_XML))
    except XmlSafetyError as error:
        rows = [
            _error(
                code="DOCUMENT_XML_ERROR",
                message=str(error),
                source_file_sha256=digest,
            )
        ]
    except (KeyError, OSError, ValueError, zipfile.BadZipFile) as error:
        rows = [
            _error(
                code="DOCUMENT_ARCHIVE_ERROR",
                message=str(error),
                source_file_sha256=digest,
            )
        ]
    else:
        body = next(
            (element for element in root.iter() if _local_name(element.tag) == "body"),
            None,
        )
        if body is None:
            rows = [
                _error(
                    code="DOCUMENT_XML_ERROR",
                    message="DOCX document body is missing",
                    source_file_sha256=digest,
                )
            ]
        else:
            rows = []
            page = 1
            logical_index = 0
            paragraph_number = 0
            table_number = 0

            def walk_blocks(container: Any, parent_node: str) -> None:
                nonlocal page, logical_index, paragraph_number, table_number
                for child_position, child in enumerate(container, start=1):
                    name = _local_name(child.tag)
                    if name == "p":
                        paragraph_number += 1
                        if _contains_page_break(child):
                            page += 1
                        logical_index += 1
                        node = f"{parent_node}/paragraph[{paragraph_number}]"
                        rows.append(
                            _row(
                                text=_text(child),
                                kind="paragraph",
                                page=page,
                                node=node,
                                source_row=logical_index,
                                source_file_sha256=digest,
                            )
                        )
                    elif name == "tbl":
                        table_number += 1
                        for row_number, table_row in enumerate(
                            (
                                item
                                for item in child.iter()
                                if _local_name(item.tag) == "tr"
                            ),
                            start=1,
                        ):
                            cells = [
                                item
                                for item in table_row
                                if _local_name(item.tag) == "tc"
                            ]
                            for cell_number, cell in enumerate(cells, start=1):
                                if _contains_page_break(cell):
                                    page += 1
                                logical_index += 1
                                node = (
                                    f"{parent_node}/table[{table_number}]"
                                    f"/row[{row_number}]/cell[{cell_number}]"
                                )
                                rows.append(
                                    _row(
                                        text=_text(cell),
                                        kind="table_cell",
                                        page=page,
                                        node=node,
                                        source_row=logical_index,
                                        source_file_sha256=digest,
                                    )
                                )
                    else:
                        walk_blocks(child, f"{parent_node}/{name}[{child_position}]")

            walk_blocks(body, "body")
    return ParseResult(
        role=role,
        detected_format="DOCX",
        parser_version=PARSER_VERSION,
        parser_backend="zip-defusedxml",
        rows=rows,
        elapsed_seconds=max(time.perf_counter() - started, 0.000001),
    )
