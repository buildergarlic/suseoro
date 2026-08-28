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
_ISBN_PRESENTATION = re.compile(
    r"^(?:ISBN(?:-(10|13))?\s*:?\s*)?(.+)$",
    re.IGNORECASE,
)
_ISBN_BODY = re.compile(
    r"(?:[0-9Xx]+|[0-9Xx]+(?:-[0-9Xx]+){3,4}|[0-9Xx]+(?: [0-9Xx]+){3,4})"
)


def _isbn_digits(value: object) -> str:
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value)).strip()
    presentation = _ISBN_PRESENTATION.fullmatch(text)
    if presentation is None:
        return ""
    declared_length, body = presentation.groups()
    if _ISBN_BODY.fullmatch(body) is None:
        return ""
    digits = re.sub(r"[ -]", "", body).upper()
    if declared_length is not None and len(digits) != int(declared_length):
        return ""
    return digits


def _valid_isbn10(value: str) -> bool:
    if not re.fullmatch(r"[0-9]{9}[0-9X]", value):
        return False
    total = sum(
        (10 - index) * int(character) for index, character in enumerate(value[:9])
    )
    total += 10 if value[-1] == "X" else int(value[-1])
    return total % 11 == 0


def _valid_isbn13(value: str) -> bool:
    if not re.fullmatch(r"(?:978|979)[0-9]{10}", value):
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


def _semantic_number(value: object) -> tuple[str, tuple[str, ...]]:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    return normalize_key(text), tuple(re.findall(r"\d+", text))


def normalize_volume(value: object) -> str:
    """Preserve part semantics while merging obvious volume-format variants."""
    key, numbers = _semantic_number(value)
    if not key:
        return ""
    qualifiers = (
        (("상권", "상편", "upper"), "upper"),
        (("중권", "중편", "middle"), "middle"),
        (("하권", "하편", "lower"), "lower"),
        (("부록", "appendix"), "appendix"),
        (("부", "part"), "part"),
        (("편",), "part"),
        (("권", "volume", "vol"), "volume"),
    )
    qualifier = next(
        (
            name
            for markers, name in qualifiers
            if any(marker in key for marker in markers)
        ),
        "volume" if numbers else key,
    )
    return f"{qualifier}:{'.'.join(numbers)}" if numbers else qualifier


def normalize_edition(value: object) -> str:
    """Keep edition/revision/printing qualifiers instead of numeric-only collapse."""
    text = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    key, numbers = _semantic_number(text)
    if not key:
        return ""
    edition_match = re.search(r"(?:제\s*)?(\d+)\s*(?:판|edition)", text)
    printing_match = re.search(r"(?:제\s*)?(\d+)\s*(?:쇄|printing|impression)", text)
    edition_number = edition_match.group(1) if edition_match else None
    printing_number = printing_match.group(1) if printing_match else None
    parts: list[str] = []
    if "개정" in key or "revision" in key or "revised" in key:
        parts.append(f"revision:{edition_number}" if edition_number else "revision")
    elif "초판" in key or "firstedition" in key:
        parts.append("edition:1")
    elif "판" in key or "edition" in key:
        parts.append(f"edition:{edition_number}" if edition_number else "edition")
    if "쇄" in key or "printing" in key or "impression" in key:
        parts.append(f"printing:{printing_number}" if printing_number else "printing")
    if parts:
        return "|".join(dict.fromkeys(parts))
    return f"edition:{'.'.join(numbers)}" if numbers else key


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
    volume_key = normalize_volume(record.volume)
    edition_key = normalize_edition(record.edition)
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
