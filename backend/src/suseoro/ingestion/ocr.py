"""Injectable OCR port and bounded Tesseract implementation."""

from __future__ import annotations

import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

OCR_INSTALLATION_GUIDANCE = (
    "Install Tesseract OCR with the Korean and English language packs, "
    "then configure the executable path."
)


class OcrError(RuntimeError):
    pass


class OcrUnavailable(OcrError):
    pass


class OcrLanguageUnavailable(OcrError):
    pass


class OcrTimeout(OcrError):
    pass


class OcrCancelled(OcrError):
    pass


class CancellationToken(Protocol):
    def is_set(self) -> bool: ...


class OcrEngine(Protocol):
    def recognize(
        self,
        page: object,
        *,
        page_number: int,
        language: str,
        timeout_seconds: float,
        cancel_event: CancellationToken,
    ) -> str: ...


def discover_tesseract(executable_name: str = "tesseract") -> Path | None:
    """Return an installed executable without invoking a shell."""
    found = shutil.which(executable_name)
    return Path(found) if found else None


def _installed_languages(executable: Path, *, timeout_seconds: float = 5) -> set[str]:
    try:
        completed = subprocess.run(
            [str(executable), "--list-langs"],
            check=True,
            capture_output=True,
            shell=False,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise OcrUnavailable(f"Could not query Tesseract languages: {error}") from error
    lines = completed.stdout.decode("utf-8", errors="replace").splitlines()
    return {line.strip() for line in lines[1:] if line.strip()}


class TesseractOcrEngine:
    """Render one injected PDF page and pipe it to Tesseract via stdin/stdout."""

    def __init__(
        self,
        executable: Path | None,
        *,
        renderer: Callable[[object], bytes],
        available_languages: set[str] | None = None,
    ) -> None:
        if executable is None:
            raise OcrUnavailable(OCR_INSTALLATION_GUIDANCE)
        self.executable = Path(executable)
        self.renderer = renderer
        self.available_languages = (
            frozenset(available_languages)
            if available_languages is not None
            else frozenset(_installed_languages(self.executable))
        )

    def recognize(
        self,
        page: object,
        *,
        page_number: int,
        language: str,
        timeout_seconds: float,
        cancel_event: CancellationToken,
    ) -> str:
        if cancel_event.is_set():
            raise OcrCancelled(f"OCR cancelled before page {page_number}")
        requested = {item for item in language.split("+") if item}
        missing = requested - self.available_languages
        if missing:
            raise OcrLanguageUnavailable(
                "Tesseract language data is missing: " + ", ".join(sorted(missing))
            )
        image = self.renderer(page)
        try:
            process = subprocess.Popen(
                [str(self.executable), "stdin", "stdout", "-l", language],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
            )
        except OSError as error:
            raise OcrUnavailable(f"Could not start Tesseract: {error}") from error

        deadline = time.monotonic() + timeout_seconds
        input_bytes: bytes | None = image
        while True:
            if cancel_event.is_set():
                process.kill()
                process.communicate()
                raise OcrCancelled(f"OCR cancelled on page {page_number}")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                process.kill()
                process.communicate()
                raise OcrTimeout(f"Tesseract timed out on page {page_number}")
            try:
                stdout, stderr = process.communicate(
                    input=input_bytes, timeout=min(remaining, 0.1)
                )
                break
            except subprocess.TimeoutExpired:
                input_bytes = None
        if process.returncode:
            message = stderr.decode("utf-8", errors="replace").strip()
            raise OcrError(message or f"Tesseract failed on page {page_number}")
        return stdout.decode("utf-8", errors="replace").strip()
