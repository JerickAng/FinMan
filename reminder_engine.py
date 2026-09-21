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


def run_reminder_check(db, cfo_uid, data_uid, issuer):
    """The core automation. For every overdue invoice belonging to the
    linked SME, find the most advanced rule stage it has now reached,
    and if that stage hasn't already been sent, email it and log it.

    Returns a dict summary: {"sent": int, "skipped": int, "errors": list}
    """
    from app import _get_receivables  # reuse the live overdue calculation

    rules = _get_active_rules(db, cfo_uid)
    if not rules:
        rules = DEFAULT_RULES

    receivables = _get_receivables(data_uid)
    overdue     = [i for i in receivables if i["days_overdue"] > 0]

    sent, skipped, errors = 0, 0, []

    for inv in overdue:
        # Pick the highest-day rule this invoice now qualifies for
        applicable = [r for r in rules if inv["days_overdue"] >= r["days"]]
        if not applicable:
            skipped += 1
            continue
        rule = max(applicable, key=lambda r: r["days"])

        last_stage_days = inv.get("last_reminder_stage_days", -1)
        if last_stage_days >= rule["days"]:
            # Already reminded at this stage or a later one -- don't repeat
            skipped += 1
            continue

        client_email = inv.get("client_email", "")
        if not client_email:
            skipped += 1
            errors.append(f"{inv.get('invoice_no', inv['id'])}: no client email on file")
            continue

        body = rule["template"].format(
            name=inv.get("client_name", "there"),
            inv_no=inv.get("invoice_no", inv["id"]),
            amount=f"{inv['amount']:,.2f}",
            due_date=inv.get("due_date", ""),
            days_overdue=inv["days_overdue"],
        )
        subject = f"{rule['stage']}: Invoice {inv.get('invoice_no', '')} overdue"

        try:
            _send_reminder_email(
                to_email=client_email,
                to_name=inv.get("client_name", ""),
                subject=subject,
                body=body,
                from_email=issuer.get("email", ""),
                from_name=issuer.get("company") or issuer.get("name", "FinMan"),
            )
            email_status = "sent"
        except ReminderEmailNotConfigured:
            # Still log it so the CFO can see what *would* have gone out --
            # matches the same graceful-degradation pattern as invoice emails
            email_status = "not_configured"
        except Exception as e:
            errors.append(f"{inv.get('invoice_no', inv['id'])}: {e}")
            continue

        # Record the log entry regardless, so the history table is honest
        # about what the system attempted
        db.collection("users").document(cfo_uid).collection("reminderLogs").add({
            "invoice_no":   inv.get("invoice_no", inv["id"]),
            "recipient":    client_email,
            "stage":        rule["stage"],
            "sent_at":      datetime.now(timezone.utc).isoformat(),
            "email_status": email_status,
        })

        # Mark the invoice itself so we never re-send this stage again
        db.collection("users").document(data_uid)\
          .collection("invoices").document(inv["id"]).update({
              "last_reminder_stage":      rule["stage"],
              "last_reminder_stage_days": rule["days"],
          })

        sent += 1

    return {"sent": sent, "skipped": skipped, "errors": errors}