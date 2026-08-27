from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """A per-test application data directory outside the repository."""
    return tmp_path / "application-data"
