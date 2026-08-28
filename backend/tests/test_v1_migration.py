from __future__ import annotations

import hashlib
from pathlib import Path

from openpyxl import Workbook

from suseoro.config import Settings
from suseoro.db.connection import connect
from suseoro.db.migrations import apply_migrations
from suseoro.migration.v1 import inspect_v1, migrate_v1


def _xlsx(path: Path, rows: list[list[object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    worksheet = workbook.active
    for row in rows:
        worksheet.append(row)
    workbook.save(path)


def _snapshot(root: Path) -> dict[str, tuple[str, int, int]]:
    return {
        path.relative_to(root).as_posix(): (
            hashlib.sha256(path.read_bytes()).hexdigest(),
            path.stat().st_size,
            path.stat().st_mtime_ns,
        )
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _v1_tree(root: Path) -> None:
    _xlsx(
        root / "한빛학교" / "DLS_20260801.xlsx", [["ISBN", "자료명"], ["1", "옛 장서"]]
    )
    _xlsx(
        root / "한빛학교" / "DLS_20260820.xlsx",
        [["ISBN", "자료명"], ["2", "최신 장서"]],
    )
    (root / "한빛학교" / "DLS_20260825.xlsx").write_bytes(b"broken-xlsx")
    _xlsx(root / "한빛학교" / "2026-1차" / "검수.xlsx", [["ISBN"], ["3"]])
    _xlsx(root / "한빛학교" / "2026-1차" / "최종.xlsx", [["ISBN"], ["3"]])
    _xlsx(root / "한빛학교" / "catalog_master.xlsx", [["ISBN", "서명"], ["4", "캐시"]])
    _xlsx(root / "DLS없는학교" / "2025-2차" / "최종.xlsx", [["ISBN"], ["5"]])


def test_inspection_classifies_schools_rounds_latest_valid_dls_and_cache(
    tmp_path: Path,
) -> None:
    source = tmp_path / "v1-workspace"
    _v1_tree(source)

    report = inspect_v1(source)

    assert report.school_count == 2
    assert report.legacy_workspace_count == 3
    assert report.catalog_cache_count == 1
    assert report.catalog_candidate_count == 1
    assert report.missing_count == 1
    assert report.read_error_count == 1
    hanbit = next(school for school in report.schools if school.name == "한빛학교")
    selected = hanbit.catalog_candidate
    assert selected is not None
    assert selected.name == "DLS_20260820.xlsx"
    assert hanbit.catalog_candidate_row_count == 1
    assert hanbit.requires_catalog_confirmation is True


def test_migration_copies_categories_and_never_mutates_or_deletes_v1(
    tmp_path: Path,
) -> None:
    source = tmp_path / "v1-workspace"
    destination = tmp_path / "v2-import"
    _v1_tree(source)
    before = _snapshot(source)

    report = migrate_v1(source, destination)

    assert _snapshot(source) == before
    assert report.copied_count == 5
    copied_names = {
        path.relative_to(destination).as_posix() for path in destination.rglob("*.xlsx")
    }
    assert any("catalog-candidates/DLS_20260820.xlsx" in name for name in copied_names)
    assert any("catalog-cache/catalog_master.xlsx" in name for name in copied_names)
    assert sum("legacy-workspaces" in name for name in copied_names) == 3
    assert all("DLS_20260825.xlsx" not in name for name in copied_names)
    assert report.source_delete_recommended is False
    assert "두 번" in report.source_retention_message


def test_migration_reports_duplicate_content_without_overwriting_prior_copy(
    tmp_path: Path,
) -> None:
    source = tmp_path / "v1-workspace"
    destination = tmp_path / "v2-import"
    _v1_tree(source)
    duplicate = source / "한빛학교" / "2026-2차" / "최종.xlsx"
    duplicate.parent.mkdir(parents=True)
    duplicate.write_bytes((source / "한빛학교" / "2026-1차" / "최종.xlsx").read_bytes())

    first = migrate_v1(source, destination)
    second = migrate_v1(source, destination)

    assert first.duplicate_count >= 1
    assert second.duplicate_count >= first.duplicate_count
    assert second.copied_count == 0
    assert second.skipped_existing_count == 6


def test_migration_rejects_destination_inside_source_tree(tmp_path: Path) -> None:
    source = tmp_path / "v1-workspace"
    _v1_tree(source)

    try:
        migrate_v1(source, source / "unsafe-destination")
    except ValueError as error:
        assert "원본 폴더 밖" in str(error)
    else:
        raise AssertionError("a destination inside v1 must be rejected")


def test_latest_healthy_dls_uses_natural_order_and_rejects_zero_or_arbitrary_bytes(
    tmp_path: Path,
) -> None:
    source = tmp_path / "v1-workspace"
    school = source / "자연정렬학교"
    _xlsx(school / "DLS_9.xlsx", [["ISBN", "자료명"], ["9", "아홉"]])
    _xlsx(school / "DLS_10.xlsx", [["ISBN", "자료명"], ["10", "열"]])
    _xlsx(school / "DLS_12.xlsx", [["ISBN", "자료명"]])
    (school / "DLS_11.xls").write_bytes(b"not-a-workbook")

    report = inspect_v1(source)
    selected = report.schools[0].catalog_candidate

    assert selected is not None and selected.name == "DLS_10.xlsx"
    assert report.schools[0].catalog_candidate_row_count == 1
    assert report.read_error_count == 2


def test_migration_registers_visible_schools_read_only_history_and_pending_catalog(
    tmp_path: Path,
) -> None:
    source = tmp_path / "v1-workspace"
    destination = tmp_path / "v2-import"
    _v1_tree(source)
    settings = Settings(data_dir=tmp_path / "data")
    with connect(settings.database_path) as connection:
        apply_migrations(connection)
        connection.commit()

    report = migrate_v1(
        source,
        destination,
        database_path=settings.database_path,
        request_id="550e8400-e29b-41d4-a716-446655440200",
    )

    with connect(settings.database_path) as connection:
        schools = connection.execute(
            "SELECT name FROM schools ORDER BY name"
        ).fetchall()
        history = connection.execute(
            "SELECT display_name, is_read_only FROM legacy_v1_workspaces"
        ).fetchall()
        candidates = connection.execute(
            "SELECT status, row_count FROM v1_catalog_candidates"
        ).fetchall()
        active = connection.execute(
            "SELECT COUNT(*) FROM catalog_versions WHERE status = 'ACTIVE'"
        ).fetchone()[0]
        events = connection.execute(
            "SELECT action, request_id FROM audit_events WHERE action = 'V1_MIGRATION_COMPLETED'"
        ).fetchall()

    assert {row["name"] for row in schools} == {"DLS없는학교", "한빛학교"}
    assert history and all(row["is_read_only"] == 1 for row in history)
    assert [(row["status"], row["row_count"]) for row in candidates] == [
        ("PENDING_CONFIRMATION", 1)
    ]
    assert active == 0
    assert report.catalog_cache_count == 1
    assert [(row["action"], row["request_id"]) for row in events] == [
        ("V1_MIGRATION_COMPLETED", "550e8400-e29b-41d4-a716-446655440200")
    ]
