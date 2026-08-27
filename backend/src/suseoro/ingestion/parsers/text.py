"""Streaming delimited-text and pasted-text parser."""

from __future__ import annotations

import csv
import io
from itertools import chain
from pathlib import Path

from suseoro.ingestion.contracts import DocumentRole, ParseResult
from suseoro.ingestion.detection import detect_delimiter, detect_encoding


def _parse_reader(
    reader: csv.reader,
    *,
    role: DocumentRole,
    detected_format: str,
    encoding: str,
    source_kind: str,
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
    )


def parse_delimited_file(path: Path, *, role: DocumentRole) -> ParseResult:
    path = Path(path)
    with path.open("rb") as probe:
        sample = probe.read(64_000)
    encoding, sample_text = detect_encoding(sample)
    delimiter, detected_format = detect_delimiter(sample_text)
    with path.open("r", encoding=encoding, newline="") as source:
        reader = csv.reader(source, delimiter=delimiter)
        return _parse_reader(
            reader,
            role=role,
            detected_format=detected_format,
            encoding=encoding,
            source_kind="FILE",
        )


def parse_pasted_text(text: str, *, role: DocumentRole) -> ParseResult:
    delimiter, detected_format = detect_delimiter(text)
    return _parse_reader(
        csv.reader(io.StringIO(text), delimiter=delimiter),
        role=role,
        detected_format=detected_format,
        encoding="unicode",
        source_kind="PASTE",
    )
