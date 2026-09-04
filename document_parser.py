"""
document_parser.py
Reads an uploaded invoice or bank statement (PDF or image) and extracts
text via OCR, then attempts to pull out structured financial fields.

Design principle: this NEVER guesses or fabricates a value. Every field
comes back either with a value taken directly from the document text, or
as None. None means "leave it blank on the form, let the person fill it
in" -- it must never be replaced with a plausible-looking default.
"""
import os
import re
from datetime import datetime

import pytesseract
from PIL import Image, ImageOps, ImageFilter

# On Windows (and some other setups), Tesseract and Poppler aren't on PATH
# by default. Rather than requiring a system-wide PATH edit, read explicit
# paths from the environment if provided. Leave unset on Linux/Docker where
# `apt-get install tesseract-ocr poppler-utils` already puts both on PATH.
_TESSERACT_CMD = os.getenv("TESSERACT_CMD")
if _TESSERACT_CMD:
    pytesseract.pytesseract.tesseract_cmd = _TESSERACT_CMD

POPPLER_PATH = os.getenv("POPPLER_PATH")  # e.g. C:\poppler\Library\bin on Windows

try:
    import pdfplumber
except ImportError:
    pdfplumber = None

try:
    from pdf2image import convert_from_path
except ImportError:
    convert_from_path = None

try:
    from dateutil import parser as dateutil_parser
except ImportError:
    dateutil_parser = None


ALLOWED_EXTENSIONS = {"pdf", "png", "jpg", "jpeg"}
MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def _preprocess_for_ocr(image: Image.Image) -> Image.Image:
    """
    Cleans up a photo/scan before handing it to Tesseract. Confirmed via
    direct testing to remove stray noise characters that otherwise leak
    into the extracted text (e.g. a smudge OCR'd as a stray quote mark).
    This does NOT fix everything -- severe blur or extreme low light can
    still defeat OCR entirely -- but it's a real, measured improvement on
    typical "bad phone photo" quality scans, for zero additional cost.
    """
    image = image.convert("L")  # grayscale -- color info doesn't help OCR
    w, h = image.size
    # Upscale small/low-res images -- Tesseract's accuracy drops sharply
    # below a certain character pixel-height.
    if max(w, h) < 2200:
        scale = 2200 / max(w, h)
        image = image.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    image = ImageOps.autocontrast(image, cutoff=1)
    image = image.filter(ImageFilter.SHARPEN)
    return image


def extract_text(filepath: str) -> str:
    """
    Extracts raw text from a PDF or image file.
    For PDFs: tries direct text extraction first (fast, exact -- works for
    "digital" PDFs that were generated rather than scanned). Falls back to
    rendering pages as images and OCR-ing them only if direct extraction
    comes back empty (i.e. it's a scanned document).
    For images: goes straight to OCR.
    """
    ext = filepath.rsplit(".", 1)[1].lower()

    if ext == "pdf":
        text = ""
        if pdfplumber is not None:
            try:
                with pdfplumber.open(filepath) as pdf:
                    text = "\n".join(page.extract_text() or "" for page in pdf.pages)
            except Exception:
                text = ""

        if text.strip():
            return text

        # Scanned/image-only PDF -- OCR each page.
        if convert_from_path is None:
            return ""
        pages = convert_from_path(filepath, dpi=300, poppler_path=POPPLER_PATH)
        return "\n".join(pytesseract.image_to_string(_preprocess_for_ocr(page)) for page in pages)

    else:
        image = Image.open(filepath)
        return pytesseract.image_to_string(_preprocess_for_ocr(image))


# ── Field extraction helpers ──────────────────────────────────────────────

_AMOUNT_RE = r"(?:RM|MYR|\$)?\s*([\d]{1,3}(?:,\d{3})*(?:\.\d{2})?)"

_TOTAL_LABELS = [
    r"\bgrand\s*total\b", r"\btotal\s*due\b", r"\bamount\s*due\b", r"\bbalance\s*due\b",
    r"\bamount\s*payable\b", r"\btotal\s*payable\b", r"\bnet\s*amount\b",
    r"\binvoice\s*total\b", r"\btotal\s*amount\b", r"\btotal\b",
]
_INVOICE_NO_RE = re.compile(
    r"invoice\s*(?:no\.?|number|#)\s*[:\-]?\s*([A-Za-z0-9\-\/]+)", re.IGNORECASE
)
_DATE_RE = re.compile(
    r"\b(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4}|\d{4}[\/\-\.]\d{1,2}[\/\-\.]\d{1,2}"
    r"|[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})\b"
)
_CLIENT_RE = re.compile(
    r"(?:bill\s*to|invoice\s*to|customer)\s*[:\-]?\s*\n?\s*([A-Za-z0-9 &.,'\-]{3,60})",
    re.IGNORECASE,
)

_VENDOR_RE = re.compile(
    r"(?:from|vendor|supplier|sold\s*by)\s*[:\-]?\s*\n?\s*([A-Za-z0-9 &.,'\-]{3,60})",
    re.IGNORECASE,
)

_STATEMENT_KEYWORDS = ["statement", "opening balance", "closing balance", "account summary"]
_INVOICE_KEYWORDS   = ["invoice", "bill to", "due date"]

_CREDIT_LABELS = [r"total\s*credit", r"total\s*deposit", r"deposits?"]
_DEBIT_LABELS  = [r"total\s*debit", r"total\s*withdrawal", r"withdrawals?"]

VALID_DOC_TYPES = {"invoice_issued", "bill_received", "statement", "auto"}


def _find_amount_after_label(text: str, labels: list) -> float | None:
    for label in labels:
        pattern = re.compile(label + r"[^\d\n]{0,20}" + _AMOUNT_RE, re.IGNORECASE)
        match = pattern.search(text)
        if match:
            raw = match.group(1).replace(",", "")
            try:
                return float(raw)
            except ValueError:
                continue
    return None


def _find_largest_amount(text: str) -> float | None:
    """
    Fallback when no labeled total was found: the grand total is almost
    always the largest currency-formatted number on the page. This is a
    weaker signal than a labeled match, so it's only used as a last resort.
    """
    amounts = []
    for m in re.finditer(r"(?:RM|MYR|\$)\s*([\d]{1,3}(?:,\d{3})*\.\d{2})", text):
        try:
            amounts.append(float(m.group(1).replace(",", "")))
        except ValueError:
            continue
    return max(amounts) if amounts else None


def _find_period(text: str) -> str | None:
    """Finds the first parseable date in the document and returns it as YYYY-MM."""
    if dateutil_parser is None:
        return None
    for match in _DATE_RE.finditer(text):
        try:
            dt = dateutil_parser.parse(match.group(1), dayfirst=True, fuzzy=True)
            # Sanity check: reject obviously-wrong OCR misreads (e.g. year 0001, 2999)
            if 2000 <= dt.year <= datetime.today().year + 1:
                return dt.strftime("%Y-%m")
        except (ValueError, OverflowError):
            continue
    return None


def _guess_doc_type(text: str) -> str:
    """Fallback only -- used when the person selects 'auto' rather than
    telling us directly what kind of document this is. Picking the type
    explicitly is far more reliable than this heuristic, since document
    wording varies a lot between banks/vendors."""
    lower = text.lower()
    statement_hits = sum(1 for kw in _STATEMENT_KEYWORDS if kw in lower)
    invoice_hits   = sum(1 for kw in _INVOICE_KEYWORDS if kw in lower)
    if statement_hits > invoice_hits:
        return "statement"
    if invoice_hits > 0:
        return "invoice_issued"
    return "unknown"


def parse_financial_data(text: str, doc_type_hint: str = "auto") -> dict:
    """
    Returns a dict of extracted fields, each field's value is either a
    real value found in the text, or None if it couldn't be confidently
    identified -- never a guess dressed up as a value.

    doc_type_hint should be one of:
      "invoice_issued" -- an invoice you sent to a client (they owe you)
      "bill_received"  -- a bill/invoice you received (you owe someone)
      "statement"      -- a bank/financial statement
      "auto"           -- let the (unreliable) keyword guesser decide

    Asking the person to pick this up front avoids two failure modes at
    once: mis-detecting the document type, and -- more importantly --
    guessing whether a total is money owed to the business or money the
    business owes, which isn't something text alone can answer.
    """
    if doc_type_hint not in VALID_DOC_TYPES:
        doc_type_hint = "auto"

    doc_type = _guess_doc_type(text) if doc_type_hint == "auto" else doc_type_hint

    result = {
        "doc_type":       doc_type,
        "period":         _find_period(text),
        "invoice_no":     None,
        "client_name":    None,
        "vendor_name":    None,
        "income":         None,
        "expenses":       None,
        "invoices":       None,
        "amount_low_confidence": False,
        "raw_text_preview": text[:2000],
    }

    invoice_matches = _INVOICE_NO_RE.finditer(text)
    for m in invoice_matches:
        candidate = m.group(1).strip()
        # Real invoice numbers contain a digit; skip false matches like a
        # standalone "INVOICE" header being mistaken for the label itself.
        if re.search(r"\d", candidate):
            result["invoice_no"] = candidate
            break

    total = _find_amount_after_label(text, _TOTAL_LABELS)
    total_is_fallback = False
    if total is None and doc_type != "statement":
        total = _find_largest_amount(text)
        total_is_fallback = total is not None

    if doc_type == "statement":
        result["income"]   = _find_amount_after_label(text, _CREDIT_LABELS)
        result["expenses"] = _find_amount_after_label(text, _DEBIT_LABELS)

    elif doc_type == "bill_received":
        # You owe this -- goes to Expenses. Try to find who it's from.
        vendor_match = _VENDOR_RE.search(text)
        if vendor_match:
            result["vendor_name"] = vendor_match.group(1).strip().split("\n")[0].strip()
        result["expenses"] = total

    else:
        # invoice_issued (or unknown/"auto" with no clearer signal):
        # this is money owed TO the business -- an outstanding invoice,
        # not automatically income until it's actually paid.
        client_match = _CLIENT_RE.search(text)
        if client_match:
            candidate = client_match.group(1).strip().split("\n")[0].strip()
            if candidate:
                result["client_name"] = candidate
        result["invoices"] = total

    result["amount_low_confidence"] = total_is_fallback

    return result
