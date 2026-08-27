"""Column-semantic inference with explicit uncertainty."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any


def normalize_header(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value)).casefold().strip()
    return re.sub(r"[^0-9a-z가-힣]", "", normalized)


_SYNONYMS = {
    "isbn": {"isbn", "국제표준도서번호", "도서번호", "isbn13", "isbn10"},
    "title": {"제목", "도서명", "서명", "책제목", "품명"},
    "author": {"저자", "지은이", "글쓴이", "작가"},
    "publisher": {"출판사", "발행처", "출판", "발행사"},
    "quantity": {"수량", "부수", "권수", "주문수량"},
    "unit_price": {"단가", "정가", "가격", "공급가", "판매가"},
    "registration_number": {"등록번호", "도서등록번호", "원부번호"},
    "call_number": {"청구기호", "분류기호", "청구번호"},
}
_NORMALIZED_SYNONYMS = {
    normalize_header(alias): field
    for field, aliases in _SYNONYMS.items()
    for alias in aliases
}


def canonical_field_for_header(header: str) -> str | None:
    return _NORMALIZED_SYNONYMS.get(normalize_header(header))


@dataclass(frozen=True)
class MappingInference:
    mapping: dict[str, str | None]
    confidence: float
    preview: list[list[Any]]
    questions: list[str]


def infer_mapping(headers: list[str], rows: list[list[Any]]) -> MappingInference:
    mapping = {header: canonical_field_for_header(header) for header in headers}
    matched = sum(value is not None for value in mapping.values())
    confidence = matched / len(headers) if headers else 0.0
    questions: list[str] = []
    unknown = [header for header, field in mapping.items() if field is None]
    duplicates = {
        field
        for field in mapping.values()
        if field and list(mapping.values()).count(field) > 1
    }
    if confidence < 0.75:
        questions.append("어떤 열을 ISBN, 제목, 수량 등 표준 필드에 연결할까요?")
    if unknown:
        questions.append("의미를 확인할 열: " + ", ".join(unknown))
    if duplicates:
        questions.append("중복 의미를 확인할 필드: " + ", ".join(sorted(duplicates)))
    return MappingInference(mapping, confidence, rows[:20], questions)
