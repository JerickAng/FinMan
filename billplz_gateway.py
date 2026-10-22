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

    resp = requests.get(
        f"{_base_url()}/bills/{bill_id}",
        auth=(_api_key(), ""),
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def verify_callback_signature(form_data: dict) -> bool:

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