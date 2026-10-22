import pytest
from invoice_generator import generate_invoice_pdf

ISSUER = {"company": "SME Sdn Bhd", "company_reg": "202001000001",
          "company_address": "1 Jalan Test\nPenang", "email": "cfo@sme.com"}

def invoice(**kw):
    base = {"invoice_no": "INV-001", "client_name": "Acme Trading",
            "client_email": "acme@example.com", "issue_date": "2026-10-01",
            "due_date": "2026-10-31", "total": 1500.0,
            "line_items": [{"description": "Consulting", "qty": 2, "unit_price": 500},
                           {"description": "Setup", "qty": 1, "unit_price": 500}]}
    base.update(kw)
    return base

def text_of(path):
    pdfplumber = pytest.importorskip("pdfplumber")
    with pdfplumber.open(path) as pdf:
        return "\n".join(p.extract_text() or "" for p in pdf.pages)


def test_pdf_file_is_created(tmp_path):
    out = tmp_path / "a.pdf"
    assert generate_invoice_pdf(invoice(), str(out), ISSUER) == str(out)
    assert out.read_bytes().startswith(b"%PDF") and out.stat().st_size > 1000

def test_pdf_contains_key_invoice_details(tmp_path):
    out = tmp_path / "a.pdf"
    generate_invoice_pdf(invoice(), str(out), ISSUER)
    t = text_of(out)
    for expected in ["INVOICE", "INV-001", "Acme Trading", "2026-10-31",
                     "SME Sdn Bhd", "Consulting", "1,000.00", "RM 1,500.00"]:
        assert expected in t

def test_line_amount_is_qty_times_price(tmp_path):
    out = tmp_path / "a.pdf"
    generate_invoice_pdf(invoice(line_items=[
        {"description": "Item", "qty": 3, "unit_price": "12.50"}], total=37.5), str(out), ISSUER)
    assert "37.50" in text_of(out)

def test_payment_link_and_notes_included_when_given(tmp_path):
    out = tmp_path / "a.pdf"
    generate_invoice_pdf(invoice(payment_url="https://pay/b1", notes="Thank you!"), str(out), ISSUER)
    t = text_of(out)
    assert "Pay this invoice online" in t and "Thank you!" in t

def test_payment_link_and_notes_omitted_when_absent(tmp_path):
    out = tmp_path / "a.pdf"
    generate_invoice_pdf(invoice(), str(out), ISSUER)
    t = text_of(out)
    assert "Pay this invoice online" not in t and "Notes" not in t

def test_issuer_name_used_when_no_company(tmp_path):
    out = tmp_path / "a.pdf"
    generate_invoice_pdf(invoice(), str(out), {"name": "Jane Doe", "email": "j@x.com"})
    assert "Jane Doe" in text_of(out)

def test_missing_required_field_raises(tmp_path):
    bad = invoice(); del bad["invoice_no"]
    with pytest.raises(KeyError):
        generate_invoice_pdf(bad, str(tmp_path / "a.pdf"), ISSUER)