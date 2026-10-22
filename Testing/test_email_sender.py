from unittest.mock import MagicMock, patch
import pytest
import email_sender as es

pytest.importorskip("sendgrid")


@pytest.fixture
def pdf(tmp_path):
    p = tmp_path / "inv.pdf"; p.write_bytes(b"%PDF-1.4 test")
    return str(p)

@pytest.fixture
def client():
    """Replaces SendGridAPIClient so no real email is sent."""
    with patch("sendgrid.SendGridAPIClient") as cls:
        cls.return_value.send.return_value.status_code = 202
        yield cls

def send(pdf, **kw):
    args = dict(to_email="c@x.com", to_name="Acme", invoice_no="INV-001",
                total=1500.0, due_date="2026-11-01", pdf_path=pdf,
                from_email="issuer@sme.com", from_name="SME Sdn Bhd")
    args.update(kw)
    return es.send_invoice_email(**args)


def test_missing_api_key_raises(monkeypatch, pdf):
    monkeypatch.delenv("SENDGRID_API_KEY", raising=False)
    with pytest.raises(es.EmailNotConfigured):
        send(pdf)

def test_missing_sender_raises(monkeypatch, pdf):
    monkeypatch.setenv("SENDGRID_API_KEY", "k")
    monkeypatch.delenv("SENDGRID_FROM_EMAIL", raising=False)
    with pytest.raises(es.EmailNotConfigured):
        send(pdf, from_email=None)

def test_success_returns_status_and_builds_message(monkeypatch, pdf, client):
    monkeypatch.setenv("SENDGRID_API_KEY", "k")
    monkeypatch.setenv("SENDGRID_FROM_EMAIL", "verified@finman.com")
    assert send(pdf) == 202
    msg = client.return_value.send.call_args.args[0].get()
    assert msg["subject"] == "Invoice INV-001 — RM 1,500.00"
    assert msg["from"]["email"] == "verified@finman.com"
    assert msg["attachments"][0]["filename"] == "INV-001.pdf"
    assert msg["attachments"][0]["type"] == "application/pdf"

def test_reply_to_set_when_issuer_differs_from_verified_sender(monkeypatch, pdf, client):
    monkeypatch.setenv("SENDGRID_API_KEY", "k")
    monkeypatch.setenv("SENDGRID_FROM_EMAIL", "verified@finman.com")
    send(pdf)
    msg = client.return_value.send.call_args.args[0].get()
    assert msg["reply_to"]["email"] == "issuer@sme.com"

def test_payment_button_only_when_url_given(monkeypatch, pdf, client):
    monkeypatch.setenv("SENDGRID_API_KEY", "k")
    monkeypatch.setenv("SENDGRID_FROM_EMAIL", "verified@finman.com")
    send(pdf, payment_url="https://pay/b1")
    html = client.return_value.send.call_args.args[0].get()["content"][0]["value"]
    assert "Pay Now" in html and "https://pay/b1" in html
    send(pdf)
    html = client.return_value.send.call_args.args[0].get()["content"][0]["value"]
    assert "Pay Now" not in html

def test_greeting_falls_back_when_no_name(monkeypatch, pdf, client):
    monkeypatch.setenv("SENDGRID_API_KEY", "k")
    monkeypatch.setenv("SENDGRID_FROM_EMAIL", "verified@finman.com")
    send(pdf, to_name="")
    html = client.return_value.send.call_args.args[0].get()["content"][0]["value"]
    assert html.startswith("Hi there,")