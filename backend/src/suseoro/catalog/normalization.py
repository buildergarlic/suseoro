"""ISBN validation and lossless Korean-safe search-key normalization."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

from suseoro.catalog.contracts import CatalogRecord, NormalizedBook

_AUTHOR_ROLES = re.compile(r"(?:지음|글|그림|옮김|번역|역|저)\s*$")
_PUBLISHER_MARKERS = re.compile(
    r"(?:\(\s*주\s*\)|（\s*주\s*）|주식회사|유한회사|출판사)", re.IGNORECASE
)
_ISBN_PREFIX = re.compile(r"^\s*ISBN(?:-1[03])?\s*:?\s*", re.IGNORECASE)


def _isbn_digits(value: object) -> str:
    if value is None:
        return ""
    text = _ISBN_PREFIX.sub("", unicodedata.normalize("NFKC", str(value)))
    return re.sub(r"[^0-9Xx]", "", text).upper()


def _valid_isbn10(value: str) -> bool:
    if not re.fullmatch(r"[0-9]{9}[0-9X]", value):
        return False
    total = sum(
        (10 - index) * int(character) for index, character in enumerate(value[:9])
    )
    total += 10 if value[-1] == "X" else int(value[-1])
    return total % 11 == 0


def _valid_isbn13(value: str) -> bool:
    if not re.fullmatch(r"[0-9]{13}", value):
        return False
    total = sum(
        int(character) * (1 if index % 2 == 0 else 3)
        for index, character in enumerate(value[:12])
    )
    check = (10 - total % 10) % 10
    return check == int(value[-1])


def canonical_isbn13(value: object) -> str | None:
    """Validate ISBN-10/13 and return a digits-only ISBN-13."""
    digits = _isbn_digits(value)
    if len(digits) == 13:
        return digits if _valid_isbn13(digits) else None
    if len(digits) != 10 or not _valid_isbn10(digits):
        return None
    stem = "978" + digits[:9]
    total = sum(
        int(character) * (1 if index % 2 == 0 else 3)
        for index, character in enumerate(stem)
    )
    return stem + str((10 - total % 10) % 10)


def normalize_key(value: object) -> str:
    """Build a comparison key without altering the stored original value."""
    if value is None:
        return ""
    normalized = unicodedata.normalize("NFKC", str(value)).casefold()
    return "".join(character for character in normalized if character.isalnum())


def normalize_author(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    while text and _AUTHOR_ROLES.search(text):
        text = _AUTHOR_ROLES.sub("", text).strip()
    return normalize_key(text)


def normalize_authors(values: Iterable[object]) -> str:
    normalized = {normalize_author(value) for value in values}
    return "|".join(sorted(value for value in normalized if value))


def normalize_publisher(value: object) -> str:
    return normalize_key(_PUBLISHER_MARKERS.sub("", str(value or "")))


def normalize_numbered_part(value: object) -> str:
    key = normalize_key(value)
    numbers = re.findall(r"\d+", key)
    return ".".join(numbers) if numbers else key


def _ngrams(value: str, width: int = 2) -> list[str]:
    if not value:
        return []
    if len(value) <= width:
        return [value]
    return [value[index : index + width] for index in range(len(value) - width + 1)]


def search_terms(normalized: NormalizedBook) -> tuple[str, ...]:
    terms: list[str] = []
    for value in (
        normalized.title_key,
        normalized.subtitle_key,
        normalized.author_key.replace("|", ""),
        normalized.series_key,
    ):
        terms.extend(_ngrams(value))
    return tuple(dict.fromkeys(term for term in terms if term))


def normalize_book(record: CatalogRecord) -> NormalizedBook:
    title_key = normalize_key(record.title)
    subtitle_key = normalize_key(record.subtitle)
    author_key = normalize_authors(record.authors)
    publisher_key = normalize_publisher(record.publisher)
    volume_key = normalize_numbered_part(record.volume)
    edition_key = normalize_numbered_part(record.edition)
    series_key = normalize_key(record.series)
    provisional = NormalizedBook(
        original=record,
        isbn13=canonical_isbn13(record.isbn),
        title_key=title_key,
        subtitle_key=subtitle_key,
        author_key=author_key,
        publisher_key=publisher_key,
        volume_key=volume_key,
        edition_key=edition_key,
        series_key=series_key,
        search_text="",
    )
    return NormalizedBook(
        **{
            **provisional.__dict__,
            "search_text": " ".join(search_terms(provisional)),
        }
    )
