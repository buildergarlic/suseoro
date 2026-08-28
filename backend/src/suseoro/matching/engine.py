"""Bounded indexed matching with conservative automatic exclusion."""

from __future__ import annotations

from collections.abc import Callable

from rapidfuzz import fuzz

from suseoro.catalog.contracts import (
    CandidateOutcome,
    CatalogRecord,
    HoldingMatch,
    HoldingStatus,
    MatchingDecision,
    NormalizedBook,
)
from suseoro.catalog.normalization import normalize_book, search_terms
from suseoro.catalog.repository import CatalogRepository

FUZZY_REVIEW_THRESHOLD = 86.0


def _comparison_text(book: NormalizedBook | HoldingMatch) -> str:
    return (
        f"{book.title_key}|{book.subtitle_key}|{book.author_key}|"
        f"{book.publisher_key}|{book.volume_key}|{book.edition_key}|{book.series_key}"
    )


def _edition_or_volume_conflicts(
    recommendation: NormalizedBook, holding: HoldingMatch
) -> bool:
    return any(
        recommendation_value and holding_value and recommendation_value != holding_value
        for recommendation_value, holding_value in (
            (recommendation.volume_key, holding.volume_key),
            (recommendation.edition_key, holding.edition_key),
        )
    )


class MatchingEngine:
    def __init__(
        self,
        repository: CatalogRepository,
        school_id: str,
        *,
        scorer: Callable[[str, str], float] | None = None,
    ) -> None:
        self.repository = repository
        self.school_id = school_id
        self.scorer = scorer or fuzz.WRatio

    def classify(self, record: CatalogRecord) -> MatchingDecision:
        normalized = normalize_book(record)
        if normalized.isbn13:
            isbn_matches = self.repository.exact_isbn_matches(
                self.school_id, normalized.isbn13
            )
            available = [
                holding
                for holding in isbn_matches
                if holding.holding_status == HoldingStatus.AVAILABLE
            ]
            for holding in available:
                if not _edition_or_volume_conflicts(normalized, holding):
                    return MatchingDecision(
                        CandidateOutcome.EXCLUDED,
                        "EXACT_VALID_ISBN",
                        holding.id,
                        100.0,
                    )
            if available:
                return MatchingDecision(
                    CandidateOutcome.NEEDS_REVIEW,
                    "ISBN_EDITION_OR_VOLUME_CONFLICT",
                    available[0].id,
                )
            for holding in isbn_matches:
                if holding.holding_status == HoldingStatus.UNCERTAIN:
                    return MatchingDecision(
                        CandidateOutcome.NEEDS_REVIEW,
                        "UNCERTAIN_HOLDING_MATCH",
                        holding.id,
                    )

        if normalized.title_key and normalized.author_key:
            exact_matches = self.repository.exact_title_author_matches(
                self.school_id, normalized.title_key, normalized.author_key
            )
            if exact_matches:
                holding = exact_matches[0]
                if holding.holding_status == HoldingStatus.UNCERTAIN:
                    reason = "UNCERTAIN_HOLDING_MATCH"
                elif not normalized.isbn13:
                    reason = "EXACT_TITLE_AUTHOR_WITHOUT_ISBN"
                else:
                    reason = "EXACT_TITLE_AUTHOR_CATALOG_MATCH"
                return MatchingDecision(
                    CandidateOutcome.NEEDS_REVIEW, reason, holding.id, 100.0
                )

        best: tuple[float, HoldingMatch] | None = None
        candidates = self.repository.fts_candidates(
            self.school_id, search_terms(normalized), limit=30
        )
        query_text = _comparison_text(normalized)
        for holding in candidates:
            score = float(self.scorer(query_text, _comparison_text(holding)))
            if best is None or score > best[0]:
                best = (score, holding)
        if best is not None and best[0] >= FUZZY_REVIEW_THRESHOLD:
            score, holding = best
            reason = (
                "UNCERTAIN_HOLDING_MATCH"
                if holding.holding_status == HoldingStatus.UNCERTAIN
                else "FUZZY_CATALOG_MATCH"
            )
            return MatchingDecision(
                CandidateOutcome.NEEDS_REVIEW, reason, holding.id, score
            )

        reason = (
            "NO_CATALOG_MATCH_INVALID_ISBN"
            if record.isbn and normalized.isbn13 is None
            else "NO_CATALOG_MATCH"
        )
        return MatchingDecision(CandidateOutcome.CANDIDATE, reason)
