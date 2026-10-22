import base64
import os


class EmailNotConfigured(Exception):
    pass


def send_invoice_email(to_email: str, to_name: str, invoice_no: str,
                        total: float, due_date: str, pdf_path: str,
                        from_email: str = None, from_name: str = None,
                        payment_url: str = None):

    api_key = os.getenv("SENDGRID_API_KEY")
    if not api_key:
        raise EmailNotConfigured("SENDGRID_API_KEY is not set")

    from sendgrid import SendGridAPIClient
    from sendgrid.helpers.mail import Mail, Attachment, FileContent, FileName, FileType, Disposition

    sender = os.getenv("SENDGRID_FROM_EMAIL") or from_email
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
    if payment_url:
        body += (
            f'<br><br><a href="{payment_url}" '
            f'style="background:#2563EB;color:#ffffff;padding:10px 20px;'
            f'border-radius:6px;text-decoration:none;display:inline-block;">'
            f'Pay Now</a>'
        )

    message = Mail(
        from_email=sender,
        to_emails=to_email,
        subject=subject,
        html_content=body,
    )
    # Client replies go to the real issuer's inbox even though SendGrid
    # delivers the message under the verified sender address above
    if from_email and from_email != sender:
        message.reply_to = from_email

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