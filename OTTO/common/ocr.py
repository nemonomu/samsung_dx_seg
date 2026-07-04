"""OCR fallback for image-only EU energy datasheets (scanned PDFs with no text layer).

Some datasheets (e.g. Camry mini washers) embed the Produktdatenblatt as a single scanned
image, so pdfplumber extracts nothing — but the values (Nennkapazität, Modellkennung, ...)
are legible. Render with PyMuPDF and OCR with easyocr. Lazy + cached; degrades to "" if the
optional deps are missing so the pipeline never hard-fails on OCR.
"""
from __future__ import annotations

_READER = None
_UNAVAILABLE = False
_OCR_DEPS = ("easyocr", "PyMuPDF")


def _pip_install(packages) -> bool:
    import subprocess
    import sys
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", *packages])
        return True
    except Exception:
        return False


def _reader():
    global _READER, _UNAVAILABLE
    if _READER is not None or _UNAVAILABLE:
        return _READER
    for attempt in range(2):
        try:
            import easyocr  # noqa: F401 (PyMuPDF checked lazily in pdf_text)
            import fitz  # noqa: F401
            import easyocr as _e
            _READER = _e.Reader(["de", "en"], gpu=False, verbose=False)
            return _READER
        except Exception:
            # auto-install the optional OCR deps once (RDP may not have run requirements)
            if attempt == 0 and _pip_install(_OCR_DEPS):
                print("[ocr] installed OCR deps (easyocr, PyMuPDF)", flush=True)
                continue
            _UNAVAILABLE = True
    return _READER


def available() -> bool:
    return _reader() is not None


def pdf_text(pdf_bytes: bytes, *, dpi: int = 200, max_pages: int = 2) -> str:
    """OCR the first pages of a PDF and return the recognized text (space-joined)."""
    reader = _reader()
    if not reader or not pdf_bytes:
        return ""
    try:
        import fitz  # PyMuPDF
    except Exception:
        return ""
    out: list[str] = []
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        for page in list(doc)[:max_pages]:
            png = page.get_pixmap(dpi=dpi).tobytes("png")
            out.extend(reader.readtext(png, detail=0))
    except Exception:
        return " ".join(out)
    return " ".join(out)
