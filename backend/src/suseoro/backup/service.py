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
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

from suseoro.db.connection import connect, quiesce_database
from suseoro.db.migrations import apply_migrations


class RestoreVerificationError(RuntimeError):
    pass


class LocalAdminConfirmationRequired(PermissionError):
    pass


class RestoreReplayConflict(RuntimeError):
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
    replayed: bool = field(default=False, compare=False)


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
        if schema and len(schema) <= len(bundled):
            if tuple(bundled[: len(schema)]) == schema:
                return
            historical_0011: list[dict[str, str]] = []
            for migration in bundled:
                if migration["version"] == "0010a_task8_round4_upgrade_prelude":
                    continue
                historical_0011.append(migration)
                if migration["version"] == "0011_task8_round3_integrity":
                    break
            if tuple(historical_0011) == schema:
                return
        raise RestoreVerificationError(
            "backup migration history is future, incomplete, or altered"
        )

    def _open_restore_journal(self) -> sqlite3.Connection:
        journal = sqlite3.connect(
            str(self.backups_dir / "restore-operations.sqlite3"),
            timeout=60,
        )
        journal.row_factory = sqlite3.Row
        journal.execute("PRAGMA synchronous=FULL")
        journal.execute(
            """
            CREATE TABLE IF NOT EXISTS restore_operations (
                operation_key TEXT PRIMARY KEY,
                request_fingerprint TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('RUNNING', 'SUCCEEDED')),
                phase TEXT NOT NULL DEFAULT 'RUNNING',
                result_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        columns = {
            str(row[1])
            for row in journal.execute("PRAGMA table_info(restore_operations)")
        }
        if "phase" not in columns:
            journal.execute(
                "ALTER TABLE restore_operations "
                "ADD COLUMN phase TEXT NOT NULL DEFAULT 'RUNNING'"
            )
        journal.execute(
            "UPDATE restore_operations SET phase = 'SUCCEEDED' "
            "WHERE status = 'SUCCEEDED' AND phase <> 'SUCCEEDED'"
        )
        journal.commit()
        return journal

    def _result_from_journal(self, encoded: str) -> RestoreResult:
        data = json.loads(encoded)
        restored = self._load_manifest(
            self.backups_dir / str(data["restored_manifest_file"])
        )
        pre_restore = self._load_manifest(
            self.backups_dir / str(data["pre_restore_manifest_file"])
        )
        return RestoreResult(restored, pre_restore, replayed=True)

    @staticmethod
    def _encoded_restore_result(result: RestoreResult, target_sha256: str) -> str:
        return json.dumps(
            {
                "restored_manifest_file": result.restored_manifest.manifest_path.name,
                "pre_restore_manifest_file": result.pre_restore_backup.manifest_path.name,
                "target_sha256": target_sha256,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _write_restore_phase(
        journal: sqlite3.Connection,
        *,
        operation_key: str,
        fingerprint: str,
        phase: str,
        result_json: str | None = None,
        succeeded: bool = False,
    ) -> None:
        timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        updated = journal.execute(
            """
            UPDATE restore_operations
            SET status = ?, phase = ?, result_json = COALESCE(?, result_json),
                updated_at = ?
            WHERE operation_key = ? AND request_fingerprint = ?
            """,
            (
                "SUCCEEDED" if succeeded else "RUNNING",
                phase,
                result_json,
                timestamp,
                operation_key,
                fingerprint,
            ),
        )
        if updated.rowcount != 1:
            journal.rollback()
            raise RestoreReplayConflict("restore journal reservation was lost")
        journal.commit()

    def _perform_restore(
        self,
        manifest_path: Path,
        *,
        now: datetime | None = None,
        before_swap: Callable[[RestoreResult, str], None] | None = None,
        after_swap: Callable[[], None] | None = None,
    ) -> RestoreResult:
        manifest = self._load_manifest(manifest_path)
        if not manifest.verified:
            raise RestoreVerificationError("backup is not marked verified")
        if not hmac.compare_digest(_sha256(manifest.database_path), manifest.sha256):
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
            target_sha256 = _sha256(temporary)
            with quiesce_database(self.database_path):
                pre_restore = self.create(kind="pre_restore", now=now)
                checkpoint = sqlite3.connect(str(self.database_path))
                try:
                    checkpoint.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                finally:
                    checkpoint.close()
                result = RestoreResult(manifest, pre_restore)
                if before_swap is not None:
                    before_swap(result, target_sha256)
                os.replace(temporary, self.database_path)
                for suffix in ("-wal", "-shm"):
                    Path(f"{self.database_path}{suffix}").unlink(missing_ok=True)
                if after_swap is not None:
                    after_swap()
            return result
        finally:
            temporary.unlink(missing_ok=True)

    def restore(
        self,
        manifest_path: Path,
        *,
        confirmation_token: str | None,
        expected_confirmation_token: str | None,
        local_request: bool,
        now: datetime | None = None,
        operation_key: str | None = None,
        request_fingerprint: str | None = None,
    ) -> RestoreResult:
        if (
            not local_request
            or not confirmation_token
            or not expected_confirmation_token
            or not hmac.compare_digest(confirmation_token, expected_confirmation_token)
        ):
            raise LocalAdminConfirmationRequired("local admin confirmation is required")
        if operation_key is None:
            with self._restore_lock:
                return self._perform_restore(manifest_path, now=now)
        fingerprint = request_fingerprint or operation_key
        with self._restore_lock:
            journal = self._open_restore_journal()
            try:
                journal.execute("BEGIN IMMEDIATE")
                existing = journal.execute(
                    "SELECT * FROM restore_operations WHERE operation_key = ?",
                    (operation_key,),
                ).fetchone()
                if existing is not None:
                    if not hmac.compare_digest(
                        existing["request_fingerprint"], fingerprint
                    ):
                        raise RestoreReplayConflict(
                            "restore idempotency key was reused with another request"
                        )
                    if existing["status"] == "SUCCEEDED" and existing["result_json"]:
                        result = self._result_from_journal(existing["result_json"])
                        journal.commit()
                        return result
                    if (
                        existing["phase"] in {"SWAP_READY", "SWAPPED"}
                        and existing["result_json"]
                    ):
                        encoded = str(existing["result_json"])
                        data = json.loads(encoded)
                        if existing["phase"] == "SWAP_READY":
                            expected_digest = str(data["target_sha256"])
                            if (
                                not self.database_path.is_file()
                                or not hmac.compare_digest(
                                    _sha256(self.database_path), expected_digest
                                )
                            ):
                                raise RestoreVerificationError(
                                    "restore swap state cannot be recovered safely"
                                )
                            self._write_restore_phase(
                                journal,
                                operation_key=operation_key,
                                fingerprint=fingerprint,
                                phase="SWAPPED",
                                result_json=encoded,
                            )
                        result = self._result_from_journal(encoded)
                        self._write_restore_phase(
                            journal,
                            operation_key=operation_key,
                            fingerprint=fingerprint,
                            phase="SUCCEEDED",
                            result_json=encoded,
                            succeeded=True,
                        )
                        return result
                timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
                journal.execute(
                    """
                    INSERT INTO restore_operations (
                        operation_key, request_fingerprint, status, phase,
                        result_json, created_at, updated_at
                    ) VALUES (?, ?, 'RUNNING', 'RUNNING', NULL, ?, ?)
                    ON CONFLICT (operation_key) DO UPDATE SET
                        status = 'RUNNING', phase = 'RUNNING', result_json = NULL,
                        updated_at = excluded.updated_at
                    """,
                    (operation_key, fingerprint, timestamp, timestamp),
                )
                journal.commit()
                encoded: str | None = None

                def record_swap_ready(
                    pending: RestoreResult, target_sha256: str
                ) -> None:
                    nonlocal encoded
                    encoded = self._encoded_restore_result(pending, target_sha256)
                    self._write_restore_phase(
                        journal,
                        operation_key=operation_key,
                        fingerprint=fingerprint,
                        phase="SWAP_READY",
                        result_json=encoded,
                    )

                def record_swapped() -> None:
                    self._write_restore_phase(
                        journal,
                        operation_key=operation_key,
                        fingerprint=fingerprint,
                        phase="SWAPPED",
                        result_json=encoded,
                    )

                result = self._perform_restore(
                    manifest_path,
                    now=now,
                    before_swap=record_swap_ready,
                    after_swap=record_swapped,
                )
                if encoded is None:
                    raise RestoreVerificationError("restore journal result is missing")
                self._write_restore_phase(
                    journal,
                    operation_key=operation_key,
                    fingerprint=fingerprint,
                    phase="SUCCEEDED",
                    result_json=encoded,
                    succeeded=True,
                )
                return result
            except BaseException:
                journal.rollback()
                raise
            finally:
                journal.close()
