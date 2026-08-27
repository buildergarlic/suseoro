from __future__ import annotations

import hashlib
import importlib
import importlib.util
import threading
import time
from pathlib import Path

import pytest

from suseoro.ingestion.contracts import DocumentRole, RowStatus
from suseoro.ingestion.detection import detect_file_type


def _module(name: str):
    assert importlib.util.find_spec(name) is not None, f"missing parser module: {name}"
    return importlib.import_module(name)


def _pdf_bytes(page_texts: list[str]) -> bytes:
    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    kids: list[str] = []
    for index, text in enumerate(page_texts):
        page_id = 4 + index * 2
        content_id = page_id + 1
        kids.append(f"{page_id} 0 R")
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("ascii")
        objects[page_id] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>"
        ).encode("ascii")
        objects[content_id] = (
            f"<< /Length {len(stream)} >>\nstream\n".encode("ascii")
            + stream
            + b"\nendstream"
        )
    objects[2] = (
        f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    ).encode("ascii")

    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for object_id in range(1, max(objects) + 1):
        offsets.append(len(output))
        output.extend(f"{object_id} 0 obj\n".encode("ascii"))
        output.extend(objects[object_id])
        output.extend(b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(offsets)}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode(
            "ascii"
        )
    )
    return bytes(output)


def _write_pdf(path: Path, page_texts: list[str]) -> str:
    contents = _pdf_bytes(page_texts)
    path.write_bytes(contents)
    return hashlib.sha256(contents).hexdigest()


class CoordinatedOcrEngine:
    def __init__(self) -> None:
        self.calls: list[int] = []
        self.active = 0
        self.maximum_active = 0
        self.lock = threading.Lock()

    def recognize(
        self,
        page: object,
        *,
        page_number: int,
        language: str,
        timeout_seconds: float,
        cancel_event: threading.Event,
    ) -> str:
        with self.lock:
            self.calls.append(page_number)
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
        time.sleep(0.04)
        with self.lock:
            self.active -= 1
        return f"OCR page {page_number}"


def test_pdf_computes_page_density_and_only_queues_deficient_pages_with_default_two_workers(
    tmp_path: Path,
) -> None:
    """OCRing text pages, using a non-two default, or losing page accounting must fail."""
    pdf = _module("suseoro.ingestion.parsers.pdf")
    target = tmp_path / "pages.bin"
    digest = _write_pdf(
        target,
        [
            "This page already has enough searchable text for direct extraction.",
            "",
            "Another page already has enough searchable text for direct extraction.",
            "",
            "",
        ],
    )
    engine = CoordinatedOcrEngine()

    result = pdf.parse_pdf(
        target,
        role=DocumentRole.UNKNOWN,
        sha256=digest,
        ocr_engine=engine,
    )

    assert engine.calls == [2, 4, 5]
    assert engine.maximum_active == 2
    assert len(result.rows) == 5
    assert all(row.status == RowStatus.SUCCESS for row in result.rows)
    assert [row.raw_values["ocr_used"] for row in result.rows] == [
        False,
        True,
        False,
        True,
        True,
    ]
    assert (
        result.rows[0].raw_values["text_density"]
        > result.rows[1].raw_values["text_density"]
    )
    assert [row.provenance.source_row for row in result.rows] == [1, 2, 3, 4, 5]
    assert all(row.provenance.source_file_sha256 == digest for row in result.rows)


def test_missing_ocr_keeps_text_pages_and_errors_only_deficient_pages_with_guidance(
    tmp_path: Path,
) -> None:
    """Failing the whole PDF or silently dropping an image page when OCR is absent must fail."""
    pdf = _module("suseoro.ingestion.parsers.pdf")
    target = tmp_path / "mixed.pdf"
    digest = _write_pdf(target, ["Enough embedded text to retain without OCR.", ""])

    result = pdf.parse_pdf(
        target, role=DocumentRole.UNKNOWN, sha256=digest, ocr_engine=None
    )

    assert [row.status for row in result.rows] == [
        RowStatus.SUCCESS,
        RowStatus.ROW_ERROR,
    ]
    assert result.rows[1].error_code == "OCR_UNAVAILABLE"
    assert "Tesseract" in (result.rows[1].error_message or "")
    assert result.rows[0].fields["text"].value.startswith("Enough embedded")


def test_pdf_timeout_and_preexisting_cancellation_are_explicit_page_errors(
    tmp_path: Path,
) -> None:
    """Hanging indefinitely or returning an unaccounted cancelled page must fail."""
    pdf = _module("suseoro.ingestion.parsers.pdf")
    ocr = _module("suseoro.ingestion.ocr")
    target = tmp_path / "blank.pdf"
    digest = _write_pdf(target, [""])

    class TimeoutEngine:
        def recognize(self, *args: object, **kwargs: object) -> str:
            raise ocr.OcrTimeout("deadline exceeded")

    timed_out = pdf.parse_pdf(
        target,
        role=DocumentRole.UNKNOWN,
        sha256=digest,
        ocr_engine=TimeoutEngine(),
        ocr_timeout_seconds=0.01,
    )
    cancelled_event = threading.Event()
    cancelled_event.set()
    cancelled = pdf.parse_pdf(
        target,
        role=DocumentRole.UNKNOWN,
        sha256=digest,
        ocr_engine=CoordinatedOcrEngine(),
        cancel_event=cancelled_event,
    )

    assert timed_out.rows[0].error_code == "OCR_TIMEOUT"
    assert cancelled.rows[0].error_code == "OCR_CANCELLED"


def test_tesseract_port_discovers_binary_checks_languages_and_honors_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Skipping discovery/language checks or launching after cancellation must fail."""
    ocr = _module("suseoro.ingestion.ocr")
    executable = tmp_path / "tesseract.exe"
    executable.write_bytes(b"")
    monkeypatch.setattr(ocr.shutil, "which", lambda _: str(executable))

    found = ocr.discover_tesseract()
    engine = ocr.TesseractOcrEngine(
        found,
        renderer=lambda _: b"PNG",
        available_languages={"eng"},
    )

    assert found == executable
    with pytest.raises(ocr.OcrLanguageUnavailable, match="kor"):
        engine.recognize(
            object(),
            page_number=1,
            language="kor+eng",
            timeout_seconds=1,
            cancel_event=threading.Event(),
        )
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(ocr.OcrCancelled):
        engine.recognize(
            object(),
            page_number=1,
            language="eng",
            timeout_seconds=1,
            cancel_event=cancelled,
        )


def test_pdf_detection_is_signature_based(tmp_path: Path) -> None:
    """Requiring a .pdf suffix or accepting extension-only files must fail."""
    target = tmp_path / "renamed.data"
    _write_pdf(target, ["PDF signature"])

    assert detect_file_type(target).format == "PDF"
