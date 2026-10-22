from unittest.mock import MagicMock, patch
import pytest
import reminder_engine as re_


# ---------- helpers ----------
def rule_docs(rules):
    docs = []
    for i, r in enumerate(rules):
        d = MagicMock(); d.id = f"r{i}"; d.to_dict.return_value = dict(r)
        docs.append(d)
    return docs

def make_db(rules=None):
    """Mock Firestore whose reminderRules stream returns the given rules."""
    db = MagicMock()
    db.collection.return_value.document.return_value \
      .collection.return_value.stream.return_value = rule_docs(rules or [])
    return db

def inv(days_overdue, **kw):
    base = {"id": "i1", "invoice_no": "INV-001", "client_name": "Acme",
            "client_email": "acme@example.com", "amount": 1500.5,
            "due_date": "2026-10-01", "days_overdue": days_overdue}
    base.update(kw)
    return base

ISSUER = {"email": "cfo@sme.com", "company": "SME Sdn Bhd"}


# ---------- Default rules ----------
def test_default_rules_cover_four_escalating_stages():
    assert [r["days"] for r in re_.DEFAULT_RULES] == [1, 7, 14, 21]
    assert [r["stage"] for r in re_.DEFAULT_RULES] == \
        ["Gentle Reminder", "Follow-up", "Firm Notice", "Final Warning"]

def test_ensure_default_rules_seeds_when_empty():
    db = MagicMock()
    coll = db.collection.return_value.document.return_value.collection.return_value
    coll.limit.return_value.stream.return_value = []
    re_.ensure_default_rules(db, "cfo1")
    assert coll.add.call_count == 4

def test_ensure_default_rules_does_not_duplicate():
    db = MagicMock()
    coll = db.collection.return_value.document.return_value.collection.return_value
    coll.limit.return_value.stream.return_value = [MagicMock()]
    re_.ensure_default_rules(db, "cfo1")
    coll.add.assert_not_called()


# ---------- Rule selection ----------
@pytest.mark.parametrize("days, stage", [
    (1, "Gentle Reminder"), (6, "Gentle Reminder"), (7, "Follow-up"),
    (14, "Firm Notice"), (21, "Final Warning"), (90, "Final Warning"),
])
def test_pick_rule_highest_reached_stage(days, stage):
    assert re_._pick_rule(re_.DEFAULT_RULES, days)["stage"] == stage

def test_pick_rule_none_when_not_reached():
    assert re_._pick_rule(re_.DEFAULT_RULES, 0) is None

def test_active_rules_sorted_by_days():
    db = make_db([{"days": 14, "stage": "B"}, {"days": 1, "stage": "A"}])
    assert [r["days"] for r in re_._get_active_rules(db, "cfo1")] == [1, 14]


# ---------- Email configuration ----------
def test_send_email_raises_when_not_configured(monkeypatch):
    monkeypatch.delenv("SENDGRID_API_KEY", raising=False)
    monkeypatch.delenv("SENDGRID_FROM_EMAIL", raising=False)
    with pytest.raises(re_.ReminderEmailNotConfigured):
        re_._send_reminder_email("a@b.com", "A", "s", "b", "x@y.com", "X")


# ---------- Delivery ----------
def test_deliver_success_logs_and_marks_invoice():
    db = MagicMock()
    with patch.object(re_, "_send_reminder_email") as send:
        status, _ = re_._deliver(db, "cfo1", "sme1", ISSUER, inv(8), re_.DEFAULT_RULES[1])
    assert status == "sent"
    body = send.call_args.kwargs["body"]
    assert "INV-001" in body and "1,500.50" in body and "8 days overdue" in body
    assert send.call_args.kwargs["subject"] == "Follow-up: Invoice INV-001 overdue"
    update = db.collection.return_value.document.return_value.collection.return_value \
               .document.return_value.update
    update.assert_called_once()
    assert update.call_args.args[0]["last_reminder_stage_days"] == 7

def test_deliver_without_client_email_is_error():
    with patch.object(re_, "_send_reminder_email") as send:
        status, detail = re_._deliver(MagicMock(), "c", "s", ISSUER,
                                      inv(8, client_email=""), re_.DEFAULT_RULES[1])
    assert status == "error" and "no client email" in detail
    send.assert_not_called()

def test_deliver_not_configured_does_not_log_or_mark():
    db = MagicMock()
    with patch.object(re_, "_send_reminder_email", side_effect=re_.ReminderEmailNotConfigured()):
        status, _ = re_._deliver(db, "c", "s", ISSUER, inv(8), re_.DEFAULT_RULES[1])
    assert status == "not_configured"
    db.collection.return_value.document.return_value.collection.return_value.add.assert_not_called()

def test_deliver_send_failure_reported_as_error():
    with patch.object(re_, "_send_reminder_email", side_effect=RuntimeError("smtp down")):
        status, detail = re_._deliver(MagicMock(), "c", "s", ISSUER, inv(8), re_.DEFAULT_RULES[1])
    assert status == "error" and "smtp down" in detail

def test_deliver_bad_template_reported_as_error():
    bad = {"days": 1, "stage": "Broken", "template": "Hi {unknown_field}"}
    status, detail = re_._deliver(MagicMock(), "c", "s", ISSUER, inv(2), bad)
    assert status == "error" and "template has a problem" in detail

def test_deliver_never_moves_stage_marker_backwards():
    db = MagicMock()
    with patch.object(re_, "_send_reminder_email"):
        re_._deliver(db, "c", "s", ISSUER, inv(8, last_reminder_stage_days=14),
                     re_.DEFAULT_RULES[1])
    update = db.collection.return_value.document.return_value.collection.return_value \
               .document.return_value.update
    update.assert_not_called()


# ---------- Daily run ----------
def test_run_sends_due_and_skips_not_due_and_already_sent():
    db = make_db([])        # no custom rules -> defaults
    receivables = [
        inv(8,  id="a"),                                    # Follow-up due
        inv(0,  id="b"),                                    # not overdue
        inv(8,  id="c", last_reminder_stage_days=7),        # already sent this stage
    ]
    with patch.object(re_, "_send_reminder_email"):
        result = re_.run_reminder_check(db, "cfo1", "sme1", ISSUER, receivables)
    assert result["sent"] == 1 and result["skipped"] == 1 and result["errors"] == []

def test_run_reports_pending_when_email_not_configured():
    with patch.object(re_, "_send_reminder_email", side_effect=re_.ReminderEmailNotConfigured()):
        result = re_.run_reminder_check(make_db([]), "c", "s", ISSUER, [inv(8)])
    assert result["sent"] == 0
    assert any("SENDGRID_API_KEY" in e for e in result["errors"])

def test_run_with_no_receivables():
    assert re_.run_reminder_check(make_db([]), "c", "s", ISSUER, []) == \
        {"sent": 0, "skipped": 0, "errors": []}


# ---------- Manual send ----------
def test_send_now_unknown_invoice_rejected():
    ok, msg = re_.send_reminder_now(make_db([]), "c", "s", ISSUER, "nope", [inv(8)])
    assert ok is False and "isn't overdue" in msg

def test_send_now_not_overdue_rejected():
    ok, _ = re_.send_reminder_now(make_db([]), "c", "s", ISSUER, "i1", [inv(0)])
    assert ok is False

def test_send_now_success_message():
    with patch.object(re_, "_send_reminder_email"):
        ok, msg = re_.send_reminder_now(make_db([]), "c", "s", ISSUER, "i1", [inv(15)])
    assert ok is True and "Firm Notice sent to acme@example.com" in msg


# ---------- Annotation and queue ----------
def test_annotate_next_stage_due_now():
    out = re_.annotate_invoices(make_db([]), "c", [inv(8)])
    assert out[0]["next_stage"] == "Follow-up" and out[0]["next_in_days"] == 0

def test_annotate_waiting_for_next_stage():
    out = re_.annotate_invoices(make_db([]), "c", [inv(8, last_reminder_stage_days=7)])
    assert out[0]["next_stage"] == "Firm Notice" and out[0]["next_in_days"] == 6

def test_annotate_sequence_complete():
    out = re_.annotate_invoices(make_db([]), "c", [inv(30, last_reminder_stage_days=21)])
    assert out[0]["next_stage"] is None and out[0]["next_in_days"] is None

def test_queue_ladder_states_and_preview():
    q = re_.build_queue(make_db([]), "c", [inv(8, last_reminder_stage_days=1)])[0]
    states = {l["days"]: l["state"] for l in q["ladder"]}
    assert states == {1: "sent", 7: "ready", 14: "upcoming", 21: "upcoming"}
    assert q["ready"] is True and q["has_email"] is True
    assert "INV-001" in q["preview"]

def test_queue_ready_invoices_listed_first():
    items = [inv(2, id="x"), inv(8, id="y", last_reminder_stage_days=7), inv(30, id="z")]
    order = [i["id"] for i in re_.build_queue(make_db([]), "c", items)]
    assert order[0] in ("x", "z") and order[-1] == "y"

def test_queue_flags_missing_email():
    q = re_.build_queue(make_db([]), "c", [inv(8, client_email="")])[0]
    assert q["has_email"] is False