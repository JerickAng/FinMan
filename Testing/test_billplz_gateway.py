import hashlib
import hmac
from unittest.mock import MagicMock, patch
import pytest
import billplz_gateway as bp


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("BILLPLZ_API_KEY", "test-key")
    monkeypatch.setenv("BILLPLZ_COLLECTION_ID", "col123")
    monkeypatch.setenv("BILLPLZ_X_SIGNATURE_KEY", "secret")
    monkeypatch.delenv("BILLPLZ_SANDBOX", raising=False)


def _resp(json_data):
    r = MagicMock(); r.json.return_value = json_data; r.raise_for_status.return_value = None
    return r

def sign(data, key="secret"):
    fields = {k: v for k, v in data.items() if k.lower() != "x_signature"}
    src = "|".join(f"{k}{fields[k]}" for k in sorted(fields, key=str.lower))
    return hmac.new(key.encode(), src.encode(), hashlib.sha256).hexdigest()


# ---------- Configuration ----------
def test_sandbox_is_default():
    assert bp._base_url() == bp.SANDBOX_BASE

def test_production_when_sandbox_false(monkeypatch):
    monkeypatch.setenv("BILLPLZ_SANDBOX", "false")
    assert bp._base_url() == bp.PRODUCTION_BASE

def test_missing_api_key_raises(monkeypatch):
    monkeypatch.delenv("BILLPLZ_API_KEY")
    with pytest.raises(bp.BillplzNotConfigured):
        bp._api_key()


# ---------- create_bill ----------
def test_create_bill_converts_rm_to_sen_and_returns_link():
    with patch.object(bp.requests, "post", return_value=_resp({"id": "b1", "url": "https://pay/b1"})) as post:
        out = bp.create_bill(amount=19.99, email="a@b.com", name="Acme",
                             description="Invoice 1", callback_url="https://cb")
    assert out == {"id": "b1", "url": "https://pay/b1"}
    sent = post.call_args.kwargs
    assert sent["data"]["amount"] == 1999
    assert sent["data"]["collection_id"] == "col123"
    assert sent["auth"] == ("test-key", "")

def test_create_bill_truncates_long_description():
    with patch.object(bp.requests, "post", return_value=_resp({"id": "b", "url": "u"})) as post:
        bp.create_bill(amount=1, email="a@b.com", name="A",
                       description="x" * 500, callback_url="https://cb")
    assert len(post.call_args.kwargs["data"]["description"]) == 200

def test_create_bill_includes_optional_fields_only_when_given():
    with patch.object(bp.requests, "post", return_value=_resp({"id": "b", "url": "u"})) as post:
        bp.create_bill(amount=1, email="a@b.com", name="A", description="d",
                       callback_url="https://cb", redirect_url="https://r",
                       reference_1_label="Invoice", reference_1="INV-1")
    d = post.call_args.kwargs["data"]
    assert d["redirect_url"] == "https://r" and d["reference_1"] == "INV-1"
    with patch.object(bp.requests, "post", return_value=_resp({"id": "b", "url": "u"})) as post:
        bp.create_bill(amount=1, email="a@b.com", name="A", description="d", callback_url="https://cb")
    assert "redirect_url" not in post.call_args.kwargs["data"]

def test_create_bill_requires_collection_id(monkeypatch):
    monkeypatch.delenv("BILLPLZ_COLLECTION_ID")
    with pytest.raises(bp.BillplzNotConfigured):
        bp.create_bill(amount=1, email="a@b.com", name="A", description="d", callback_url="x")


# ---------- get_bill ----------
def test_get_bill_returns_json():
    with patch.object(bp.requests, "get", return_value=_resp({"id": "b1", "paid": True})) as get:
        assert bp.get_bill("b1")["paid"] is True
    assert get.call_args.args[0].endswith("/bills/b1")


# ---------- Callback signature ----------
def test_valid_signature_accepted():
    data = {"id": "b1", "paid": "true", "amount": "1999"}
    data["x_signature"] = sign(data)
    assert bp.verify_callback_signature(data) is True

def test_tampered_amount_rejected():
    data = {"id": "b1", "paid": "true", "amount": "1999"}
    data["x_signature"] = sign(data)
    data["amount"] = "1"
    assert bp.verify_callback_signature(data) is False

def test_wrong_secret_rejected():
    data = {"id": "b1", "paid": "true"}
    data["x_signature"] = sign(data, key="other")
    assert bp.verify_callback_signature(data) is False

def test_missing_signature_rejected():
    assert bp.verify_callback_signature({"id": "b1", "paid": "true"}) is False

def test_signature_rejected_when_secret_not_configured(monkeypatch):
    data = {"id": "b1"}; data["x_signature"] = sign(data)
    monkeypatch.delenv("BILLPLZ_X_SIGNATURE_KEY")
    assert bp.verify_callback_signature(data) is False

def test_signature_independent_of_field_order():
    a = {"id": "b1", "paid": "true", "amount": "5"}
    sig = sign(a)
    b = {"amount": "5", "x_signature": sig, "paid": "true", "id": "b1"}
    assert bp.verify_callback_signature(b) is True