from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch
import pytest
import app as finman


@pytest.fixture
def client():
    finman.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    return finman.app.test_client()


# ---------- Password policy ----------
def test_strong_password_accepted():
    assert finman.validate_password("Abcdef1!") == []

@pytest.mark.parametrize("pw, missing", [
    ("Ab1!",        "at least 8 characters"),
    ("abcdefg1!",   "one uppercase letter"),
    ("ABCDEFG1!",   "one lowercase letter"),
    ("Abcdefg12",   "one special character"),
])
def test_weak_password_rejected(pw, missing):
    assert missing in finman.validate_password(pw)

def test_empty_password_fails_all_rules():
    assert len(finman.validate_password("")) == 4


# ---------- Idle timeout ----------
def test_idle_not_expired_when_recent():
    with finman.app.test_request_context():
        finman.session["last_active"] = datetime.utcnow().timestamp() - 60
        assert finman._is_idle_expired() is False

def test_idle_expired_after_timeout():
    with finman.app.test_request_context():
        finman.session["last_active"] = datetime.utcnow().timestamp() - 11 * 60
        assert finman._is_idle_expired() is True

def test_idle_expired_when_no_stamp():
    with finman.app.test_request_context():
        assert finman._is_idle_expired() is True


# ---------- Safe redirect ----------
@pytest.mark.parametrize("nxt, ok", [
    ("/sme/summary", True),
    ("//evil.com", False),
    ("https://evil.com", False),
    ("", False),
])
def test_safe_next(nxt, ok):
    with finman.app.test_request_context("/", method="POST", data={"next": nxt}):
        result = finman._safe_next("dashboard")
        assert (result == nxt) is ok


# ---------- Balance inputs ----------
def test_balance_inputs_use_latest_period():
    records = [{"period": "2026-02", "cash": 200},
               {"period": "2026-01", "cash": 100, "inventory": 50}]
    out = finman._get_balance_inputs(records)
    assert out["cash"] == 200 and out["inventory"] == 50


# ---------- Overdue detection ----------
def _doc(due, total=100):
    d = MagicMock(); d.id = "x"
    d.to_dict.return_value = {"due_date": due, "total": total, "status": "sent"}
    return d

@pytest.fixture
def mock_invoices():
    """Replaces app.db with a mock so no real Firestore call is made."""
    fake_db = MagicMock()
    def _set(docs):
        chain = (fake_db.collection.return_value.document.return_value
                 .collection.return_value.where.return_value)
        chain.stream.return_value = docs
    with patch.object(finman, "db", fake_db):
        yield _set


def test_overdue_invoice_detected(mock_invoices):
    past = (date.today() - timedelta(days=5)).isoformat()
    mock_invoices([_doc(past)])
    result = finman._get_overdue_invoices("uid")
    assert len(result) == 1 and result[0]["days_overdue"] == 5

def test_future_invoice_not_overdue(mock_invoices):
    future = (date.today() + timedelta(days=5)).isoformat()
    mock_invoices([_doc(future)])
    assert finman._get_overdue_invoices("uid") == []

def test_overdue_sorted_most_overdue_first(mock_invoices):
    a = (date.today() - timedelta(days=2)).isoformat()
    b = (date.today() - timedelta(days=9)).isoformat()
    mock_invoices([_doc(a), _doc(b)])
    days = [i["days_overdue"] for i in finman._get_overdue_invoices("uid")]
    assert days == [9, 2]

def test_invalid_due_date_not_overdue(mock_invoices):
    mock_invoices([_doc("not-a-date")])
    assert finman._get_overdue_invoices("uid") == []


# ---------- Authentication / role checks ----------
def test_protected_page_without_login_redirects(client):
    r = client.get("/sme/dashboard")
    assert r.status_code == 302 and "/login" in r.headers["Location"]

def test_expired_session_cookie_redirects_to_login(client):
    err = finman.auth.InvalidSessionCookieError("expired")
    client.set_cookie("finman_session", "bad", domain="localhost")
    with patch.object(finman.auth, "verify_session_cookie", side_effect=err):
        r = client.get("/sme/dashboard")
    assert r.status_code == 302 and "/login" in r.headers["Location"]

def test_cfo_blocked_from_sme_page(client):
    user = {"uid": "u1", "role": "cfo", "email": "", "name": "", "company": "",
            "company_reg": "", "company_address": "", "linked_sme_uid": "",
            "linked_sme_company": ""}
    client.set_cookie("finman_session", "ok", domain="localhost")
    with patch.object(finman.auth, "verify_session_cookie", return_value={"uid": "u1"}), \
         patch.object(finman, "_load_user", return_value=user), \
         patch.object(finman, "_is_idle_expired", return_value=False):
        r = client.get("/sme/dashboard")
    assert r.status_code == 302 and "/dashboard" in r.headers["Location"]

def test_suspended_user_blocked(client):
    client.set_cookie("finman_session", "ok", domain="localhost")
    with patch.object(finman.auth, "verify_session_cookie", return_value={"uid": "u2"}), \
         patch.object(finman, "_load_user", return_value=None), \
         patch.object(finman, "_is_idle_expired", return_value=False):
        r = client.get("/sme/dashboard")
    assert r.status_code == 302 and "/login" in r.headers["Location"]