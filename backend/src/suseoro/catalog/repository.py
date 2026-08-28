"""Indexed SQLite persistence for immutable catalog versions."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import date

from suseoro.catalog.contracts import (
    CatalogRecord,
    CatalogSourceState,
    CatalogVersion,
    HoldingMatch,
    HoldingStatus,
    SourceType,
)
from suseoro.catalog.normalization import normalize_book
from suseoro.security.sessions import format_utc, parse_utc, utc_now

_UUID_SQL = """(
    lower(hex(randomblob(4))) || '-' || lower(hex(randomblob(2))) || '-4' ||
    substr(lower(hex(randomblob(2))), 2) || '-' ||
    substr('89ab', abs(random()) % 4 + 1, 1) ||
    substr(lower(hex(randomblob(2))), 2) || '-' || lower(hex(randomblob(6)))
)"""

_EXACT_ISBN_SQL = """
    SELECT h.id, h.stable_id, h.source_item_id, h.isbn13,
           h.holding_status, nw.title_key, nw.subtitle_key, nw.author_key,
           nw.publisher_key, nw.volume_key, nw.edition_key, nw.series_key,
           nw.search_text
    FROM holdings AS h INDEXED BY idx_holdings_school_isbn
    JOIN catalog_versions AS cv ON cv.id = h.catalog_version_id
    JOIN normalized_works AS nw ON nw.holding_id = h.id
    WHERE h.school_id = ? AND h.isbn13 = ? AND cv.status = 'ACTIVE'
    ORDER BY CASE h.holding_status WHEN 'AVAILABLE' THEN 0 WHEN 'UNCERTAIN' THEN 1 ELSE 2 END,
             h.id
"""


def _date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def _version(row: sqlite3.Row | None) -> CatalogVersion | None:
    if row is None:
        return None
    return CatalogVersion(
        id=row["id"],
        school_id=row["school_id"],
        source_type=SourceType(row["source_type"]),
        import_mode=row["import_mode"],
        status=row["status"],
        item_count=row["item_count"],
        parent_version_id=row["parent_version_id"],
        as_of_local_date=_date(row["as_of_local_date"]),
        created_at=parse_utc(row["created_at"]),
        activated_at=parse_utc(row["activated_at"]) if row["activated_at"] else None,
    )


def _match(row: sqlite3.Row) -> HoldingMatch:
    return HoldingMatch(
        id=row["id"],
        stable_id=row["stable_id"],
        source_item_id=row["source_item_id"],
        isbn13=row["isbn13"],
        title_key=row["title_key"],
        subtitle_key=row["subtitle_key"],
        author_key=row["author_key"],
        publisher_key=row["publisher_key"],
        volume_key=row["volume_key"],
        edition_key=row["edition_key"],
        series_key=row["series_key"],
        holding_status=HoldingStatus(row["holding_status"]),
        comparison_text=row["search_text"],
    )


class CatalogRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def version(self, version_id: str) -> CatalogVersion | None:
        return _version(
            self.connection.execute(
                "SELECT * FROM catalog_versions WHERE id = ?", (version_id,)
            ).fetchone()
        )

    def active_version(self, school_id: str) -> CatalogVersion | None:
        return _version(
            self.connection.execute(
                """
                SELECT * FROM catalog_versions
                WHERE school_id = ? AND status = 'ACTIVE'
                """,
                (school_id,),
            ).fetchone()
        )

    def source_state(self, school_id: str) -> CatalogSourceState | None:
        row = self.connection.execute(
            "SELECT * FROM catalog_source_state WHERE school_id = ?", (school_id,)
        ).fetchone()
        if row is None:
            return None
        return CatalogSourceState(
            school_id=row["school_id"],
            source_type=SourceType(row["source_type"]),
            active_version_id=row["active_version_id"],
            watermark_local_date=_date(row["watermark_local_date"]),
            last_full_snapshot_at=parse_utc(row["last_full_snapshot_at"]),
        )

    def create_staging_version(
        self,
        *,
        school_id: str,
        source_type: SourceType,
        import_mode: str,
        parent_version_id: str | None = None,
        source_document_id: str | None = None,
        as_of_local_date: date | None = None,
        expected_active_version_id: str | None = None,
        expected_active_item_count: int | None = None,
        expected_active_source_type: SourceType | None = None,
        created_at=None,
    ) -> CatalogVersion:
        version_id = str(uuid.uuid4())
        now = created_at or utc_now()
        self.connection.execute(
            """
            INSERT INTO catalog_versions (
                id, school_id, source_type, import_mode, status,
                source_document_id, parent_version_id, item_count,
                as_of_local_date, expected_active_version_id,
                expected_active_item_count, expected_active_source_type, created_at
            ) VALUES (?, ?, ?, ?, 'STAGING', ?, ?, 0, ?, ?, ?, ?, ?)
            """,
            (
                version_id,
                school_id,
                source_type.value,
                import_mode,
                source_document_id,
                parent_version_id,
                as_of_local_date.isoformat() if as_of_local_date else None,
                expected_active_version_id,
                expected_active_item_count,
                expected_active_source_type.value
                if expected_active_source_type is not None
                else None,
                format_utc(now),
            ),
        )
        return self.version(version_id)

    def put_holding(
        self,
        *,
        version_id: str,
        school_id: str,
        source_type: SourceType,
        record: CatalogRecord,
        created_at=None,
    ) -> str:
        normalized = normalize_book(record)
        now = format_utc(created_at or utc_now())
        stable_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"suseoro:{school_id}:{source_type.value}:{record.source_item_id}",
            )
        )
        existing = self.connection.execute(
            """
            SELECT id FROM holdings
            WHERE catalog_version_id = ? AND source_item_id = ?
            """,
            (version_id, record.source_item_id),
        ).fetchone()
        holding_id = existing["id"] if existing else str(uuid.uuid4())
        parameters = (
            stable_id,
            record.source_row_id,
            str(record.isbn) if record.isbn is not None else None,
            normalized.isbn13,
            record.title,
            record.subtitle,
            json.dumps(record.authors, ensure_ascii=False),
            record.publisher,
            record.volume,
            record.edition,
            record.series,
            record.publication_date,
            record.price,
            record.pages,
            record.kdc,
            record.registration_number,
            record.call_number,
            record.location,
            record.holding_status.value,
            json.dumps(record.raw_fields, ensure_ascii=False, sort_keys=True),
            now,
        )
        if existing:
            self.connection.execute(
                "DELETE FROM normalized_works WHERE holding_id = ?", (holding_id,)
            )
            self.connection.execute(
                """
                UPDATE holdings SET
                    stable_id = ?, source_row_id = ?, isbn_input = ?, isbn13 = ?,
                    original_title = ?, original_subtitle = ?,
                    original_authors_json = ?, original_publisher = ?,
                    original_volume = ?, original_edition = ?, original_series = ?,
                    publication_date = ?, price = ?, pages = ?, kdc = ?,
                    registration_number = ?, call_number = ?, location = ?,
                    holding_status = ?, raw_json = ?, created_at = ?
                WHERE id = ?
                """,
                (*parameters, holding_id),
            )
        else:
            self.connection.execute(
                """
                INSERT INTO holdings (
                    id, stable_id, catalog_version_id, school_id, source_item_id,
                    source_row_id, isbn_input, isbn13, original_title,
                    original_subtitle, original_authors_json, original_publisher,
                    original_volume, original_edition, original_series,
                    publication_date, price, pages, kdc, registration_number,
                    call_number, location, holding_status, raw_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    holding_id,
                    stable_id,
                    version_id,
                    school_id,
                    record.source_item_id,
                    *parameters[1:],
                ),
            )
        self.connection.execute(
            """
            INSERT INTO normalized_works (
                id, holding_id, catalog_version_id, school_id, title_key,
                subtitle_key, author_key, publisher_key, volume_key,
                edition_key, series_key, search_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                holding_id,
                version_id,
                school_id,
                normalized.title_key,
                normalized.subtitle_key,
                normalized.author_key,
                normalized.publisher_key,
                normalized.volume_key,
                normalized.edition_key,
                normalized.series_key,
                normalized.search_text,
            ),
        )
        return holding_id

    def copy_version(
        self, *, source_version_id: str, target_version_id: str, now=None
    ) -> None:
        target = self.version(target_version_id)
        source = self.version(source_version_id)
        if target is None or source is None:
            raise ValueError("catalog version does not exist")
        timestamp = format_utc(now or utc_now())
        self.connection.execute(
            f"""
            INSERT INTO holdings (
                id, stable_id, catalog_version_id, school_id, source_item_id,
                source_row_id, isbn_input, isbn13, original_title,
                original_subtitle, original_authors_json, original_publisher,
                original_volume, original_edition, original_series,
                publication_date, price, pages, kdc, registration_number,
                call_number, location, holding_status, raw_json, created_at
            )
            SELECT {_UUID_SQL}, stable_id, ?, school_id, source_item_id,
                   source_row_id, isbn_input, isbn13, original_title,
                   original_subtitle, original_authors_json, original_publisher,
                   original_volume, original_edition, original_series,
                   publication_date, price, pages, kdc, registration_number,
                   call_number, location, holding_status, raw_json, ?
            FROM holdings WHERE catalog_version_id = ?
            """,
            (target_version_id, timestamp, source_version_id),
        )
        self.connection.execute(
            f"""
            INSERT INTO normalized_works (
                id, holding_id, catalog_version_id, school_id, title_key,
                subtitle_key, author_key, publisher_key, volume_key,
                edition_key, series_key, search_text
            )
            SELECT {_UUID_SQL}, new_h.id, ?, old_nw.school_id,
                   old_nw.title_key, old_nw.subtitle_key, old_nw.author_key,
                   old_nw.publisher_key, old_nw.volume_key, old_nw.edition_key,
                   old_nw.series_key, old_nw.search_text
            FROM normalized_works old_nw
            JOIN holdings old_h ON old_h.id = old_nw.holding_id
            JOIN holdings new_h ON new_h.catalog_version_id = ?
                               AND new_h.source_item_id = old_h.source_item_id
            WHERE old_nw.catalog_version_id = ?
            """,
            (target_version_id, target_version_id, source_version_id),
        )

    def refresh_item_count(self, version_id: str) -> int:
        count = self.connection.execute(
            "SELECT COUNT(*) FROM holdings WHERE catalog_version_id = ?", (version_id,)
        ).fetchone()[0]
        self.connection.execute(
            "UPDATE catalog_versions SET item_count = ? WHERE id = ?",
            (count, version_id),
        )
        return count

    def exact_isbn_matches(self, school_id: str, isbn13: str) -> list[HoldingMatch]:
        return [
            _match(row)
            for row in self.connection.execute(
                _EXACT_ISBN_SQL, (school_id, isbn13)
            ).fetchall()
        ]

    def explain_exact_isbn_lookup(self, school_id: str, isbn13: str) -> list[str]:
        return [
            row[3]
            for row in self.connection.execute(
                "EXPLAIN QUERY PLAN " + _EXACT_ISBN_SQL, (school_id, isbn13)
            ).fetchall()
        ]

    def exact_title_author_matches(
        self, school_id: str, title_key: str, author_key: str
    ) -> list[HoldingMatch]:
        rows = self.connection.execute(
            """
            SELECT h.id, h.stable_id, h.source_item_id, h.isbn13,
                   h.holding_status, nw.title_key, nw.subtitle_key, nw.author_key,
                   nw.publisher_key, nw.volume_key, nw.edition_key, nw.series_key,
                   nw.search_text
            FROM normalized_works AS nw
                INDEXED BY idx_normalized_works_exact_title_author
            JOIN holdings AS h ON h.id = nw.holding_id
            JOIN catalog_versions AS cv ON cv.id = nw.catalog_version_id
            WHERE nw.school_id = ? AND nw.title_key = ? AND nw.author_key = ?
              AND cv.status = 'ACTIVE'
              AND h.holding_status IN ('AVAILABLE', 'UNCERTAIN')
            ORDER BY h.id
            """,
            (school_id, title_key, author_key),
        ).fetchall()
        return [_match(row) for row in rows]

    def fts_candidates(
        self, school_id: str, terms: tuple[str, ...], *, limit: int = 30
    ) -> list[HoldingMatch]:
        bounded_limit = min(max(limit, 0), 30)
        if not terms or bounded_limit == 0:
            return []
        escaped = [term.replace('"', '""') for term in terms[:24]]
        query = " OR ".join(f'"{term}"' for term in escaped)
        rows = self.connection.execute(
            """
            SELECT h.id, h.stable_id, h.source_item_id, h.isbn13,
                   h.holding_status, nw.title_key, nw.subtitle_key, nw.author_key,
                   nw.publisher_key, nw.volume_key, nw.edition_key, nw.series_key,
                   nw.search_text, bm25(holding_search_fts_index) AS rank
            FROM holding_search_fts_index
            JOIN holdings AS h ON h.id = holding_search_fts_index.holding_id
            JOIN normalized_works AS nw ON nw.holding_id = h.id
            JOIN catalog_versions AS cv ON cv.id = h.catalog_version_id
            WHERE holding_search_fts_index MATCH ?
              AND holding_search_fts_index.school_id = ?
              AND cv.status = 'ACTIVE'
              AND h.holding_status IN ('AVAILABLE', 'UNCERTAIN')
            ORDER BY rank, h.id
            LIMIT ?
            """,
            (query, school_id, bounded_limit),
        ).fetchall()
        return [_match(row) for row in rows]
