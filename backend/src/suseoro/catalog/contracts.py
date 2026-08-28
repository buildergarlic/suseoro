"""Typed contracts for catalog snapshots, deltas, and matching decisions."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any


class SourceType(str, Enum):
    DLS_MARC = "DLS_MARC"
    DLS_EXCEL = "DLS_EXCEL"
    DLS_API = "DLS_API"


class HoldingStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    WITHDRAWN = "WITHDRAWN"
    MISSING = "MISSING"
    UNCERTAIN = "UNCERTAIN"


class ParserStatus(str, Enum):
    PENDING = "PENDING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class CandidateOutcome(str, Enum):
    CANDIDATE = "CANDIDATE"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    EXCLUDED = "EXCLUDED"


@dataclass(frozen=True)
class CatalogRecord:
    source_item_id: str
    title: str
    isbn: str | None = None
    subtitle: str | None = None
    authors: tuple[str, ...] = ()
    publisher: str | None = None
    volume: str | None = None
    edition: str | None = None
    series: str | None = None
    publication_date: str | None = None
    price: int | None = None
    pages: int | None = None
    kdc: str | None = None
    registration_number: str | None = None
    call_number: str | None = None
    location: str | None = None
    holding_status: HoldingStatus = HoldingStatus.AVAILABLE
    source_row_id: str | None = None
    raw_fields: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "authors", tuple(self.authors))
        if not isinstance(self.holding_status, HoldingStatus):
            object.__setattr__(
                self, "holding_status", HoldingStatus(self.holding_status)
            )


@dataclass(frozen=True)
class NormalizedBook:
    original: CatalogRecord
    isbn13: str | None
    title_key: str
    subtitle_key: str
    author_key: str
    publisher_key: str
    volume_key: str
    edition_key: str
    series_key: str
    search_text: str


@dataclass(frozen=True)
class CatalogVersion:
    id: str
    school_id: str
    source_type: SourceType
    import_mode: str
    status: str
    item_count: int
    parent_version_id: str | None = None
    as_of_local_date: date | None = None
    created_at: datetime | None = None
    activated_at: datetime | None = None


@dataclass(frozen=True)
class HoldingMatch:
    id: str
    stable_id: str
    source_item_id: str
    isbn13: str | None
    title_key: str
    subtitle_key: str
    author_key: str
    publisher_key: str
    volume_key: str
    edition_key: str
    series_key: str
    holding_status: HoldingStatus
    comparison_text: str


@dataclass(frozen=True)
class MatchingDecision:
    outcome: CandidateOutcome
    reason: str
    evidence_holding_id: str | None = None
    score: float | None = None


@dataclass(frozen=True)
class DeltaWindow:
    start: date
    end: date
    start_inclusive: bool = True
    end_inclusive: bool = True

    def __post_init__(self) -> None:
        if self.start > self.end:
            raise ValueError("delta window start cannot follow its end")


@dataclass(frozen=True)
class DeltaFile:
    source_file_sha256: str
    parser_version: str
    status: ParserStatus
    records: tuple[CatalogRecord, ...] = ()
    activation_allowed: bool = True
    source_document_id: str | None = None
    window: DeltaWindow | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "records", tuple(self.records))
        if not isinstance(self.status, ParserStatus):
            object.__setattr__(self, "status", ParserStatus(self.status))
        if not re.fullmatch(r"[0-9a-f]{64}", self.source_file_sha256):
            raise ValueError("source_file_sha256 must be canonical lowercase SHA-256")
        if not self.parser_version.strip():
            raise ValueError("parser_version is required")


@dataclass(frozen=True)
class DeltaApplyResult:
    applied: bool
    status: str
    catalog_version_id: str | None
    watermark_local_date: date | None
    idempotent: bool = False


@dataclass(frozen=True)
class CatalogSourceState:
    school_id: str
    source_type: SourceType
    active_version_id: str
    watermark_local_date: date | None
    last_full_snapshot_at: datetime


@dataclass(frozen=True)
class ComparisonSummary:
    total_rows: int
    counts: dict[str, int]
    file_statuses: dict[str, str]
