"""Streaming immutable storage for original upload bytes."""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from suseoro.ingestion.detection import detect_file_type
from suseoro.ingestion.safety import inspect_zip


class FileTooLarge(ValueError):
    pass


class UnsupportedFileType(ValueError):
    pass


class StagedUploadRejected(Exception):
    """Carry a complete content fingerprint for a rejected staged upload."""

    def __init__(self, cause: ValueError, *, sha256: str, size: int) -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.sha256 = sha256
        self.size = size


@dataclass(frozen=True)
class StagedFile:
    sha256: str
    size: int
    temporary_path: Path
    detected_format: str
    original_filename: str


@dataclass(frozen=True)
class StoredFile:
    sha256: str
    size: int
    path: Path
    detected_format: str
    created: bool
    original_filename: str


class ImmutableFileStore:
    def __init__(self, root: Path, *, max_bytes: int = 100 * 1024 * 1024) -> None:
        self.root = Path(root)
        self.max_bytes = max_bytes
        self.root.mkdir(parents=True, exist_ok=True)
        self.staging_root = self.root / ".staging"
        self.staging_root.mkdir(parents=True, exist_ok=True)

    def stage(self, chunks: Iterable[bytes], *, filename: str) -> StagedFile:
        """Hash and validate a stream privately without publishing durable bytes."""
        digest = hashlib.sha256()
        size = 0
        too_large = False
        descriptor, temporary_name = tempfile.mkstemp(
            suffix=".tmp", dir=self.staging_root
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as target:
                for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise TypeError("upload chunks must be bytes")
                    size += len(chunk)
                    digest.update(chunk)
                    if size > self.max_bytes:
                        too_large = True
                    elif not too_large:
                        target.write(chunk)
                if not too_large:
                    target.flush()
                    os.fsync(target.fileno())

            sha256 = digest.hexdigest()
            if too_large:
                raise StagedUploadRejected(
                    FileTooLarge(f"upload exceeds {self.max_bytes} bytes"),
                    sha256=sha256,
                    size=size,
                )
            with temporary.open("rb") as source:
                signature = source.read(4)
                if signature.startswith(b"MZ"):
                    raise UnsupportedFileType("executable uploads are not allowed")
                if signature.startswith(b"PK"):
                    inspect_zip(temporary)
            detected = detect_file_type(temporary)
            if detected.format == "UNKNOWN":
                raise UnsupportedFileType(
                    "upload signature is not an allowed tabular format"
                )
            return StagedFile(
                sha256=sha256,
                size=size,
                temporary_path=temporary,
                detected_format=detected.format,
                original_filename=filename,
            )
        except StagedUploadRejected:
            temporary.unlink(missing_ok=True)
            raise
        except ValueError as error:
            temporary.unlink(missing_ok=True)
            raise StagedUploadRejected(
                error,
                sha256=digest.hexdigest(),
                size=size,
            ) from error
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    def publish(self, staged: StagedFile) -> StoredFile:
        """Publish one validated staged upload by an exclusive immutable link."""
        destination = self.root / staged.sha256[:2] / staged.sha256[2:4] / staged.sha256
        destination.parent.mkdir(parents=True, exist_ok=True)
        created = True
        try:
            os.link(staged.temporary_path, destination)
        except FileExistsError:
            created = False
            if destination.stat().st_size != staged.size:
                raise RuntimeError("immutable source hash collision")
        return StoredFile(
            sha256=staged.sha256,
            size=staged.size,
            path=destination,
            detected_format=staged.detected_format,
            created=created,
            original_filename=staged.original_filename,
        )

    def discard(self, staged: StagedFile) -> None:
        staged.temporary_path.unlink(missing_ok=True)

    def store(self, chunks: Iterable[bytes], *, filename: str) -> StoredFile:
        """Hash a stream while writing, validate it, then publish by exclusive link."""
        try:
            staged = self.stage(chunks, filename=filename)
        except StagedUploadRejected as rejected:
            raise rejected.cause from rejected
        try:
            return self.publish(staged)
        finally:
            self.discard(staged)
