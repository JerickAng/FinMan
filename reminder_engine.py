"""
reminder_engine.py
The actual automation behind FinMan's debtor reminder feature.

Previously, /cfo/reminders only let a CFO save rule *definitions* --
nothing ever read them back, matched them against real overdue invoices,
sent an email, or wrote a history log entry. This module is that missing
piece: given a CFO's configured rules (or the four sensible defaults) and
their linked SME's actual overdue invoices, it decides who needs a
reminder right now, sends it, and records that it happened.
"""
import os
from datetime import datetime, timezone

DEFAULT_RULES = [
    {"days": 1,  "stage": "Gentle Reminder",
     "template": "Hi {name}, just a friendly reminder that invoice {inv_no} "
                 "for RM {amount} was due on {due_date}. Let us know if you "
                 "have any questions!"},
    {"days": 7,  "stage": "Follow-up",
     "template": "Hi {name}, we noticed invoice {inv_no} for RM {amount} "
                 "is now {days_overdue} days overdue. Please arrange "
                 "payment at your earliest convenience."},
    {"days": 14, "stage": "Firm Notice",
     "template": "Hi {name}, invoice {inv_no} for RM {amount} is now "
                 "{days_overdue} days overdue. This is a formal notice "
                 "requesting immediate payment."},
    {"days": 21, "stage": "Final Warning",
     "template": "Hi {name}, invoice {inv_no} for RM {amount} remains "
                 "unpaid at {days_overdue} days overdue. Immediate action "
                 "is required to avoid further steps."},
]


class ReminderEmailNotConfigured(Exception):
    """Raised when SendGrid credentials aren't set -- mirrors the same
    graceful-failure pattern used by email_sender.py for invoices."""
    pass


def _send_reminder_email(to_email, to_name, subject, body, from_email, from_name):
    api_key       = os.getenv("SENDGRID_API_KEY")
    verified_from = os.getenv("SENDGRID_FROM_EMAIL")
    if not api_key or not verified_from:
        raise ReminderEmailNotConfigured()

    from sendgrid import SendGridAPIClient
    from sendgrid.helpers.mail import Mail

    message = Mail(
        from_email=(verified_from, from_name or "FinMan"),  # must be your verified sender
        to_emails=to_email,
        subject=subject,
        plain_text_content=body,
    )
    # Replies from the client land in the real issuer's inbox, even though
    # SendGrid delivers under the verified identity above
    if from_email:
        message.reply_to = from_email

    sg = SendGridAPIClient(api_key)
    sg.send(message)


def ensure_default_rules(db, cfo_uid):
    """Seeds the four default rules into Firestore the first time a CFO
    opens the reminders page, so they become real, editable documents
    instead of hardcoded template rows that can never be changed."""
    existing = list(db.collection("users").document(cfo_uid)
                       .collection("reminderRules").limit(1).stream())
    if existing:
        return
    for rule in DEFAULT_RULES:
        db.collection("users").document(cfo_uid)\
          .collection("reminderRules").add(dict(rule))


def _get_active_rules(db, cfo_uid):
    docs = db.collection("users").document(cfo_uid)\
             .collection("reminderRules").stream()
    rules = [d.to_dict() | {"id": d.id} for d in docs]
    return sorted(rules, key=lambda r: r.get("days", 0))


def _pick_rule(rules, days_overdue):
    """Highest-day rule an invoice has reached, or None if it hasn't reached any."""
    applicable = [r for r in rules if days_overdue >= r["days"]]
    return max(applicable, key=lambda r: r["days"]) if applicable else None


def _deliver(db, cfo_uid, data_uid, issuer, inv, rule):
    """Email one reminder, then log it and mark the invoice.
    Returns (status, detail): status is "sent", "not_configured" or "error"."""
    inv_no       = inv.get("invoice_no", inv["id"])
    client_email = inv.get("client_email", "")
    if not client_email:
        return "error", f"{inv_no}: no client email on file"

    try:
        body = rule["template"].format(
            name=inv.get("client_name", "there"),
            inv_no=inv_no,
            amount=f"{inv['amount']:,.2f}",
            due_date=inv.get("due_date", ""),
            days_overdue=inv["days_overdue"],
        )
    except (KeyError, IndexError, ValueError) as e:
        return "error", f"{inv_no}: the '{rule['stage']}' template has a problem ({e})"

    subject = f"{rule['stage']}: Invoice {inv_no} overdue"

    try:
        _send_reminder_email(
            to_email=client_email,
            to_name=inv.get("client_name", ""),
            subject=subject,
            body=body,
            from_email=issuer.get("email", ""),
            from_name=issuer.get("company") or issuer.get("name", "FinMan"),
        )
    except ReminderEmailNotConfigured:
        # Nothing was sent, so don't log it or mark the invoice;
        # this stage stays due until email is configured
        return "not_configured", ""
    except Exception as e:
        return "error", f"{inv_no}: {e}"

    db.collection("users").document(cfo_uid).collection("reminderLogs").add({
        "invoice_no":   inv_no,
        "recipient":    client_email,
        "stage":        rule["stage"],
        "sent_at":      datetime.now(timezone.utc).isoformat(),
        "email_status": "sent",
    })

    # Mark the invoice so the daily run never re-sends this stage. Never move
    # the marker backwards (a manual send must not re-arm earlier stages).
    if rule["days"] >= inv.get("last_reminder_stage_days", -1):
        db.collection("users").document(data_uid)\
          .collection("invoices").document(inv["id"]).update({
              "last_reminder_stage":      rule["stage"],
              "last_reminder_stage_days": rule["days"],
          })

    return "sent", ""


def run_reminder_check(db, cfo_uid, data_uid, issuer, receivables=None):
    """The core automation. For every overdue invoice belonging to the
    linked SME, find the most advanced rule stage it has now reached,
    and if that stage hasn't already been sent, email it and log it.

    Returns a dict summary: {"sent": int, "skipped": int, "errors": list}
    """
    rules = _get_active_rules(db, cfo_uid) or DEFAULT_RULES

    if receivables is None:
        from app import _get_receivables  # reuse the live overdue calculation
        receivables = _get_receivables(data_uid)
    overdue = [i for i in receivables if i["days_overdue"] > 0]

    sent, skipped, pending, errors = 0, 0, 0, []

    for inv in overdue:
        rule = _pick_rule(rules, inv["days_overdue"])
        if rule is None or inv.get("last_reminder_stage_days", -1) >= rule["days"]:
            skipped += 1          # not due yet, or this stage already sent
            continue

        status, detail = _deliver(db, cfo_uid, data_uid, issuer, inv, rule)
        if status == "sent":
            sent += 1
        elif status == "not_configured":
            pending += 1
        else:
            errors.append(detail)

    if pending:
        errors.append(f"{pending} reminder(s) are due but email isn't configured "
                      f"(set SENDGRID_API_KEY and SENDGRID_FROM_EMAIL in .env).")

    return {"sent": sent, "skipped": skipped, "errors": errors}


def send_reminder_now(db, cfo_uid, data_uid, issuer, invoice_id, receivables):
    """Manual 'Send now' for one invoice. Sends the stage the invoice has
    currently reached, even if that stage was already sent automatically.
    Returns (ok: bool, message: str) for flashing."""
    inv = next((i for i in receivables
                if i["id"] == invoice_id and i["days_overdue"] > 0), None)
    if inv is None:
        return False, "That invoice isn't overdue (or no longer exists)."

    rules = _get_active_rules(db, cfo_uid) or DEFAULT_RULES
    rule  = _pick_rule(rules, inv["days_overdue"]) or min(rules, key=lambda r: r["days"])

    status, detail = _deliver(db, cfo_uid, data_uid, issuer, inv, rule)
    if status == "sent":
        return True, f"{rule['stage']} sent to {inv.get('client_email')}."
    if status == "not_configured":
        return False, ("Email isn't configured "
                       "(set SENDGRID_API_KEY and SENDGRID_FROM_EMAIL in .env).")
    return False, detail


def annotate_invoices(db, cfo_uid, invoices):
    """Adds reminder status to each overdue invoice for the Invoices page.
    next_stage / next_in_days: the stage that will go out next and how many
    days until then (0 = due at the next daily run, None = sequence complete)."""
    rules = sorted(_get_active_rules(db, cfo_uid) or DEFAULT_RULES,
                   key=lambda r: r["days"])
    for inv in invoices:
        last_days = inv.get("last_reminder_stage_days", -1)
        overdue   = inv["days_overdue"]

        due_now = [r for r in rules if last_days < r["days"] <= overdue]
        if due_now:
            nxt, wait = max(due_now, key=lambda r: r["days"]), 0
        else:
            later = [r for r in rules if r["days"] > max(last_days, overdue)]
            nxt   = later[0] if later else None
            wait  = (nxt["days"] - overdue) if nxt else None

        inv["next_stage"]   = nxt["stage"] if nxt else None
        inv["next_in_days"] = wait
    return invoices