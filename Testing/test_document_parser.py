from unittest.mock import MagicMock, patch
import pytest
import requests
from PIL import Image
import document_parser as dp

INVOICE_TEXT = """INVOICE
Invoice No: INV-2026-045
Bill To: Acme Trading Sdn Bhd
Issue Date: 15/10/2026
Due Date: 30/10/2026
Grand Total: RM 1,250.50
"""


# ---------- File validation ----------
@pytest.mark.parametrize("name, ok", [
    ("a.pdf", True), ("b.PNG", True), ("c.jpeg", True), ("d.jpg", True),
    ("e.exe", False), ("noextension", False), ("f.pdf.exe", False), ("", False),
])
def test_allowed_file(name, ok):
    assert dp.allowed_file(name) is ok

def test_max_file_size_is_10mb():
    assert dp.MAX_FILE_SIZE_BYTES == 10 * 1024 * 1024


# ---------- Image preprocessing / text extraction ----------
def test_preprocess_converts_to_grayscale_and_upscales_small_images():
    out = dp._preprocess_for_ocr(Image.new("RGB", (100, 50), "white"))
    assert out.mode == "L" and max(out.size) >= 2200

def test_extract_text_from_image_uses_ocr(tmp_path):
    p = tmp_path / "scan.png"; Image.new("RGB", (50, 50), "white").save(p)
    with patch.object(dp.pytesseract, "image_to_string", return_value="hello"):
        assert dp.extract_text(str(p)) == "hello"

def test_extract_text_reads_text_pdf_without_ocr(tmp_path):
    from invoice_generator import generate_invoice_pdf
    out = tmp_path / "inv.pdf"
    generate_invoice_pdf({"invoice_no": "INV-9", "client_name": "Zed", "issue_date": "2026-10-01",
                          "due_date": "2026-10-31", "total": 10.0,
                          "line_items": [{"description": "x", "qty": 1, "unit_price": 10}]},
                         str(out), {"company": "Co", "email": "a@b.com"})
    with patch.object(dp.pytesseract, "image_to_string") as ocr:
        text = dp.extract_text(str(out))
    assert "INV-9" in text
    ocr.assert_not_called()


# ---------- Field extraction helpers ----------
def test_find_amount_after_label():
    assert dp._find_amount_after_label("Grand Total: RM 1,250.50", dp._TOTAL_LABELS) == 1250.50

def test_find_amount_returns_none_without_label():
    assert dp._find_amount_after_label("no totals here", dp._TOTAL_LABELS) is None

def test_find_largest_amount():
    assert dp._find_largest_amount("RM 12.00 and RM 1,030.50 and RM 5.00") == 1030.50
    assert dp._find_largest_amount("nothing") is None

def test_find_period_day_first_date():
    assert dp._find_period("Issued 15/10/2026") == "2026-10"

def test_find_period_none_when_no_date():
    assert dp._find_period("no dates") is None

def test_guess_doc_type():
    assert dp._guess_doc_type("Invoice No 1, Bill To X") == "invoice_issued"
    assert dp._guess_doc_type("Account Summary Opening Balance 100") == "statement"
    assert dp._guess_doc_type("random words") == "unknown"


# ---------- Structured field parsing (OCR fallback) ----------
def test_parse_invoice_extracts_fields():
    r = dp.parse_financial_data(INVOICE_TEXT, "auto")
    assert r["doc_type"] == "invoice_issued"
    assert r["invoice_no"] == "INV-2026-045"
    assert r["client_name"] == "Acme Trading Sdn Bhd"
    assert r["invoices"] == 1250.50
    assert r["period"] == "2026-10"
    assert r["amount_low_confidence"] is False

def test_parse_bill_received_maps_total_to_expenses():
    text = "Vendor: ABC Supplies\nTotal Amount Due: RM 800.00"
    r = dp.parse_financial_data(text, "bill_received")
    assert r["expenses"] == 800.0 and r["income"] is None
    assert r["vendor_name"] == "ABC Supplies"

def test_parse_receipt_issued_maps_total_to_income():
    r = dp.parse_financial_data("Receipt\nTotal: RM 99.90", "receipt_issued")
    assert r["income"] == 99.90 and r["expenses"] is None

def test_parse_statement_extracts_credits_and_debits():
    text = "Account Summary\nOpening Balance 1,000.00\nTotal Credits: RM 5,000.00\nTotal Debits: RM 3,200.00"
    r = dp.parse_financial_data(text, "auto")
    assert r["doc_type"] == "statement"
    assert r["income"] == 5000.0 and r["expenses"] == 3200.0

def test_unlabelled_total_uses_largest_amount_and_is_flagged():
    r = dp.parse_financial_data("Receipt\nCoffee RM 12.00\nCake RM 30.50", "receipt_issued")
    assert r["income"] == 30.50
    assert r["amount_low_confidence"] is True and "verify" in r["amount_low_confidence_reason"].lower()

def test_nothing_is_fabricated_from_empty_text():
    r = dp.parse_financial_data("", "auto")
    for field in ("period", "invoice_no", "client_name", "vendor_name", "income", "expenses", "invoices"):
        assert r[field] is None
    assert r["amount_low_confidence"] is False

def test_invalid_doc_type_hint_treated_as_auto():
    assert dp.parse_financial_data(INVOICE_TEXT, "nonsense")["doc_type"] == "invoice_issued"


# ---------- AI reader (VLM) ----------
@pytest.fixture
def vlm_env(monkeypatch):
    monkeypatch.setenv("FINMAN_VLM_URL", "https://vlm.example/")
    monkeypatch.setenv("FINMAN_VLM_KEY", "k")
    with patch.object(dp, "_image_for_vlm", return_value=(b"x", "a.jpg", "image/jpeg")):
        yield

def _resp(status=200, json_data=None, text=""):
    r = MagicMock(); r.status_code = status; r.text = text
    r.json.return_value = json_data
    return r

GOOD = {"invoice_no": "B-1", "vendor_name": "ABC", "period": "2026-09", "total": 100.0,
        "income": 100.0, "expenses": 100.0, "confidence": {"min_token_probability": 0.9}}

def test_vlm_not_used_for_bank_statements(vlm_env):
    r, reason = dp.parse_with_vlm("x.pdf", "statement")
    assert r is None and "bank statements" in reason

def test_vlm_requires_url_and_key(monkeypatch):
    monkeypatch.delenv("FINMAN_VLM_URL", raising=False)
    assert "FINMAN_VLM_URL" in dp.parse_with_vlm("x.jpg", "bill_received")[1]
    monkeypatch.setenv("FINMAN_VLM_URL", "https://v"); monkeypatch.setenv("FINMAN_VLM_KEY", "")
    assert "FINMAN_VLM_KEY" in dp.parse_with_vlm("x.jpg", "bill_received")[1]

def test_vlm_server_unreachable(vlm_env):
    with patch.object(dp.requests, "post", side_effect=requests.exceptions.ConnectionError()):
        r, reason = dp.parse_with_vlm("x.jpg", "bill_received")
    assert r is None and "cannot reach" in reason

def test_vlm_timeout(vlm_env):
    with patch.object(dp.requests, "post", side_effect=requests.exceptions.Timeout()):
        assert "too long" in dp.parse_with_vlm("x.jpg", "bill_received")[1]

@pytest.mark.parametrize("status, expected", [(401, "rejected the key"), (500, "HTTP 500")])
def test_vlm_http_errors(vlm_env, status, expected):
    with patch.object(dp.requests, "post", return_value=_resp(status)):
        assert expected in dp.parse_with_vlm("x.jpg", "bill_received")[1]

def test_vlm_empty_result_rejected(vlm_env):
    with patch.object(dp.requests, "post", return_value=_resp(200, {})):
        assert "no usable fields" in dp.parse_with_vlm("x.jpg", "bill_received")[1]

def test_vlm_bill_received_keeps_expenses_only(vlm_env):
    with patch.object(dp.requests, "post", return_value=_resp(200, dict(GOOD))), \
         patch.object(dp, "_amount_in_document", return_value=True):
        r, err = dp.parse_with_vlm("x.jpg", "bill_received")
    assert err is None and r["engine"] == "vlm"
    assert r["expenses"] == 100.0 and r["income"] is None
    assert r["amount_low_confidence"] is False

def test_vlm_receipt_issued_keeps_income_only(vlm_env):
    with patch.object(dp.requests, "post", return_value=_resp(200, dict(GOOD))), \
         patch.object(dp, "_amount_in_document", return_value=True):
        r, _ = dp.parse_with_vlm("x.jpg", "receipt_issued")
    assert r["income"] == 100.0 and r["expenses"] is None

def test_vlm_low_token_probability_flags_amount(vlm_env):
    data = dict(GOOD, confidence={"min_token_probability": 0.3})
    with patch.object(dp.requests, "post", return_value=_resp(200, data)), \
         patch.object(dp, "_amount_in_document", return_value=True):
        r, _ = dp.parse_with_vlm("x.jpg", "bill_received")
    assert r["amount_low_confidence"] is True

def test_vlm_amount_missing_from_document_is_flagged(vlm_env):
    with patch.object(dp.requests, "post", return_value=_resp(200, dict(GOOD))), \
         patch.object(dp, "_amount_in_document", return_value=False):
        r, _ = dp.parse_with_vlm("x.jpg", "bill_received")
    assert r["amount_low_confidence"] is True
    assert "could not be found" in r["amount_low_confidence_reason"]


# ---------- Amount cross-check ----------
def test_amount_found_in_document_text():
    with patch.object(dp, "extract_text", return_value="Total RM 1 250.50"):
        assert dp._amount_in_document("x.pdf", 1250.50) is True

def test_amount_absent_from_document_text():
    with patch.object(dp, "extract_text", return_value="Total RM 999.00"):
        assert dp._amount_in_document("x.pdf", 1250.50) is False

def test_amount_check_inconclusive_when_no_text_or_error():
    with patch.object(dp, "extract_text", return_value="   "):
        assert dp._amount_in_document("x.pdf", 10) is None
    with patch.object(dp, "extract_text", side_effect=RuntimeError()):
        assert dp._amount_in_document("x.pdf", 10) is None


# ---------- Entry point ----------
def test_parse_document_prefers_vlm_result():
    with patch.object(dp, "parse_with_vlm", return_value=({"engine": "vlm"}, None)):
        assert dp.parse_document("x.jpg", "bill_received")["engine"] == "vlm"

def test_parse_document_falls_back_to_ocr_with_reason():
    with patch.object(dp, "parse_with_vlm", return_value=(None, "server offline")), \
         patch.object(dp, "extract_text", return_value=INVOICE_TEXT):
        r = dp.parse_document("x.pdf", "invoice_issued")
    assert r["engine"] == "tesseract" and r["vlm_fallback_reason"] == "server offline"
    assert r["invoices"] == 1250.50