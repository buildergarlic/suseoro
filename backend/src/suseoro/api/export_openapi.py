"""Generate the checked-in OpenAPI schema from the tested application."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from suseoro.api.app import create_app
from suseoro.config import Settings


def main() -> None:
    backend_root = Path(__file__).resolve().parents[3]
    with tempfile.TemporaryDirectory(prefix="suseoro-openapi-") as directory:
        app = create_app(Settings(data_dir=Path(directory), secure_cookies=False))
        schema = app.openapi()
    target = backend_root / "openapi.json"
    target.write_text(
        json.dumps(schema, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
