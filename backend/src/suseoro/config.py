"""Application configuration and local data-directory layout."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _default_data_dir() -> Path:
    """Return an operating-system data location, never a repository path."""
    base_dir = os.environ.get("PROGRAMDATA")
    if base_dir:
        return Path(base_dir) / "Suseoro"
    return Path.home() / ".local" / "share" / "suseoro"


@dataclass
class Settings:
    """Paths and non-secret runtime settings for a Suseoro installation."""

    data_dir: Path = field(default_factory=_default_data_dir)
    database_path: Path | None = None
    version: str = "0.1.0"
    secure_cookies: bool = True
    session_ttl_seconds: int = 8 * 60 * 60

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir).expanduser()
        if self.database_path is None:
            self.database_path = self.data_dir / "db" / "suseoro.sqlite3"
        else:
            self.database_path = Path(self.database_path).expanduser()

        for directory in (
            self.data_dir,
            self.database_path.parent,
            self.sources_dir,
            self.exports_dir,
            self.backups_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    @property
    def sources_dir(self) -> Path:
        return self.data_dir / "sources"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @classmethod
    def from_environment(cls) -> Settings:
        """Build settings from the supported SUSEORO_ environment variables."""
        data_dir = Path(os.environ.get("SUSEORO_DATA_DIR", _default_data_dir()))
        database_value = os.environ.get("SUSEORO_DATABASE_PATH")
        return cls(
            data_dir=data_dir,
            database_path=Path(database_value) if database_value else None,
            version=os.environ.get("SUSEORO_VERSION", "0.1.0"),
            secure_cookies=os.environ.get("SUSEORO_SECURE_COOKIES", "true").lower()
            not in {"0", "false", "no"},
            session_ttl_seconds=int(
                os.environ.get("SUSEORO_SESSION_TTL_SECONDS", str(8 * 60 * 60))
            ),
        )
