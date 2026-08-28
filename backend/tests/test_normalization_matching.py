from __future__ import annotations

import importlib
import importlib.util
from types import SimpleNamespace

import pytest

from suseoro.db.connection import connect
from suseoro.db.migrations import apply_migrations

SCHOOL_ID = "10000000-0000-4000-8000-000000000001"
NOW = "2026-08-28T00:00:00Z"


def _api() -> SimpleNamespace:
    modules = (
        "suseoro.catalog.contracts",
        "suseoro.catalog.normalization",
        "suseoro.catalog.repository",
        "suseoro.catalog.sync",
        "suseoro.matching.engine",
    )
    missing = [name for name in modules if importlib.util.find_spec(name) is None]
    assert not missing, f"Task 5 modules are not implemented: {', '.join(missing)}"
    contracts = importlib.import_module(modules[0])
    normalization = importlib.import_module(modules[1])
    repository = importlib.import_module(modules[2])
    sync = importlib.import_module(modules[3])
    engine = importlib.import_module(modules[4])
    return SimpleNamespace(
        CatalogRecord=contracts.CatalogRecord,
        CandidateOutcome=contracts.CandidateOutcome,
        HoldingStatus=contracts.HoldingStatus,
        SourceType=contracts.SourceType,
        canonical_isbn13=normalization.canonical_isbn13,
        normalize_book=normalization.normalize_book,
        CatalogRepository=repository.CatalogRepository,
        CatalogSyncService=sync.CatalogSyncService,
        MatchingEngine=engine.MatchingEngine,
    )


def _database(tmp_path):
    connection = connect(tmp_path / "catalog.sqlite3")
    apply_migrations(connection)
    connection.execute(
        "INSERT INTO schools (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (SCHOOL_ID, "테스트 학교", NOW, NOW),
    )
    return connection


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0-306-40615-2", "9780306406157"),
        ("978-0-306-40615-7", "9780306406157"),
        ("ISBN 9780306406157", "9780306406157"),
        ("0306406153", None),
        ("9780306406158", None),
        ("", None),
        (None, None),
    ],
)
def test_isbn_checksum_validation_and_isbn10_conversion(raw, expected) -> None:
    """Accepting a bad checksum or retaining ISBN-10 would break exact matching."""
    api = _api()

    assert api.canonical_isbn13(raw) == expected


def test_korean_safe_normalization_preserves_originals_and_builds_separate_keys() -> (
    None
):
    """Mutating display text or dropping subtitle/volume/edition keys must fail."""
    api = _api()
    original = api.CatalogRecord(
        source_item_id="BIB-001",
        isbn="0-306-40615-2",
        title="  우리 집, 고양이! ",
        subtitle="두 번째 이야기",
        authors=("김 하늘 지음", "박 별"),
        publisher="(주) 푸른-숲",
        volume="제 2권",
        edition="개정 3판",
        series="마음 문고",
    )

    normalized = api.normalize_book(original)

    assert normalized.original is original
    assert normalized.isbn13 == "9780306406157"
    assert normalized.title_key == "우리집고양이"
    assert normalized.subtitle_key == "두번째이야기"
    assert normalized.author_key == "김하늘|박별"
    assert normalized.publisher_key == "푸른숲"
    assert normalized.volume_key == "2"
    assert normalized.edition_key == "3"
    assert normalized.series_key == "마음문고"
    assert original.title == "  우리 집, 고양이! "
    assert original.authors == ("김 하늘 지음", "박 별")


def test_exact_valid_isbn_excludes_but_edition_or_volume_conflict_requires_review(
    tmp_path,
) -> None:
    """Auto-excluding a different volume/edition on ISBN alone is unsafe."""
    api = _api()
    with _database(tmp_path) as connection:
        sync = api.CatalogSyncService(connection)
        sync.import_full_snapshot(
            school_id=SCHOOL_ID,
            source_type=api.SourceType.DLS_MARC,
            records=(
                api.CatalogRecord(
                    source_item_id="H-001",
                    isbn="9780306406157",
                    title="자료 구조",
                    authors=("김하늘",),
                    volume="1",
                    edition="초판",
                ),
            ),
            confirm_anomaly=True,
        )
        engine = api.MatchingEngine(api.CatalogRepository(connection), SCHOOL_ID)

        same = engine.classify(
            api.CatalogRecord(
                source_item_id="R-001",
                isbn="9780306406157",
                title="자료 구조",
                authors=("김하늘",),
                volume="1",
                edition="초판",
            )
        )
        conflict = engine.classify(
            api.CatalogRecord(
                source_item_id="R-002",
                isbn="9780306406157",
                title="자료 구조",
                authors=("김하늘",),
                volume="2",
                edition="개정판",
            )
        )

    assert same.outcome == api.CandidateOutcome.EXCLUDED
    assert same.reason == "EXACT_VALID_ISBN"
    assert same.evidence_holding_id is not None
    assert conflict.outcome == api.CandidateOutcome.NEEDS_REVIEW
    assert conflict.reason == "ISBN_EDITION_OR_VOLUME_CONFLICT"
    assert conflict.evidence_holding_id == same.evidence_holding_id


def test_invalid_isbn_never_auto_excludes_and_no_isbn_exact_title_author_is_review(
    tmp_path,
) -> None:
    """Treating malformed or absent ISBN as exact evidence must fail."""
    api = _api()
    with _database(tmp_path) as connection:
        sync = api.CatalogSyncService(connection)
        sync.import_full_snapshot(
            school_id=SCHOOL_ID,
            source_type=api.SourceType.DLS_EXCEL,
            records=(
                api.CatalogRecord(
                    source_item_id="H-001",
                    isbn="9780306406157",
                    title="한글 띄어쓰기",
                    authors=("김 하늘 지음",),
                ),
            ),
            confirm_anomaly=True,
        )
        engine = api.MatchingEngine(api.CatalogRepository(connection), SCHOOL_ID)

        malformed = engine.classify(
            api.CatalogRecord(
                source_item_id="R-001",
                isbn="9780306406158",
                title="전혀 다른 책",
                authors=("다른 사람",),
            )
        )
        no_isbn = engine.classify(
            api.CatalogRecord(
                source_item_id="R-002",
                isbn=None,
                title="한글, 띄어쓰기!",
                authors=("김하늘",),
            )
        )

    assert malformed.outcome == api.CandidateOutcome.CANDIDATE
    assert malformed.reason == "NO_CATALOG_MATCH_INVALID_ISBN"
    assert no_isbn.outcome == api.CandidateOutcome.NEEDS_REVIEW
    assert no_isbn.reason == "EXACT_TITLE_AUTHOR_WITHOUT_ISBN"
    assert no_isbn.evidence_holding_id is not None


def test_withdrawn_holding_is_not_presence_evidence_but_uncertain_holding_is_reviewed(
    tmp_path,
) -> None:
    """Withdrawn books must not exclude; uncertain holdings must not silently decide."""
    api = _api()
    with _database(tmp_path) as connection:
        sync = api.CatalogSyncService(connection)
        sync.import_full_snapshot(
            school_id=SCHOOL_ID,
            source_type=api.SourceType.DLS_MARC,
            records=(
                api.CatalogRecord(
                    source_item_id="H-WITHDRAWN",
                    isbn="9780306406157",
                    title="제적된 책",
                    authors=("가나다",),
                    holding_status=api.HoldingStatus.WITHDRAWN,
                ),
                api.CatalogRecord(
                    source_item_id="H-UNCERTAIN",
                    isbn="9781861972712",
                    title="상태 불확실",
                    authors=("라마바",),
                    holding_status=api.HoldingStatus.UNCERTAIN,
                ),
            ),
            confirm_anomaly=True,
        )
        engine = api.MatchingEngine(api.CatalogRepository(connection), SCHOOL_ID)

        withdrawn = engine.classify(
            api.CatalogRecord(
                source_item_id="R-001",
                isbn="9780306406157",
                title="제적된 책",
                authors=("가나다",),
            )
        )
        uncertain = engine.classify(
            api.CatalogRecord(
                source_item_id="R-002",
                isbn="9781861972712",
                title="상태 불확실",
                authors=("라마바",),
            )
        )

    assert withdrawn.outcome == api.CandidateOutcome.CANDIDATE
    assert withdrawn.reason == "NO_CATALOG_MATCH"
    assert withdrawn.evidence_holding_id is None
    assert uncertain.outcome == api.CandidateOutcome.NEEDS_REVIEW
    assert uncertain.reason == "UNCERTAIN_HOLDING_MATCH"
    assert uncertain.evidence_holding_id is not None


def test_exact_isbn_repository_query_uses_the_school_isbn_index(tmp_path) -> None:
    """Removing the exact ISBN index or bypassing it in repository SQL must fail."""
    api = _api()
    with _database(tmp_path) as connection:
        sync = api.CatalogSyncService(connection)
        sync.import_full_snapshot(
            school_id=SCHOOL_ID,
            source_type=api.SourceType.DLS_MARC,
            records=(
                api.CatalogRecord(
                    source_item_id="H-001",
                    isbn="9780306406157",
                    title="인덱스 검사",
                    authors=("저자",),
                ),
            ),
            confirm_anomaly=True,
        )
        repository = api.CatalogRepository(connection)

        plan = repository.explain_exact_isbn_lookup(SCHOOL_ID, "9780306406157")

    assert any("idx_holdings_school_isbn" in detail for detail in plan), plan
    assert not any("SCAN holdings" in detail for detail in plan), plan


def test_fuzzy_engine_scores_no_more_than_thirty_fts_candidates(tmp_path) -> None:
    """Removing the SQL LIMIT would recreate the all-holdings Python loop."""
    api = _api()
    scored: list[tuple[str, str]] = []

    def scorer(query: str, candidate: str) -> float:
        scored.append((query, candidate))
        return 91.0

    with _database(tmp_path) as connection:
        sync = api.CatalogSyncService(connection)
        sync.import_full_snapshot(
            school_id=SCHOOL_ID,
            source_type=api.SourceType.DLS_MARC,
            records=tuple(
                api.CatalogRecord(
                    source_item_id=f"H-{index:03d}",
                    isbn=None,
                    title=f"우주 도서관 탐험 {index}",
                    authors=(f"작가 {index}",),
                )
                for index in range(75)
            ),
            confirm_anomaly=True,
        )
        engine = api.MatchingEngine(
            api.CatalogRepository(connection), SCHOOL_ID, scorer=scorer
        )

        decision = engine.classify(
            api.CatalogRecord(
                source_item_id="R-001",
                isbn=None,
                title="우주 도서관 탐험 이야기",
                authors=("새 작가",),
            )
        )

    assert decision.outcome == api.CandidateOutcome.NEEDS_REVIEW
    assert decision.reason == "FUZZY_CATALOG_MATCH"
    assert 1 <= len(scored) <= 30
