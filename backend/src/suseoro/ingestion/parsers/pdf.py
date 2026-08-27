"""Page-accounted PDF extraction with selective, injected OCR."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path
from typing import Any

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from suseoro.ingestion.contracts import (
    DocumentRole,
    ParsedField,
    ParsedRow,
    ParseResult,
    Provenance,
    RowStatus,
)
from suseoro.ingestion.file_store import StoredFile
from suseoro.ingestion.ocr import (
    OCR_INSTALLATION_GUIDANCE,
    OcrCancelled,
    OcrEngine,
    OcrError,
    OcrLanguageUnavailable,
    OcrTimeout,
    OcrUnavailable,
)
from suseoro.ingestion.parsers.tabular import _source_path_and_sha256

PARSER_VERSION = "pdf-v1"
DEFAULT_OCR_CONCURRENCY = 2
DEFAULT_TEXT_DENSITY_THRESHOLD = 0.00005


class _CombinedCancelEvent:
    def __init__(
        self, global_event: threading.Event, page_event: threading.Event
    ) -> None:
        self.global_event = global_event
        self.page_event = page_event

    def is_set(self) -> bool:
        return self.global_event.is_set() or self.page_event.is_set()


def _density(page: Any, text: str) -> float:
    width = abs(float(page.mediabox.right) - float(page.mediabox.left))
    height = abs(float(page.mediabox.top) - float(page.mediabox.bottom))
    return sum(not character.isspace() for character in text) / max(width * height, 1.0)


def _page_row(
    *,
    page_number: int,
    digest: str,
    extracted_text: str,
    density: float,
    text: str | None = None,
    ocr_used: bool = False,
    error_code: str | None = None,
    error_message: str | None = None,
) -> ParsedRow:
    raw_values = {
        "page": page_number,
        "extracted_text": extracted_text,
        "text_density": density,
        "ocr_used": ocr_used,
    }
    return ParsedRow(
        status=RowStatus.ROW_ERROR if error_code else RowStatus.SUCCESS,
        provenance=Provenance(
            sheet="page",
            source_row=page_number,
            source_columns={"text": 1} if text is not None else {},
            source_file_sha256=digest,
        ),
        raw_values=raw_values,
        fields={} if text is None else {"text": ParsedField(text, text)},
        error_code=error_code,
        error_message=error_message,
    )


def _file_error(*, digest: str, code: str, message: str) -> ParsedRow:
    return ParsedRow(
        status=RowStatus.ROW_ERROR,
        provenance=Provenance(sheet="page", source_row=0, source_file_sha256=digest),
        raw_values={"page": 0},
        fields={},
        error_code=code,
        error_message=message,
    )


def _ocr_error(
    error: BaseException,
) -> tuple[str, str]:
    if isinstance(error, OcrTimeout | FutureTimeout):
        return "OCR_TIMEOUT", str(error) or "OCR page timed out"
    if isinstance(error, OcrCancelled):
        return "OCR_CANCELLED", str(error)
    if isinstance(error, OcrLanguageUnavailable):
        return "OCR_LANGUAGE_UNAVAILABLE", str(error)
    if isinstance(error, OcrUnavailable):
        return "OCR_UNAVAILABLE", f"{error}. {OCR_INSTALLATION_GUIDANCE}"
    if isinstance(error, OcrError):
        return "OCR_ERROR", str(error)
    return "OCR_ERROR", f"OCR failed: {error}"


def parse_pdf(
    source: Path | StoredFile,
    *,
    role: DocumentRole,
    sha256: str | None = None,
    ocr_engine: OcrEngine | None = None,
    text_density_threshold: float = DEFAULT_TEXT_DENSITY_THRESHOLD,
    ocr_concurrency: int = DEFAULT_OCR_CONCURRENCY,
    ocr_timeout_seconds: float = 30,
    ocr_language: str = "kor+eng",
    cancel_event: threading.Event | None = None,
) -> ParseResult:
    started = time.perf_counter()
    path, digest = _source_path_and_sha256(source, sha256)
    if ocr_concurrency < 1:
        raise ValueError("ocr_concurrency must be positive")
    global_cancel = cancel_event or threading.Event()
    try:
        reader = PdfReader(path, strict=False)
        if reader.is_encrypted:
            raise PdfReadError("encrypted PDF is not supported")
        pages = list(reader.pages)
    except (OSError, PdfReadError, ValueError) as error:
        rows = [_file_error(digest=digest, code="PDF_ERROR", message=str(error))]
    else:
        rows_by_page: dict[int, ParsedRow] = {}
        deficient: list[tuple[int, Any, str, float]] = []
        for page_number, page in enumerate(pages, start=1):
            try:
                extracted = page.extract_text() or ""
                density = _density(page, extracted)
            except (KeyError, TypeError, ValueError) as error:
                rows_by_page[page_number] = _page_row(
                    page_number=page_number,
                    digest=digest,
                    extracted_text="",
                    density=0.0,
                    error_code="PDF_PAGE_ERROR",
                    error_message=str(error),
                )
                continue
            if density >= text_density_threshold:
                rows_by_page[page_number] = _page_row(
                    page_number=page_number,
                    digest=digest,
                    extracted_text=extracted,
                    density=density,
                    text=extracted,
                )
            else:
                deficient.append((page_number, page, extracted, density))

        if deficient and global_cancel.is_set():
            for page_number, _page, extracted, density in deficient:
                rows_by_page[page_number] = _page_row(
                    page_number=page_number,
                    digest=digest,
                    extracted_text=extracted,
                    density=density,
                    error_code="OCR_CANCELLED",
                    error_message=f"OCR cancelled before page {page_number}",
                )
        elif deficient and ocr_engine is None:
            for page_number, _page, extracted, density in deficient:
                rows_by_page[page_number] = _page_row(
                    page_number=page_number,
                    digest=digest,
                    extracted_text=extracted,
                    density=density,
                    error_code="OCR_UNAVAILABLE",
                    error_message=OCR_INSTALLATION_GUIDANCE,
                )
        elif deficient:
            executor = ThreadPoolExecutor(max_workers=ocr_concurrency)
            future_items = []
            try:
                for page_number, page, extracted, density in deficient:
                    page_cancel = threading.Event()
                    combined = _CombinedCancelEvent(global_cancel, page_cancel)
                    future = executor.submit(
                        ocr_engine.recognize,
                        page,
                        page_number=page_number,
                        language=ocr_language,
                        timeout_seconds=ocr_timeout_seconds,
                        cancel_event=combined,
                    )
                    future_items.append(
                        (future, page_cancel, page_number, extracted, density)
                    )
                for (
                    future,
                    page_cancel,
                    page_number,
                    extracted,
                    density,
                ) in future_items:
                    try:
                        text = future.result(timeout=ocr_timeout_seconds)
                    except Exception as error:  # noqa: BLE001 - engine boundary becomes a page error
                        page_cancel.set()
                        code, message = _ocr_error(error)
                        rows_by_page[page_number] = _page_row(
                            page_number=page_number,
                            digest=digest,
                            extracted_text=extracted,
                            density=density,
                            ocr_used=True,
                            error_code=code,
                            error_message=message,
                        )
                    else:
                        rows_by_page[page_number] = _page_row(
                            page_number=page_number,
                            digest=digest,
                            extracted_text=extracted,
                            density=density,
                            text=text,
                            ocr_used=True,
                        )
            finally:
                executor.shutdown(wait=False, cancel_futures=True)
        rows = [rows_by_page[number] for number in sorted(rows_by_page)]
    return ParseResult(
        role=role,
        detected_format="PDF",
        parser_version=PARSER_VERSION,
        parser_backend="pypdf-selective-ocr",
        rows=rows,
        elapsed_seconds=max(time.perf_counter() - started, 0.000001),
    )
