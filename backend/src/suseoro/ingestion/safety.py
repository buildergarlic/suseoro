"""Archive and XML safety gates used before workbook parsing."""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from xml.etree.ElementTree import Element

from defusedxml import ElementTree as safe_element_tree
from defusedxml.common import DefusedXmlException


class ArchiveSafetyError(ValueError):
    pass


class XmlSafetyError(ValueError):
    pass


@dataclass(frozen=True)
class ArchiveInspection:
    entries: int
    expanded_bytes: int
    maximum_compression_ratio: float


def _validate_member_path(name: str) -> None:
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        not name
        or path.is_absolute()
        or ".." in path.parts
        or normalized.startswith("/")
        or re.match(r"^[A-Za-z]:", normalized)
    ):
        raise ArchiveSafetyError(f"unsafe archive path: {name!r}")


def inspect_zip(
    path: Path,
    *,
    max_entries: int = 10_000,
    max_expanded_bytes: int = 512 * 1024 * 1024,
    max_compression_ratio: float = 200.0,
) -> ArchiveInspection:
    """Validate ZIP metadata without extracting any member."""
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if len(members) > max_entries:
                raise ArchiveSafetyError("archive has too many entries")
            expanded = 0
            maximum_ratio = 0.0
            for member in members:
                _validate_member_path(member.filename)
                if member.flag_bits & 0x1:
                    raise ArchiveSafetyError(
                        "encrypted archive entries are not allowed"
                    )
                expanded += member.file_size
                if expanded > max_expanded_bytes:
                    raise ArchiveSafetyError("archive expanded size exceeds limit")
                if member.file_size:
                    ratio = member.file_size / max(member.compress_size, 1)
                    maximum_ratio = max(maximum_ratio, ratio)
                    if ratio > max_compression_ratio:
                        raise ArchiveSafetyError(
                            "archive compression ratio exceeds limit"
                        )
            return ArchiveInspection(len(members), expanded, maximum_ratio)
    except ArchiveSafetyError:
        raise
    except (OSError, zipfile.BadZipFile, ValueError) as error:
        raise ArchiveSafetyError("invalid ZIP archive") from error


def safe_xml_from_bytes(contents: bytes) -> Element:
    """Parse XML with DTD/entity/network expansion disabled."""
    try:
        return safe_element_tree.fromstring(
            contents,
            forbid_dtd=True,
            forbid_entities=True,
            forbid_external=True,
        )
    except (DefusedXmlException, safe_element_tree.ParseError, ValueError) as error:
        raise XmlSafetyError("unsafe or invalid XML") from error
