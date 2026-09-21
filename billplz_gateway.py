import os
import hmac
import hashlib
import requests

SANDBOX_BASE    = "https://www.billplz-sandbox.com/api/v3"
PRODUCTION_BASE = "https://www.billplz.com/api/v3"


class BillplzNotConfigured(Exception):
    pass


def _base_url():
    is_sandbox = os.getenv("BILLPLZ_SANDBOX", "true").lower() != "false"
    return SANDBOX_BASE if is_sandbox else PRODUCTION_BASE


def _api_key():
    key = os.getenv("BILLPLZ_API_KEY")
    if not key:
        raise BillplzNotConfigured("BILLPLZ_API_KEY is not set")
    return key


def create_bill(*, amount, email, name, description, callback_url,
                 redirect_url=None, reference_1_label=None, reference_1=None):
    """
    Creates a Billplz Bill and returns {"id": bill_id, "url": payment_url}.

    amount must be in the smallest currency unit (sen, not RM) -- Billplz
    expects e.g. RM 150.00 as 15000.

    reference_1 / reference_1_label are optional metadata Billplz stores
    alongside the bill and echoes back -- used here to carry our own
    invoice_id so the webhook knows which invoice this bill belongs to
    without needing a separate lookup table for simple setups. (We also
    keep a Firestore lookup as the primary mechanism -- see app.py -- this
    is a convenience backup.)
    """
    collection_id = os.getenv("BILLPLZ_COLLECTION_ID")
    if not collection_id:
        raise BillplzNotConfigured("BILLPLZ_COLLECTION_ID is not set")

    payload = {
        "collection_id": collection_id,
        "email":         email,
        "name":          name,
        "amount":        int(round(amount * 100)),   # RM -> sen
        "description":   description[:200],           # Billplz caps this field
        "callback_url":  callback_url,
    }
    if redirect_url:
        payload["redirect_url"] = redirect_url
    if reference_1_label:
        payload["reference_1_label"] = reference_1_label
    if reference_1:
        payload["reference_1"] = reference_1

    resp = requests.post(
        f"{_base_url()}/bills",
        auth=(_api_key(), ""),   # Billplz uses HTTP Basic Auth, API key as username, blank password
        data=payload,
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    return {"id": data["id"], "url": data["url"]}


def get_bill(bill_id):
    """Fetches a bill's current status directly from Billplz -- used to
    double-check a webhook's claim server-side rather than trusting the
    callback payload alone."""
    resp = requests.get(
        f"{_base_url()}/bills/{bill_id}",
        auth=(_api_key(), ""),
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def verify_callback_signature(form_data: dict) -> bool:
    """
    NOTE: Billplz's newer "SSO" platform generates X Signature keys in a
    format (long hex) that doesn't match the shorter token format shown
    in Billplz's older public API documentation, and produces a
    signature this implementation of the documented algorithm can't
    reproduce. Rather than keep guessing at an undocumented algorithm
    variant, this function is no longer trusted as the sole gate --
    see billplz_webhook() in app.py, which now independently confirms
    payment status via a direct GET /bills/{id} call using the API key
    (already proven to work, since bill creation succeeds). That
    authenticated server-side check is the real security boundary here;
    this function is kept only as a best-effort extra signal and never
    blocks processing on its own.
    """
    x_sig_key = os.getenv("BILLPLZ_X_SIGNATURE_KEY")
    if not x_sig_key:
        return False

    received_sig = form_data.get("x_signature", "")
    if not received_sig:
        return False

    fields = {k: v for k, v in form_data.items() if k.lower() != "x_signature"}
    sorted_keys = sorted(fields.keys(), key=lambda k: k.lower())
    source_string = "|".join(f"{k}{fields[k]}" for k in sorted_keys)

    computed_sig = hmac.new(
        x_sig_key.encode(), source_string.encode(), hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(computed_sig, received_sig)