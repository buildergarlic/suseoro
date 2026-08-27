from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from suseoro.config import Settings
from suseoro.db.connection import connect
from suseoro.db.migrations import apply_migrations
from suseoro.ingestion.contracts import DocumentRole
from suseoro.ingestion.mapping import infer_mapping
from suseoro.ingestion.templates import (
    MappingTemplateStore,
    ParserCache,
    template_signature,
)

SCHOOL_ID = "550e8400-e29b-41d4-a716-446655440300"
OTHER_SCHOOL_ID = "550e8400-e29b-41d4-a716-446655440301"
NOW = "2026-08-28T12:34:56Z"


def _database(data_dir: Path) -> tuple[Settings, sqlite3.Connection]:
    settings = Settings(data_dir=data_dir)
    connection = connect(settings.database_path)
    apply_migrations(connection)
    for school_id, name in ((SCHOOL_ID, "Main"), (OTHER_SCHOOL_ID, "Other")):
        connection.execute(
            "INSERT INTO schools (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (school_id, name, NOW, NOW),
        )
    connection.commit()
    return settings, connection


def test_mapping_infers_korean_synonyms_and_confidence() -> None:
    """Missing any required library/vendor synonym must fail this test."""
    headers = [
        "국제표준도서번호",
        "도서명",
        "지은이",
        "발행처",
        "부수",
        "정가",
        "등록 번호",
        "청구기호",
    ]

    result = infer_mapping(
        headers, [["9781", "책", "김", "출판", "2", "1000", "R1", "800-K"]]
    )

    assert result.mapping == {
        "국제표준도서번호": "isbn",
        "도서명": "title",
        "지은이": "author",
        "발행처": "publisher",
        "부수": "quantity",
        "정가": "unit_price",
        "등록 번호": "registration_number",
        "청구기호": "call_number",
    }
    assert result.confidence == 1.0
    assert result.questions == []


def test_low_confidence_mapping_returns_at_most_twenty_preview_rows_and_questions() -> (
    None
):
    """Silently guessing unknown columns or returning an unbounded preview must fail."""
    preview = [[f"value-{number}", number] for number in range(30)]

    result = infer_mapping(["알 수 없는 값", "수량 또는 가격"], preview)

    assert result.confidence < 0.75
    assert len(result.preview) == 20
    assert result.questions
    assert result.mapping["알 수 없는 값"] is None


def test_template_signature_ignores_order_and_optional_columns_but_not_required_semantics() -> (
    None
):
    """Order-only drift must reuse while required-semantic drift must not."""
    original = template_signature(
        ["ISBN", "제목", "저자"],
        DocumentRole.PURCHASE_REQUEST,
        required_fields={"isbn", "title"},
    )
    reordered_plus_optional = template_signature(
        ["비고", "저자", "도서명", "국제표준도서번호"],
        DocumentRole.PURCHASE_REQUEST,
        required_fields={"isbn", "title"},
    )
    changed_required = template_signature(
        ["등록번호", "제목", "저자"],
        DocumentRole.PURCHASE_REQUEST,
        required_fields={"registration_number", "title"},
    )

    assert original == reordered_plus_optional
    assert original != changed_required


def test_mapping_templates_are_scoped_and_required_change_is_not_silently_reused(
    data_dir: Path,
) -> None:
    """Cross-school/vendor reuse or incompatible required fields must fail this test."""
    _, connection = _database(data_dir)
    try:
        store = MappingTemplateStore(connection)
        template_id = store.save(
            school_id=SCHOOL_ID,
            vendor_scope="vendor-a",
            role=DocumentRole.PURCHASE_REQUEST,
            headers=["ISBN", "제목", "비고"],
            mapping={"ISBN": "isbn", "제목": "title", "비고": None},
            required_fields={"isbn", "title"},
            template_version="mapping-v1",
        )
        matched = store.find(
            school_id=SCHOOL_ID,
            vendor_scope="vendor-a",
            role=DocumentRole.PURCHASE_REQUEST,
            headers=["도서명", "메모", "국제표준도서번호"],
            required_fields={"isbn", "title"},
        )
        wrong_school = store.find(
            school_id=OTHER_SCHOOL_ID,
            vendor_scope="vendor-a",
            role=DocumentRole.PURCHASE_REQUEST,
            headers=["ISBN", "제목"],
            required_fields={"isbn", "title"},
        )
        wrong_required = store.find(
            school_id=SCHOOL_ID,
            vendor_scope="vendor-a",
            role=DocumentRole.PURCHASE_REQUEST,
            headers=["등록번호", "제목"],
            required_fields={"registration_number", "title"},
        )
    finally:
        connection.close()

    assert matched is not None
    assert matched.id == template_id
    assert matched.mapping["ISBN"] == "isbn"
    assert wrong_school is None
    assert wrong_required is None


def test_parser_cache_reuses_exact_sha_version_role_and_reports_cached(
    data_dir: Path,
) -> None:
    """Reparsing an identical tuple or reusing a different tuple must fail this test."""
    _, connection = _database(data_dir)
    calls: list[str] = []
    try:
        cache = ParserCache(connection)

        def parse() -> dict[str, object]:
            calls.append("parse")
            return {"rows": [{"status": "SUCCESS", "isbn": "00123"}]}

        first = cache.get_or_parse(
            sha256="a" * 64,
            parser_version="tabular-v1",
            role=DocumentRole.PURCHASE_REQUEST,
            parse=parse,
        )
        replay = cache.get_or_parse(
            sha256="a" * 64,
            parser_version="tabular-v1",
            role=DocumentRole.PURCHASE_REQUEST,
            parse=parse,
        )
        changed = cache.get_or_parse(
            sha256="a" * 64,
            parser_version="tabular-v2",
            role=DocumentRole.PURCHASE_REQUEST,
            parse=parse,
        )
    finally:
        connection.close()

    assert calls == ["parse", "parse"]
    assert first.cached is False
    assert replay.cached is True
    assert replay.result == first.result
    assert changed.cached is False


def test_parser_cache_validates_canonical_hash_and_role(data_dir: Path) -> None:
    """Persisting malformed cache identity must fail before storage."""
    _, connection = _database(data_dir)
    try:
        cache = ParserCache(connection)
        with pytest.raises(ValueError, match="sha256"):
            cache.get_or_parse(
                sha256="NOT-A-HASH",
                parser_version="v1",
                role=DocumentRole.UNKNOWN,
                parse=dict,
            )
    finally:
        connection.close()
