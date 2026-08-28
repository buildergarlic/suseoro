"""Verified SQLite online backups, retention, and atomic restore."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

from suseoro.db.connection import connect, quiesce_database
from suseoro.db.migrations import apply_migrations


class RestoreVerificationError(RuntimeError):
    pass


class LocalAdminConfirmationRequired(PermissionError):
    pass


@dataclass(frozen=True)
class BackupManifest:
    id: str
    kind: str
    created_at: datetime
    database_path: Path
    manifest_path: Path
    sha256: str
    size_bytes: int
    schema_migrations: tuple[dict[str, str], ...]
    verified: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "created_at": self.created_at.isoformat().replace("+00:00", "Z"),
            "database_file": self.database_path.name,
            "manifest_file": self.manifest_path.name,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "schema_migrations": list(self.schema_migrations),
            "verified": self.verified,
        }


@dataclass(frozen=True)
class RestoreResult:
    restored_manifest: BackupManifest
    pre_restore_backup: BackupManifest


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _schema(connection: sqlite3.Connection) -> tuple[dict[str, str], ...]:
    rows = connection.execute(
        "SELECT migration_id, checksum FROM schema_migrations ORDER BY migration_id"
    ).fetchall()
    return tuple({"version": str(row[0]), "checksum": str(row[1])} for row in rows)


def _verify_database(
    path: Path, expected_schema: tuple[dict[str, str], ...] | None = None
) -> tuple[dict[str, str], ...]:
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise RestoreVerificationError(f"integrity check failed: {integrity}")
            schema = _schema(connection)
        finally:
            connection.close()
    except (sqlite3.DatabaseError, OSError) as error:
        raise RestoreVerificationError("schema verification failed") from error
    if expected_schema is not None and schema != expected_schema:
        raise RestoreVerificationError("schema checksum mismatch")
    return schema


def _parse_created(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(UTC)
    return datetime.fromisoformat(str(value)).astimezone(UTC)


def select_retained_backups(
    backups: Iterable[dict[str, Any]],
    *,
    daily: int = 7,
    weekly: int = 4,
    monthly: int = 12,
) -> list[dict[str, Any]]:
    ordered = sorted(
        (
            {**item, "created_at": _parse_created(item["created_at"])}
            for item in backups
        ),
        key=lambda item: (item["created_at"], str(item.get("id", ""))),
        reverse=True,
    )
    kept: dict[str, dict[str, Any]] = {}

    def keep_buckets(key, count: int) -> None:
        buckets: set[object] = set()
        for item in ordered:
            bucket = key(item["created_at"])
            if bucket in buckets:
                continue
            if len(buckets) >= count:
                break
            buckets.add(bucket)
            kept[str(item["id"])] = item

    keep_buckets(lambda value: value.date(), daily)
    keep_buckets(lambda value: value.isocalendar()[:2], weekly)
    keep_buckets(lambda value: (value.year, value.month), monthly)
    return sorted(
        kept.values(),
        key=lambda item: (item["created_at"], str(item["id"])),
        reverse=True,
    )


class BackupService:
    _restore_lock: ClassVar[threading.RLock] = threading.RLock()
    _restore_results: ClassVar[dict[tuple[str, str], RestoreResult]] = {}

    def __init__(self, database_path: Path, backups_dir: Path) -> None:
        self.database_path = Path(database_path).resolve()
        self.backups_dir = Path(backups_dir).resolve()
        self.backups_dir.mkdir(parents=True, exist_ok=True)

    def create(
        self, *, kind: str = "daily", now: datetime | None = None
    ) -> BackupManifest:
        allowed = {
            "daily",
            "weekly",
            "monthly",
            "manual",
            "pre_upgrade",
            "pre_restore",
            "upgrade",
        }
        if kind not in allowed:
            raise ValueError("invalid backup kind")
        created = (now or datetime.now(UTC)).astimezone(UTC)
        backup_id = str(uuid.uuid4())
        stamp = created.strftime("%Y%m%dT%H%M%SZ")
        basename = f"{stamp}-{kind}-{backup_id}"
        final_database = self.backups_dir / f"{basename}.sqlite3"
        manifest_path = self.backups_dir / f"{basename}.manifest.json"
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{basename}-", suffix=".tmp", dir=self.backups_dir
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            source = sqlite3.connect(str(self.database_path))
            target = sqlite3.connect(str(temporary))
            try:
                source.backup(target)
            finally:
                target.close()
                source.close()
            schema = _verify_database(temporary)
            digest = _sha256(temporary)
            size = temporary.stat().st_size
            os.replace(temporary, final_database)
            manifest = BackupManifest(
                id=backup_id,
                kind=kind,
                created_at=created,
                database_path=final_database,
                manifest_path=manifest_path,
                sha256=digest,
                size_bytes=size,
                schema_migrations=schema,
                verified=True,
            )
            encoded = json.dumps(
                manifest.to_dict(), ensure_ascii=False, sort_keys=True, indent=2
            ).encode("utf-8")
            manifest_path.write_bytes(encoded)
            manifest_path.with_suffix(manifest_path.suffix + ".sha256").write_text(
                hashlib.sha256(encoded).hexdigest(), encoding="ascii"
            )
            if kind != "pre_restore":
                self._enforce_retention()
            return manifest
        finally:
            temporary.unlink(missing_ok=True)

    def _load_manifest(self, manifest_path: Path) -> BackupManifest:
        path = Path(manifest_path).resolve(strict=True)
        if path.parent != self.backups_dir:
            raise RestoreVerificationError("manifest path escapes backup directory")
        encoded = path.read_bytes()
        sidecar = path.with_suffix(path.suffix + ".sha256")
        if not sidecar.is_file() or not hmac.compare_digest(
            sidecar.read_text(encoding="ascii").strip(),
            hashlib.sha256(encoded).hexdigest(),
        ):
            raise RestoreVerificationError("manifest checksum mismatch")
        try:
            data = json.loads(encoded)
            database_path = (path.parent / data["database_file"]).resolve(strict=True)
            if database_path.parent != self.backups_dir:
                raise RestoreVerificationError("backup path escapes backup directory")
            schema = tuple(
                {"version": str(item["version"]), "checksum": str(item["checksum"])}
                for item in data["schema_migrations"]
            )
            return BackupManifest(
                id=str(data["id"]),
                kind=str(data["kind"]),
                created_at=_parse_created(data["created_at"]),
                database_path=database_path,
                manifest_path=path,
                sha256=str(data["sha256"]),
                size_bytes=int(data["size_bytes"]),
                schema_migrations=schema,
                verified=bool(data["verified"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise RestoreVerificationError(
                "manifest schema verification failed"
            ) from error

    def list(self) -> list[BackupManifest]:
        manifests = []
        for path in self.backups_dir.glob("*.manifest.json"):
            try:
                manifests.append(self._load_manifest(path))
            except RestoreVerificationError:
                continue
        return sorted(
            manifests, key=lambda item: (item.created_at, item.id), reverse=True
        )

    def _enforce_retention(self) -> None:
        all_manifests = self.list()
        manifests = [
            item
            for item in all_manifests
            if item.kind in {"daily", "weekly", "monthly"}
        ]
        retained = {
            item["id"]
            for item in select_retained_backups(
                [manifest.to_dict() for manifest in manifests]
            )
        }
        for manifest in manifests:
            if manifest.id in retained:
                continue
            manifest.database_path.unlink(missing_ok=True)
            manifest.manifest_path.unlink(missing_ok=True)
            manifest.manifest_path.with_suffix(
                manifest.manifest_path.suffix + ".sha256"
            ).unlink(missing_ok=True)

    @staticmethod
    def _bundled_schema() -> tuple[dict[str, str], ...]:
        migrations = Path(__file__).parents[1] / "db" / "migrations"
        return tuple(
            {
                "version": path.stem,
                "checksum": hashlib.sha256(
                    path.read_text(encoding="utf-8").encode("utf-8")
                ).hexdigest(),
            }
            for path in sorted(migrations.glob("*.sql"))
        )

    @classmethod
    def _validate_migration_history(cls, schema: tuple[dict[str, str], ...]) -> None:
        bundled = cls._bundled_schema()
        if (
            not schema
            or len(schema) > len(bundled)
            or tuple(bundled[: len(schema)]) != schema
        ):
            raise RestoreVerificationError(
                "backup migration history is future, incomplete, or altered"
            )

    def restore(
        self,
        manifest_path: Path,
        *,
        confirmation_token: str | None,
        expected_confirmation_token: str | None,
        local_request: bool,
        now: datetime | None = None,
        operation_key: str | None = None,
    ) -> RestoreResult:
        if (
            not local_request
            or not confirmation_token
            or not expected_confirmation_token
            or not hmac.compare_digest(confirmation_token, expected_confirmation_token)
        ):
            raise LocalAdminConfirmationRequired("local admin confirmation is required")
        replay_key = (str(self.database_path), operation_key or str(uuid.uuid4()))
        with self._restore_lock:
            replay = self._restore_results.get(replay_key)
            if replay is not None:
                return replay
            manifest = self._load_manifest(manifest_path)
            if not manifest.verified:
                raise RestoreVerificationError("backup is not marked verified")
            if not hmac.compare_digest(
                _sha256(manifest.database_path), manifest.sha256
            ):
                raise RestoreVerificationError("backup checksum mismatch")
            if manifest.database_path.stat().st_size != manifest.size_bytes:
                raise RestoreVerificationError("backup size mismatch")
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".restore-", suffix=".sqlite3", dir=self.database_path.parent
            )
            os.close(descriptor)
            temporary = Path(temporary_name)
            try:
                shutil.copyfile(manifest.database_path, temporary)
                actual_schema = _verify_database(temporary, manifest.schema_migrations)
                self._validate_migration_history(actual_schema)
                upgrade = connect(temporary)
                try:
                    apply_migrations(upgrade)
                    upgrade.commit()
                finally:
                    upgrade.close()
                _verify_database(temporary, self._bundled_schema())
                with quiesce_database(self.database_path):
                    pre_restore = self.create(kind="pre_restore", now=now)
                    checkpoint = sqlite3.connect(str(self.database_path))
                    try:
                        checkpoint.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    finally:
                        checkpoint.close()
                    os.replace(temporary, self.database_path)
                    for suffix in ("-wal", "-shm"):
                        Path(f"{self.database_path}{suffix}").unlink(missing_ok=True)
                result = RestoreResult(manifest, pre_restore)
                if operation_key is not None:
                    self._restore_results[replay_key] = result
                return result
            finally:
                temporary.unlink(missing_ok=True)
