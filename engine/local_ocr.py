"""Local Tesseract through PyMuPDF, isolated in a process with a hard OCR deadline."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time

import pymupdf

from engine.config import Settings
from engine.logging_setup import log_event
from engine.parsing import OcrResult

logger = logging.getLogger("engine.local_ocr")


def tesseract_ocr(data: bytes, doc_id: str, settings: Settings) -> OcrResult | None:
    started = time.monotonic()
    try:
        tessdata = settings.tesseract_data_dir.resolve()
        if not all(
            (tessdata / f"{language}.traineddata").is_file() for language in settings.tesseract_language.split("+")
        ):
            raise FileNotFoundError("OCR language weights missing; run scripts/local_ai.py setup")
        process = subprocess.run(
            [
                sys.executable,
                "-m",
                "engine.local_ocr",
                str(tessdata),
                settings.tesseract_language,
                str(settings.tesseract_dpi),
                str(settings.ocr_max_pages),
                str(settings.ocr_max_page_pixels),
            ],
            input=data,
            capture_output=True,
            timeout=settings.ocr_timeout_s,
            check=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        result = json.loads(process.stdout)
        if not isinstance(result.get("text"), str) or not 0 < result.get("pages", 0) <= settings.ocr_max_pages:
            raise ValueError("Invalid OCR output")
        log_event(
            logger,
            "ocr_completed",
            doc_id=doc_id,
            provider="tesseract",
            pages=result["pages"],
            latency_ms=round((time.monotonic() - started) * 1000, 1),
        )
        return OcrResult(text=result["text"], pages=result["pages"])
    except (OSError, ValueError, TypeError, RuntimeError, subprocess.SubprocessError) as exc:
        # Errors can include resume text. The child process's stderr is deliberately not logged.
        log_event(
            logger,
            "ocr_failed",
            level=logging.WARNING,
            doc_id=doc_id,
            provider="tesseract",
            error_type=type(exc).__name__,
        )
        return None


def _read_pdf(
    data: bytes, *, tessdata: str, language: str, dpi: int, maximum_pages: int, maximum_pixels: int
) -> OcrResult:
    with pymupdf.open(stream=data, filetype="pdf") as document:
        if not 0 < len(document) <= maximum_pages:
            raise ValueError("OCR page limit exceeded")
        texts = []
        for page in document:
            if page.rect.width * page.rect.height * (dpi / 72) ** 2 > maximum_pixels:
                raise ValueError("OCR pixel limit exceeded")
            textpage = page.get_textpage_ocr(language=language, dpi=dpi, full=True, tessdata=tessdata)
            texts.append(page.get_text(textpage=textpage))
        return OcrResult(text="\n".join(texts), pages=len(document))


if __name__ == "__main__":
    # Document bytes stay in the pipe, never in a command argument or a temporary resume file.
    output = _read_pdf(
        sys.stdin.buffer.read(),
        tessdata=sys.argv[1],
        language=sys.argv[2],
        dpi=int(sys.argv[3]),
        maximum_pages=int(sys.argv[4]),
        maximum_pixels=int(sys.argv[5]),
    )
    sys.stdout.buffer.write(json.dumps({"text": output.text, "pages": output.pages}).encode("utf-8"))
