"""
document_parser.py
Reads an uploaded invoice or bank statement (PDF or image) and extracts
text via OCR or Vision-Language Model (VLM), then attempts to pull out
structured financial fields.

Design principle: this NEVER guesses or fabricates a value. Every field
comes back either with a value taken directly from the document text, or
as None. None means "leave it blank on the form, let the person fill it
in" -- it must never be replaced with a plausible-looking default.
"""
import io
import os
import re
from datetime import datetime

import pytesseract
import requests
from PIL import Image, ImageOps, ImageFilter

_TESSERACT_CMD = os.getenv("TESSERACT_CMD")
if _TESSERACT_CMD:
    pytesseract.pytesseract.tesseract_cmd = _TESSERACT_CMD

POPPLER_PATH = os.getenv("POPPLER_PATH")

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
    image = image.convert("L")
    w, h = image.size
    if max(w, h) < 2200:
        scale = 2200 / max(w, h)
        image = image.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    image = ImageOps.autocontrast(image, cutoff=1)
    image = image.filter(ImageFilter.SHARPEN)
    return image


def extract_text(filepath: str) -> str:
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

        if convert_from_path is None:
            return ""
        pages = convert_from_path(filepath, dpi=300, poppler_path=POPPLER_PATH)
        return "\n".join(pytesseract.image_to_string(_preprocess_for_ocr(page)) for page in pages)

    else:
        image = Image.open(filepath)
        return pytesseract.image_to_string(_preprocess_for_ocr(image))


# ── VLM (fine-tuned Qwen2.5-VL) integration ──────────────────────────────

# The model was trained on two kinds of document: bills RECEIVED from suppliers
# (money going out -> expenses) and receipts/invoices the business ISSUED
# (money coming in -> income). It has never seen a bank statement, so those
# still go to the OCR parser.
VLM_DOC_TYPES = {"bill_received", "receipt_issued"}

# If the model was less sure than this about ANY token it wrote, the amount is
# shown in amber with a "please verify" note. Tune after testing on real bills.
VLM_MIN_TOKEN_PROB = 0.5

# (connect, read) seconds. The first request after Colab wakes up can be slow.
VLM_TIMEOUT = (5, 120)


def _vlm_config():
    url = (os.getenv("FINMAN_VLM_URL") or "").strip().rstrip("/")
    key = (os.getenv("FINMAN_VLM_KEY") or "").strip()
    return url, key


def _image_for_vlm(filepath: str):
    """Returns (bytes, filename, mime) for the first page, or None if it can't be prepared."""
    ext = filepath.rsplit(".", 1)[1].lower() if "." in filepath else ""
    if ext == "pdf":
        if convert_from_path is None:
            return None
        pages = convert_from_path(filepath, dpi=150, first_page=1, last_page=1,
                                  poppler_path=POPPLER_PATH)
        if not pages:
            return None
        buf = io.BytesIO()
        pages[0].convert("RGB").save(buf, format="JPEG", quality=90)
        return buf.getvalue(), "document.jpg", "image/jpeg"
    with open(filepath, "rb") as f:
        data = f.read()
    return data, os.path.basename(filepath), ("image/png" if ext == "png" else "image/jpeg")


def _amount_in_document(filepath: str, amount: float):
    """Cross-check: does the number the model returned actually appear in the
    document's own text (via Tesseract)? True / False, or None if OCR couldn't
    be run or read nothing. A vision model can write a plausible number that
    is not on the page; this catches that."""
    try:
        text = extract_text(filepath)
    except Exception:
        return None
    if not text or not text.strip():
        return None
    flat = text.replace(" ", "")
    return f"{amount:,.2f}" in flat or f"{amount:.2f}" in flat


def parse_with_vlm(filepath: str, doc_type_hint: str = "auto"):
    """
    Sends the document to the fine-tuned Qwen2.5-VL API (Google Colab + ngrok).
    Returns (result_dict, None) on success, or (None, reason) when the model
    was not used, so the caller can fall back to OCR and say why.
    """
    if doc_type_hint not in VLM_DOC_TYPES:
        return None, "the AI reader is not used for bank statements (it was only trained on bills and receipts)"

    url, key = _vlm_config()
    if not url:
        return None, "FINMAN_VLM_URL is not set in .env"
    if not key:
        return None, "FINMAN_VLM_KEY is empty in .env"

    try:
        prepared = _image_for_vlm(filepath)
        if prepared is None:
            return None, "could not prepare the file for the AI reader (PDF support missing?)"
        img_bytes, filename, mime = prepared
        r = requests.post(
            f"{url}/parse",
            headers={"X-API-Key": key, "ngrok-skip-browser-warning": "true"},
            files={"file": (filename, img_bytes, mime)},
            data={"doc_type": doc_type_hint},
            timeout=VLM_TIMEOUT,
        )
    except requests.exceptions.ConnectionError:
        return None, "cannot reach the AI server (is the Colab notebook running? did the URL change?)"
    except requests.exceptions.Timeout:
        return None, "the AI server took too long to answer"
    except Exception as e:
        return None, f"AI request failed: {e}"

    if r.status_code == 401:
        return None, "the AI server rejected the key (FINMAN_VLM_KEY must equal the FINMAN_API_KEY secret in Colab)"
    if r.status_code == 404 and "ngrok" in r.text.lower():
        return None, "the ngrok tunnel is offline (re-run the last cell of the Phase 3 notebook)"
    if r.status_code != 200:
        return None, f"the AI server returned HTTP {r.status_code}"
    try:
        vlm = r.json()
    except ValueError:
        return None, "the AI server did not return JSON (ngrok warning page? wrong URL?)"

    fields = ("invoice_no", "vendor_name", "period", "total", "income", "expenses")
    if not any(vlm.get(f) is not None for f in fields):
        return None, "the AI reader returned no usable fields"

    income, expenses = vlm.get("income"), vlm.get("expenses")
    # Safety net on top of the model: a bill I received can only be an expense,
    # a receipt I issued can only be income. Never let the wrong side through.
    if doc_type_hint == "bill_received":
        income = None
    else:
        expenses = None
    amount = expenses if doc_type_hint == "bill_received" else income
    notes = list(vlm.get("warnings") or [])
    conf = vlm.get("confidence") or {}
    low_reason = None

    min_p = conf.get("min_token_probability")
    if isinstance(min_p, (int, float)) and min_p < VLM_MIN_TOKEN_PROB:
        low_reason = "The AI reader was unsure about part of this document. Please verify the amount."
    if amount is not None and _amount_in_document(filepath, amount) is False:
        low_reason = ("This amount could not be found in the document's text. "
                      "Please check it against your bill.")

    return {
        "doc_type": doc_type_hint,
        "engine": "vlm",
        "period": vlm.get("period"),
        "invoice_no": vlm.get("invoice_no"),
        "client_name": None,
        "vendor_name": vlm.get("vendor_name"),
        "income": income,
        "expenses": expenses,
        "invoices": None,
        "amount_low_confidence": low_reason is not None,
        "amount_low_confidence_reason": low_reason,
        "vlm_confidence": conf,
        "notes": notes,
        "raw_text_preview": f"[Read by the fine-tuned Qwen2.5-VL model]\n{vlm}",
    }, None


# ── Field extraction helpers (Tesseract Fallback) ─────────────────────────

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

VALID_DOC_TYPES = {"invoice_issued", "receipt_issued", "bill_received", "statement", "auto"}


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
    amounts = []
    for m in re.finditer(r"(?:RM|MYR|\$)\s*([\d]{1,3}(?:,\d{3})*\.\d{2})", text):
        try:
            amounts.append(float(m.group(1).replace(",", "")))
        except ValueError:
            continue
    return max(amounts) if amounts else None


def _find_period(text: str) -> str | None:
    if dateutil_parser is None:
        return None
    for match in _DATE_RE.finditer(text):
        try:
            dt = dateutil_parser.parse(match.group(1), dayfirst=True, fuzzy=True)
            if 2000 <= dt.year <= datetime.today().year + 1:
                return dt.strftime("%Y-%m")
        except (ValueError, OverflowError):
            continue
    return None


def _guess_doc_type(text: str) -> str:
    lower = text.lower()
    statement_hits = sum(1 for kw in _STATEMENT_KEYWORDS if kw in lower)
    invoice_hits   = sum(1 for kw in _INVOICE_KEYWORDS if kw in lower)
    if statement_hits > invoice_hits:
        return "statement"
    if invoice_hits > 0:
        return "invoice_issued"
    return "unknown"


def parse_financial_data(text: str, doc_type_hint: str = "auto") -> dict:
    if doc_type_hint not in VALID_DOC_TYPES:
        doc_type_hint = "auto"

    doc_type = _guess_doc_type(text) if doc_type_hint == "auto" else doc_type_hint

    result = {
        "doc_type":       doc_type,
        "engine":         "tesseract",
        "period":         _find_period(text),
        "invoice_no":     None,
        "client_name":    None,
        "vendor_name":    None,
        "income":         None,
        "expenses":       None,
        "invoices":       None,
        "amount_low_confidence": False,
        "amount_low_confidence_reason": None,
        "notes": [],
        "raw_text_preview": text[:2000],
    }

    invoice_matches = _INVOICE_NO_RE.finditer(text)
    for m in invoice_matches:
        candidate = m.group(1).strip()
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
        vendor_match = _VENDOR_RE.search(text)
        if vendor_match:
            result["vendor_name"] = vendor_match.group(1).strip().split("\n")[0].strip()
        result["expenses"] = total

    elif doc_type == "receipt_issued":
        result["income"] = total

    else:
        client_match = _CLIENT_RE.search(text)
        if client_match:
            candidate = client_match.group(1).strip().split("\n")[0].strip()
            if candidate:
                result["client_name"] = candidate
        result["invoices"] = total

    result["amount_low_confidence"] = total_is_fallback
    if total_is_fallback:
        result["amount_low_confidence_reason"] = (
            "Couldn't find a labeled total \u2014 this is our best guess "
            "(largest amount on the page). Please verify.")

    return result


def parse_document(filepath: str, doc_type_hint: str = "auto") -> dict:
    """
    Primary parsing entry point.
    Bills received and receipts issued go to the fine-tuned VLM first. Everything
    else (bank statements, or any case where the VLM is unreachable) uses Tesseract OCR
    plus pattern matching. The result says which engine produced it, and when
    the VLM was skipped, why.
    """
    vlm_result, vlm_reason = parse_with_vlm(filepath, doc_type_hint)
    if vlm_result is not None:
        print("[VLM] document read by the fine-tuned model")
        return vlm_result

    print(f"[VLM] not used: {vlm_reason}")
    text = extract_text(filepath)
    parsed = parse_financial_data(text, doc_type_hint)
    parsed["vlm_fallback_reason"] = vlm_reason
    return parsed