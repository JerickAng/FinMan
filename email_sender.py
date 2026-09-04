"""
email_sender.py
Sends the generated invoice PDF to the client via SendGrid. If SendGrid
isn't configured, this fails gracefully with a clear reason rather than
raising -- the invoice is still saved and downloadable either way, since
email delivery is a nice-to-have on top of record-keeping, not a
precondition for it.
"""
import base64
import os


class EmailNotConfigured(Exception):
    pass


def send_invoice_email(to_email: str, to_name: str, invoice_no: str,
                        total: float, due_date: str, pdf_path: str,
                        from_email: str = None, from_name: str = None):
    """
    Raises EmailNotConfigured if SENDGRID_API_KEY isn't set -- callers
    should catch this specifically and treat it as "email skipped", not
    as a failure of the invoice creation itself.
    """
    api_key = os.getenv("SENDGRID_API_KEY")
    if not api_key:
        raise EmailNotConfigured("SENDGRID_API_KEY is not set")

    from sendgrid import SendGridAPIClient
    from sendgrid.helpers.mail import Mail, Attachment, FileContent, FileName, FileType, Disposition

    sender = from_email or os.getenv("SENDGRID_FROM_EMAIL")
    if not sender:
        raise EmailNotConfigured("No sender email configured (SENDGRID_FROM_EMAIL)")

    subject = f"Invoice {invoice_no} — RM {total:,.2f}"
    body = (
        f"Hi {to_name or 'there'},<br><br>"
        f"Please find attached invoice <b>{invoice_no}</b> for "
        f"<b>RM {total:,.2f}</b>, due <b>{due_date}</b>.<br><br>"
        f"Thank you for your business.<br><br>"
        f"{from_name or ''}"
    )

    message = Mail(
        from_email=sender,
        to_emails=to_email,
        subject=subject,
        html_content=body,
    )

    with open(pdf_path, "rb") as f:
        data = f.read()
    encoded = base64.b64encode(data).decode()
    attachment = Attachment(
        FileContent(encoded),
        FileName(f"{invoice_no}.pdf"),
        FileType("application/pdf"),
        Disposition("attachment"),
    )
    message.attachment = attachment

    sg = SendGridAPIClient(api_key)
    response = sg.send(message)
    return response.status_code
