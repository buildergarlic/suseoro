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

    def store(self, chunks: Iterable[bytes], *, filename: str) -> StoredFile:
        """Hash a stream while writing, validate it, then publish by exclusive link."""
        digest = hashlib.sha256()
        size = 0
        descriptor, temporary_name = tempfile.mkstemp(suffix=".tmp", dir=self.root)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as target:
                for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise TypeError("upload chunks must be bytes")
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise FileTooLarge(f"upload exceeds {self.max_bytes} bytes")
                    digest.update(chunk)
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())

            sha256 = digest.hexdigest()
            with temporary.open("rb") as source:
                if source.read(4).startswith(b"PK"):
                    inspect_zip(temporary)
            detected = detect_file_type(temporary)
            if detected.format == "UNKNOWN":
                raise UnsupportedFileType(
                    "upload signature is not an allowed tabular format"
                )
            destination = self.root / sha256[:2] / sha256[2:4] / sha256
            destination.parent.mkdir(parents=True, exist_ok=True)
            created = True
            try:
                os.link(temporary, destination)
            except FileExistsError:
                created = False
                if destination.stat().st_size != size:
                    raise RuntimeError("immutable source hash collision")
            return StoredFile(
                sha256=sha256,
                size=size,
                path=destination,
                detected_format=detected.format,
                created=created,
                original_filename=filename,
            )
        finally:
            temporary.unlink(missing_ok=True)
