"""Inspect and copy a v1 workspace without modifying its source tree."""

from __future__ import annotations

import hashlib
import re
import shutil
import sqlite3
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from suseoro.db.connection import connect
from suseoro.ingestion.contracts import DocumentRole, RowStatus
from suseoro.ingestion.parsers.tabular import parse_tabular
from suseoro.security.sessions import format_utc, utc_now
from suseoro.services.audit import record_audit_event, record_system_audit_event

_WORKBOOK_SUFFIXES = {".xlsx", ".xls", ".xlsb", ".ods"}


@dataclass(frozen=True)
class V1SchoolReport:
    name: str
    source_path: Path
    catalog_candidate: Path | None
    catalog_candidate_row_count: int | None
    requires_catalog_confirmation: bool
    legacy_workspaces: tuple[Path, ...]
    catalog_caches: tuple[Path, ...]


@dataclass
class V1MigrationReport:
    source_root: Path
    schools: list[V1SchoolReport] = field(default_factory=list)
    school_count: int = 0
    legacy_workspace_count: int = 0
    catalog_cache_count: int = 0
    catalog_candidate_count: int = 0
    missing_count: int = 0
    duplicate_count: int = 0
    read_error_count: int = 0
    copied_count: int = 0
    skipped_existing_count: int = 0
    source_delete_recommended: bool = False
    source_retention_message: str = (
        "두 번의 실제 수서 회차를 검증하기 전에는 v1 원본을 삭제하지 마세요."
    )

    def to_dict(self) -> dict[str, object]:
        return {
            "source_root": str(self.source_root),
            "school_count": self.school_count,
            "legacy_workspace_count": self.legacy_workspace_count,
            "catalog_cache_count": self.catalog_cache_count,
            "catalog_candidate_count": self.catalog_candidate_count,
            "missing_count": self.missing_count,
            "duplicate_count": self.duplicate_count,
            "read_error_count": self.read_error_count,
            "copied_count": self.copied_count,
            "skipped_existing_count": self.skipped_existing_count,
            "source_delete_recommended": self.source_delete_recommended,
            "source_retention_message": self.source_retention_message,
            "schools": [
                {
                    "name": school.name,
                    "catalog_candidate": str(school.catalog_candidate)
                    if school.catalog_candidate
                    else None,
                    "catalog_candidate_row_count": school.catalog_candidate_row_count,
                    "requires_catalog_confirmation": school.requires_catalog_confirmation,
                    "legacy_workspaces": [
                        str(path) for path in school.legacy_workspaces
                    ],
                    "catalog_caches": [str(path) for path in school.catalog_caches],
                }
                for school in self.schools
            ],
        }


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _workbook_rows(path: Path) -> int:
    parsed = parse_tabular(
        path,
        role=DocumentRole.CATALOG_FULL,
        sha256=_digest(path),
    )
    if not parsed.rows:
        raise ValueError("workbook contains no catalog rows")
    for row in parsed.rows:
        title = row.fields.get("title")
        if (
            row.status != RowStatus.SUCCESS
            or title is None
            or title.value in (None, "")
        ):
            raise ValueError("workbook does not contain only usable catalog rows")
    return len(parsed.rows)


def _is_legacy(path: Path, school: Path) -> bool:
    relative = path.relative_to(school)
    if len(relative.parts) > 1 and any("차" in part for part in relative.parts[:-1]):
        return True
    stem = path.stem.casefold()
    return "검수" in stem or "최종" in stem


def inspect_v1(source_root: Path) -> V1MigrationReport:
    root = Path(source_root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("v1 원본은 폴더여야 합니다.")
    report = V1MigrationReport(source_root=root)
    seen: set[str] = set()
    for school_path in sorted(path for path in root.iterdir() if path.is_dir()):
        workbooks = sorted(
            path
            for path in school_path.rglob("*")
            if path.is_file() and path.suffix.casefold() in _WORKBOOK_SUFFIXES
        )
        caches = tuple(
            path for path in workbooks if path.name.casefold() == "catalog_master.xlsx"
        )
        legacy = tuple(
            path
            for path in workbooks
            if path not in caches and _is_legacy(path, school_path)
        )
        dls_files = [
            path
            for path in workbooks
            if path not in caches
            and path not in legacy
            and "dls" in path.stem.casefold()
        ]
        valid_dls: list[tuple[Path, int]] = []
        for path in dls_files:
            try:
                valid_dls.append((path, _workbook_rows(path)))
            except Exception:  # noqa: BLE001 - isolate every untrusted v1 workbook
                report.read_error_count += 1
        valid_dls.sort(
            key=lambda item: tuple(
                int(part) if part.isdigit() else part.casefold()
                for part in re.split(r"(\d+)", item[0].stem)
            ),
            reverse=True,
        )
        selected = valid_dls[0] if valid_dls else None
        categorized = list(legacy) + list(caches) + ([selected[0]] if selected else [])
        for path in categorized:
            digest = _digest(path)
            if digest in seen:
                report.duplicate_count += 1
            seen.add(digest)
        school = V1SchoolReport(
            name=school_path.name,
            source_path=school_path,
            catalog_candidate=selected[0] if selected else None,
            catalog_candidate_row_count=selected[1] if selected else None,
            requires_catalog_confirmation=selected is not None,
            legacy_workspaces=legacy,
            catalog_caches=caches,
        )
        report.schools.append(school)
        report.legacy_workspace_count += len(legacy)
        report.catalog_cache_count += len(caches)
        report.catalog_candidate_count += int(selected is not None)
        report.missing_count += int(selected is None)
    report.school_count = len(report.schools)
    return report


def _copy(source: Path, destination: Path, report: V1MigrationReport) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and _digest(destination) == _digest(source):
        report.skipped_existing_count += 1
        return
    if destination.exists():
        destination = destination.with_name(
            f"{destination.stem}-{_digest(source)[:8]}{destination.suffix}"
        )
        if destination.exists() and _digest(destination) == _digest(source):
            report.skipped_existing_count += 1
            return
    shutil.copy2(source, destination)
    report.copied_count += 1


def _register_visible_import(
    database_path: Path | None,
    report: V1MigrationReport,
    destination: Path,
    *,
    actor_id: str | None,
    actor_school_id: str | None,
    request_id: str,
    existing_connection: sqlite3.Connection | None = None,
) -> None:
    now = format_utc(utc_now())
    connection = existing_connection or connect(database_path)
    owns_connection = existing_connection is None
    try:
        imported_school_ids: list[str] = []
        for school in report.schools:
            existing = connection.execute(
                "SELECT id FROM schools WHERE name = ? ORDER BY created_at, id LIMIT 1",
                (school.name,),
            ).fetchone()
            school_id = existing["id"] if existing else str(uuid.uuid4())
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO schools (id, name, created_at, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (school_id, school.name, now, now),
                )
            imported_school_ids.append(school_id)
            # The authenticated tenant owns the imported read-only material.
            # We still register every discovered legacy school as application
            # metadata, but never publish records into a tenant that has no
            # authenticated administrator to review and activate them.
            owning_school_id = actor_school_id or school_id
            target = destination / "schools" / school.name
            for legacy in school.legacy_workspaces:
                copied = (
                    target
                    / "legacy-workspaces"
                    / legacy.relative_to(school.source_path)
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO legacy_v1_workspaces (
                        id, school_id, display_name, source_copy_path,
                        sha256, imported_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        owning_school_id,
                        legacy.parent.name + " / " + legacy.name,
                        str(copied),
                        _digest(legacy),
                        now,
                    ),
                )
            if school.catalog_candidate is not None:
                copied = target / "catalog-candidates" / school.catalog_candidate.name
                connection.execute(
                    """
                    INSERT OR IGNORE INTO v1_catalog_candidates (
                        id, school_id, source_copy_path, sha256,
                        row_count, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        owning_school_id,
                        str(copied),
                        _digest(school.catalog_candidate),
                        school.catalog_candidate_row_count,
                        now,
                    ),
                )
        audit_school = actor_school_id or (
            imported_school_ids[0] if imported_school_ids else None
        )
        if audit_school is not None:
            before = {"school_count": 0, "copied_count": 0}
            after = report.to_dict()
            if actor_id:
                record_audit_event(
                    connection,
                    actor_id=actor_id,
                    school_id=audit_school,
                    action="V1_MIGRATION_COMPLETED",
                    entity_type="v1_migration",
                    entity_id=None,
                    before=before,
                    after=after,
                    request_id=request_id,
                )
            else:
                record_system_audit_event(
                    connection,
                    school_id=audit_school,
                    action="V1_MIGRATION_COMPLETED",
                    entity_type="v1_migration",
                    entity_id=None,
                    before=before,
                    after=after,
                    request_id=request_id,
                )
        if owns_connection:
            connection.commit()
    finally:
        if owns_connection:
            connection.close()


def migrate_v1(
    source_root: Path,
    destination_root: Path,
    *,
    database_path: Path | None = None,
    connection: sqlite3.Connection | None = None,
    actor_id: str | None = None,
    actor_school_id: str | None = None,
    request_id: str = "v1-migration",
) -> V1MigrationReport:
    report = inspect_v1(source_root)
    destination = Path(destination_root).resolve()
    try:
        destination.relative_to(report.source_root)
    except ValueError:
        pass
    else:
        raise ValueError("이전 대상은 v1 원본 폴더 밖에 두어야 합니다.")
    for school in report.schools:
        target = destination / "schools" / school.name
        if school.catalog_candidate:
            _copy(
                school.catalog_candidate,
                target / "catalog-candidates" / school.catalog_candidate.name,
                report,
            )
        for cache in school.catalog_caches:
            _copy(cache, target / "catalog-cache" / cache.name, report)
        for legacy in school.legacy_workspaces:
            relative = legacy.relative_to(school.source_path)
            _copy(legacy, target / "legacy-workspaces" / relative, report)
    if database_path is not None or connection is not None:
        _register_visible_import(
            database_path,
            report,
            destination,
            actor_id=actor_id,
            actor_school_id=actor_school_id,
            request_id=request_id,
            existing_connection=connection,
        )
    return report
