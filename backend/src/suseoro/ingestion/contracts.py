"""Typed contracts shared by every ingestion source."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class DocumentRole(str, Enum):
    UNKNOWN = "UNKNOWN"
    PURCHASE_REQUEST = "PURCHASE_REQUEST"
    VENDOR_QUOTE = "VENDOR_QUOTE"
    INVENTORY = "INVENTORY"


class CanonicalField(str, Enum):
    ISBN = "isbn"
    TITLE = "title"
    AUTHOR = "author"
    PUBLISHER = "publisher"
    QUANTITY = "quantity"
    UNIT_PRICE = "unit_price"
    REGISTRATION_NUMBER = "registration_number"
    CALL_NUMBER = "call_number"


class RowStatus(str, Enum):
    SUCCESS = "SUCCESS"
    ROW_ERROR = "ROW_ERROR"


@dataclass(frozen=True)
class FieldWarning:
    code: str
    message: str


@dataclass(frozen=True)
class ParsedField:
    value: Any
    raw_value: Any
    warnings: tuple[FieldWarning, ...] = ()


@dataclass(frozen=True)
class Provenance:
    sheet: str | None
    source_row: int
    source_columns: dict[str, int] = field(default_factory=dict)
    source_file_sha256: str | None = None


@dataclass(frozen=True)
class ParsedRow:
    status: RowStatus
    provenance: Provenance
    raw_values: dict[str, Any]
    fields: dict[str, ParsedField]
    warnings: tuple[FieldWarning, ...] = ()
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class ParseResult:
    role: DocumentRole
    detected_format: str
    parser_version: str
    parser_backend: str
    rows: list[ParsedRow]
    header_rows: dict[str, int] = field(default_factory=dict)
    template_version: str | None = None
    encoding: str | None = None
    source_kind: str = "FILE"
    elapsed_seconds: float = 0.0


@dataclass(frozen=True)
class CachedParseResult:
    result: Any
    cached: bool
