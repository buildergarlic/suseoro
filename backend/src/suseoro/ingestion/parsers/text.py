"""Streaming delimited-text and pasted-text parser."""

from __future__ import annotations

import csv
import hashlib
import io
from itertools import chain
from pathlib import Path

from suseoro.ingestion.contracts import DocumentRole, ParseResult
from suseoro.ingestion.detection import (
    FileDetection,
    detect_delimiter,
    detect_file_type,
)


def _parse_reader(
    reader: csv.reader,
    *,
    role: DocumentRole,
    detected_format: str,
    encoding: str,
    source_kind: str,
    source_file_sha256: str,
) -> ParseResult:
    from suseoro.ingestion.parsers.tabular import _parse_sheets

    prefix = []
    for _ in range(25):
        try:
            prefix.append(next(reader))
        except StopIteration:
            break
    return _parse_sheets(
        [("텍스트", chain(prefix, reader))],
        role=role,
        detected_format=detected_format,
        parser_backend="python-csv-stream",
        encoding=encoding,
        source_kind=source_kind,
        source_file_sha256=source_file_sha256,
    )


def parse_delimited_file(
    path: Path,
    *,
    role: DocumentRole,
    source_file_sha256: str,
    detection: FileDetection | None = None,
) -> ParseResult:
    path = Path(path)
    detected = detection or detect_file_type(path)
    if detected.encoding is None or detected.delimiter is None:
        raise ValueError("text encoding and delimiter could not be detected")
    encoding = detected.encoding
    delimiter = detected.delimiter
    with path.open("r", encoding=encoding, errors="replace", newline="") as source:
        reader = csv.reader(source, delimiter=delimiter)
        return _parse_reader(
            reader,
            role=role,
            detected_format=detected.format,
            encoding=encoding,
            source_kind="FILE",
            source_file_sha256=source_file_sha256,
        )


def parse_pasted_text(text: str, *, role: DocumentRole) -> ParseResult:
    delimiter, detected_format = detect_delimiter(text)
    source_file_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return _parse_reader(
        csv.reader(io.StringIO(text), delimiter=delimiter),
        role=role,
        detected_format=detected_format,
        encoding="unicode",
        source_kind="PASTE",
        source_file_sha256=source_file_sha256,
    )
