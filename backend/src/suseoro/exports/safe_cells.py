"""Spreadsheet injection defense and exclusive immutable byte storage."""

from __future__ import annotations

import hashlib
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_FORMULA_PREFIXES = ("=", "+", "-", "@")


def escape_spreadsheet_cell(value: Any) -> Any:
    if (
        isinstance(value, str)
        and value
        and (
            value.startswith(_FORMULA_PREFIXES)
            or ord(value[0]) < 32
            or ord(value[0]) == 127
        )
    ):
        return "'" + value
    return value


@dataclass(frozen=True)
class StoredArtifact:
    path: Path
    sha256: str
    size_bytes: int
    content: bytes


def store_immutable_bytes(
    root: Path,
    *,
    category: str,
    content: bytes,
    suffix: str,
) -> StoredArtifact:
    directory = root / category
    directory.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(content).hexdigest()
    path = directory / f"{uuid.uuid4()}-{digest[:12]}{suffix}"
    with path.open("xb") as destination:
        destination.write(content)
        destination.flush()
        os.fsync(destination.fileno())
    return StoredArtifact(
        path=path, sha256=digest, size_bytes=len(content), content=content
    )
