from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, make_response
import firebase_admin
from firebase_admin import credentials, auth, firestore, storage
from functools import wraps
from dotenv import load_dotenv
from datetime import timedelta, datetime, timezone
from flask_wtf.csrf import CSRFProtect
import threading
import time
import os
import re
from billplz_gateway import verify_callback_signature, BillplzNotConfigured

# ── Global Thread Lock ────────────────────────────────────────────────────────
invoice_lock = threading.Lock()

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ["SECRET_KEY"]

# CSRF protection — every POST requires a valid token
csrf = CSRFProtect(app)

# ── Cookie / session hardening ────────────────────────────────────────────────
IS_PRODUCTION = os.getenv("FLASK_ENV") == "production"
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=IS_PRODUCTION,
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    WTF_CSRF_TIME_LIMIT=None,  # don't expire CSRF tokens mid-session
)

SESSION_COOKIE_NAME = "finman_session"
SESSION_EXPIRY      = timedelta(hours=8)    # absolute cap on one login
IDLE_TIMEOUT        = timedelta(minutes=10) # logged out after this much inactivity

# ── Firebase Admin SDK ────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
cred     = credentials.Certificate(os.path.join(BASE_DIR, "serviceAccountKey.json"))
if not firebase_admin._apps:
    firebase_admin.initialize_app(cred, {
        "storageBucket": os.getenv("FIREBASE_STORAGE_BUCKET", "finman-c0e9e.firebasestorage.app")
    })
db = firestore.client()
bucket = storage.bucket()


# ── Helpers ───────────────────────────────────────────────────────────────────
def _load_user(uid):
    """Re-reads role/status fresh from Firestore. Never trust client data."""
    doc = db.collection("users").document(uid).get()
    if not doc.exists:
        return None
    data = doc.to_dict()
    if data.get("status") == "suspended":
        return None
    return {
        "uid":             uid,
        "email":           data.get("email", ""),
        "role":            data.get("role", "sme"),
        "name":            data.get("name", data.get("email", "")),
        "company":         data.get("company", ""),
        "company_reg":     data.get("company_reg", ""),
        "company_address": data.get("company_address", ""),
        "linked_sme_uid":  data.get("linked_sme_uid", ""),
        "linked_sme_company": data.get("linked_sme_company", ""),
    }

def _touch_activity():
    """Record 'now' in Flask's signed session so the client cannot forge it."""
    session["last_active"] = datetime.utcnow().timestamp()

def _is_idle_expired():
    last = session.get("last_active")
    if last is None:
        return True   # signed-in cookie but no activity stamp: make them log in again
    return datetime.utcnow().timestamp() - last > IDLE_TIMEOUT.total_seconds()

def _clear_session_cookie(resp):
    resp.delete_cookie(SESSION_COOKIE_NAME)
    return resp

# ── Password policy ───────────────────────────────────────────────────────────
_PASSWORD_RULES = [
    (lambda p: len(p) >= 8,                         "at least 8 characters"),
    (lambda p: re.search(r"[A-Z]", p) is not None,  "one uppercase letter"),
    (lambda p: re.search(r"[a-z]", p) is not None,  "one lowercase letter"),
    (lambda p: re.search(r"[^A-Za-z0-9]", p) is not None, "one special character"),
]

def validate_password(password):
    return [msg for check, msg in _PASSWORD_RULES if not check(password)]


def _get_data_uid(uid):
    user_doc = db.collection("users").document(uid).get()
    if user_doc.exists:
        linked = user_doc.to_dict().get("linked_sme_uid", "")
        if linked:
            return linked
    return uid


def _get_outstanding_invoices_total(data_uid):
    invoices = [i.to_dict() for i in
                db.collection("users").document(data_uid).collection("invoices").stream()]
    return sum((i.get("total") or i.get("amount") or 0)
              for i in invoices if i.get("status") != "paid")

def _get_receivables(data_uid):
    from datetime import date
    today = date.today()
    docs = db.collection("users").document(data_uid)\
             .collection("invoices").where("status", "!=", "paid").stream()
    invoices = []
    for d in docs:
        inv = d.to_dict() | {"id": d.id}
        inv["amount"] = inv.get("total") or inv.get("amount") or 0
        due_raw = inv.get("due_date", "")
        days_overdue = 0
        for fmt in ("%Y-%m-%d", "%Y-%m"):
            try:
                due_date = datetime.strptime(due_raw, fmt).date()
                days_overdue = max((today - due_date).days, 0)
                break
            except (ValueError, TypeError):
                continue
        inv["days_overdue"] = days_overdue
        invoices.append(inv)
    return invoices

def _safe_next(default_endpoint):
    """Redirect target from a posted 'next' field - local paths only."""
    nxt = request.form.get("next", "")
    if nxt.startswith("/") and not nxt.startswith("//"):
        return nxt
    return url_for(default_endpoint)

def _get_overdue_invoices(data_uid):
    receivables = _get_receivables(data_uid)
    overdue = [i for i in receivables if i["days_overdue"] > 0]
    return sorted(overdue, key=lambda i: i["days_overdue"], reverse=True)


# ── Decorators ────────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        cookie = request.cookies.get(SESSION_COOKIE_NAME)
        if not cookie:
            flash("Please log in to continue.", "warning")
            return redirect(url_for("login"))
        try:
            decoded = auth.verify_session_cookie(cookie, check_revoked=True)
        except (auth.InvalidSessionCookieError,
                auth.RevokedSessionCookieError, ValueError):
            flash("Your session has expired. Please log in again.", "warning")
            return _clear_session_cookie(redirect(url_for("login")))
        if _is_idle_expired():
            # Revoke on the Firebase side too, so a copied cookie dies with it.
            try:
                auth.revoke_refresh_tokens(decoded["uid"])
            except Exception:
                pass
            session.clear()
            if request.path.startswith("/api/"):
                return _clear_session_cookie(
                    make_response(jsonify({"error": "idle_timeout"}), 401))
            flash("Your session expired due to inactivity. Please log in again to continue.", "warning")
            return _clear_session_cookie(redirect(url_for("login")))
        user = _load_user(decoded["uid"])
        if user is None:
            flash("Account not found or suspended.", "danger")
            return _clear_session_cookie(redirect(url_for("login")))
        session["user"] = user
        _touch_activity()
        return f(*args, **kwargs)
    return decorated

def role_required(*roles):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if session.get("user", {}).get("role") not in roles:
                flash("You do not have permission to access that page.", "danger")
                return redirect(url_for("dashboard"))
            return f(*args, **kwargs)
        return decorated
    return decorator


# ── Auth routes ───────────────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def index():
    if request.cookies.get(SESSION_COOKIE_NAME):
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))

@app.route("/login", methods=["GET", "POST"])
@csrf.exempt   # login uses Firebase JS SDK token, not a form CSRF token
def login():
    if request.cookies.get(SESSION_COOKIE_NAME):
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        id_token = request.form.get("id_token")
        try:
            decoded        = auth.verify_id_token(id_token, check_revoked=True)
            uid            = decoded["uid"]
            user           = _load_user(uid)
            if user is None:
                flash("Account not found or suspended. Contact your administrator.", "danger")
                return redirect(url_for("login"))
            session_cookie = auth.create_session_cookie(id_token, expires_in=SESSION_EXPIRY)
            resp           = redirect(url_for("dashboard"))
            _touch_activity()
            resp.set_cookie(
                SESSION_COOKIE_NAME, session_cookie,
                max_age=int(SESSION_EXPIRY.total_seconds()),
                httponly=True, secure=IS_PRODUCTION, samesite="Lax",
            )
            return resp
        except Exception:
            flash("Login failed. Please check your credentials and try again.", "danger")
            return redirect(url_for("login"))
    return render_template("login.html")

@app.route("/api/keepalive", methods=["POST"])
@login_required
def keepalive():
    """Called by static/js/idle_timeout.js when the user is genuinely active
    (typing, clicking) without loading a new page. CSRF-protected."""
    _touch_activity()
    return jsonify({"ok": True})

@app.route("/logout", methods=["POST"])
def logout():
    idle = request.args.get("reason") == "idle"
    uid = session.get("user", {}).get("uid")
    session.clear()
    if idle:
        # logged out by the inactivity timer in idle_timeout.js
        if uid:
            try:
                auth.revoke_refresh_tokens(uid)
            except Exception:
                pass
        flash("Your session expired due to inactivity. Please log in again to continue.", "warning")
    else:
        flash("You have been logged out.", "info")
    return _clear_session_cookie(redirect(url_for("login")))

@app.route("/reset-password")
def reset_password():
    resp = make_response(render_template("reset_password.html"))
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


@app.route("/dashboard")
@login_required
def dashboard():
    role = session["user"]["role"]
    if role == "admin":
        return redirect(url_for("admin_accounts"))
    elif role == "cfo":
        return redirect(url_for("cfo_dashboard"))
    else:
        return redirect(url_for("sme_dashboard"))


# ── SME routes ────────────────────────────────────────────────────────────────
@app.route("/sme/dashboard")
@login_required
@role_required("sme")
def sme_dashboard():
    uid     = session["user"]["uid"]
    records = [r.to_dict() for r in
               db.collection("users").document(uid)
                 .collection("financialRecords").stream()]
    outstanding_invoices = _get_outstanding_invoices_total(uid)

    # Aggregate records by period for the trend charts (sorted chronologically)
    by_period = {}
    for r in records:
        period = r.get("period", "Unknown")
        entry  = by_period.setdefault(period, {"income": 0, "expenses": 0})
        entry["income"]   += float(r.get("income", 0) or 0)
        entry["expenses"] += float(r.get("expenses", 0) or 0)

    sorted_periods = sorted(by_period.keys())
    monthly = {
        "labels":   sorted_periods,
        "income":   [by_period[p]["income"] for p in sorted_periods],
        "expenses": [by_period[p]["expenses"] for p in sorted_periods],
        "net":      [by_period[p]["income"] - by_period[p]["expenses"] for p in sorted_periods],
    }

    return render_template("sme/dashboard.html", user=session["user"],
                           records=records, outstanding_invoices=outstanding_invoices,
                           monthly=monthly)

BALANCE_FIELDS = ("cash", "inventory", "accounts_payable", "other_current_liabilities")

def _opt_float(name):
    v = (request.form.get(name) or "").strip()
    return float(v) if v else None

def _get_balance_inputs(records):
    """Latest entered value per balance-sheet field, by reporting period."""
    out = {}
    for r in sorted(records, key=lambda r: r.get("period") or ""):
        for k in BALANCE_FIELDS:
            if r.get(k) is not None:
                out[k] = r[k]
    return out


@app.route("/sme/data-entry", methods=["GET", "POST"])
@login_required
@role_required("sme")
def sme_data_entry():
    uid = session["user"]["uid"]
    if request.method == "POST":
        income = float(request.form.get("income", 0) or 0)
        expenses = float(request.form.get("expenses", 0) or 0)
        balance = {k: _opt_float(k) for k in BALANCE_FIELDS}
        has_balance = any(v is not None for v in balance.values())

        if income == 0 and expenses == 0 and not has_balance:
            flash("Please enter at least an income, expense or balance amount before saving.", "warning")
            return render_template("sme/data_entry.html", user=session["user"])

        record = {
            "income": income,
            "expenses": expenses,
            "source": request.form.get("source", ""),
            "category": request.form.get("category", ""),
            "expense_name": request.form.get("expense_name", ""),
            "expense_category": request.form.get("expense_category", ""),
            "period": request.form.get("period", ""),
            "status": "saved",
            "source_document_url": request.form.get("source_document_url", "") or None,
            **balance,
        }
        db.collection("users").document(uid).collection("financialRecords").add(record)

        flash("Financial data saved successfully.", "success")
        return redirect(url_for("sme_summary"))
    return render_template("sme/data_entry.html", user=session["user"],
                           prefill=None, source_document_url=None)

@app.route("/sme/upload-document", methods=["POST"])
@login_required
@role_required("sme")
def sme_upload_document():
    from document_parser import allowed_file, parse_document, MAX_FILE_SIZE_BYTES
    from werkzeug.utils import secure_filename
    import tempfile

    uid = session["user"]["uid"]
    file = request.files.get("document")
    doc_type_hint = request.form.get("doc_type_hint", "")

    if not doc_type_hint:
        flash("Please select what kind of document you're uploading.", "warning")
        return redirect(url_for("sme_data_entry"))

    if not file or file.filename == "":
        flash("Please choose a file to upload.", "warning")
        return redirect(url_for("sme_data_entry"))

    if not allowed_file(file.filename):
        flash("Unsupported file type. Please upload a PDF, JPG, or PNG.", "danger")
        return redirect(url_for("sme_data_entry"))

    file.seek(0, os.SEEK_END)
    size = file.tell()
    file.seek(0)
    if size > MAX_FILE_SIZE_BYTES:
        flash("File is too large. Maximum size is 10MB.", "danger")
        return redirect(url_for("sme_data_entry"))

    filename = secure_filename(file.filename)
    ext = filename.rsplit(".", 1)[1].lower()

    with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tmp:
        file.save(tmp.name)
        tmp_path = tmp.name

    try:
        parsed = parse_document(tmp_path, doc_type_hint=doc_type_hint)

        source_document_url = None
        try:
            storage_path = f"users/{uid}/documents/{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{filename}"
            blob = bucket.blob(storage_path)
            blob.upload_from_filename(tmp_path)
            source_document_url = storage_path
        except Exception:
            pass

    finally:
        os.remove(tmp_path)

    flash("We've pre-filled what we could find below — please review and correct "
          "before saving.", "info")
    return render_template("sme/data_entry.html", user=session["user"],
                           prefill=parsed, source_document_url=source_document_url)

@app.route("/sme/summary")
@login_required
@role_required("sme")
def sme_summary():
    from calculations import compute_summary
    uid              = session["user"]["uid"]
    records          = [r.to_dict() for r in
                        db.collection("users").document(uid)
                          .collection("financialRecords").stream()]
    total_invoices   = _get_outstanding_invoices_total(uid)
    overdue_invoices = _get_overdue_invoices(uid)
    overdue_total    = sum(i["amount"] for i in overdue_invoices)
    summary = compute_summary(records, total_invoices=total_invoices,balance_inputs=_get_balance_inputs(records))
    return render_template("sme/summary.html", user=session["user"],
                           summary=summary,
                           overdue_invoices=overdue_invoices,
                           overdue_total=overdue_total)

@app.route("/sme/report")
@login_required
@role_required("sme")
def sme_report():
    from calculations import compute_summary
    from datetime import datetime
    uid     = session["user"]["uid"]
    records = [r.to_dict() for r in
               db.collection("users").document(uid)
                 .collection("financialRecords").stream()]
    total_invoices = _get_outstanding_invoices_total(uid)
    summary = compute_summary(records, total_invoices=total_invoices,
                              balance_inputs=_get_balance_inputs(records))
    return render_template("sme/report.html", user=session["user"],
                           summary=summary, now=datetime.today().strftime("%d %B %Y"))


@app.route("/sme/profit-loss")
@login_required
@role_required("sme")
def sme_profit_loss():
    from calculations import compute_profit_loss
    uid     = session["user"]["uid"]
    records = [r.to_dict() for r in
               db.collection("users").document(uid)
                 .collection("financialRecords").stream()]
    period_start = request.args.get("start", "").strip()
    period_end   = request.args.get("end", "").strip()
    pl = compute_profit_loss(records, period_start or None, period_end or None)
    return render_template("sme/profit_loss.html", user=session["user"], pl=pl)


@app.route("/sme/invoices")
@login_required
@role_required("sme")
def sme_invoices():
    uid = session["user"]["uid"]
    invoices = [i.to_dict() | {"id": i.id} for i in
                db.collection("users").document(uid)
                  .collection("invoices").order_by("created_at", direction=firestore.Query.DESCENDING)
                  .stream()]
    return render_template("sme/invoices.html", user=session["user"], invoices=invoices)


@app.route("/sme/invoices/create", methods=["POST"])
@login_required
@role_required("sme")
def sme_invoice_create():
    from invoice_generator import generate_invoice_pdf
    from email_sender import send_invoice_email, EmailNotConfigured
    import tempfile

    uid = session["user"]["uid"]

    # ── Thread Safety Lock ──────────────────────────────────────────────────
    # Attempts to acquire the global lock without waiting. If another request
    # (from a double/triple click) is already executing, this returns False immediately.
    acquired = invoice_lock.acquire(blocking=False)
    if not acquired:
        flash("Invoice creation is already processing. Please wait a moment.", "warning")
        return redirect(url_for("sme_invoices"))

    try:
        # ── Session Time Check ──────────────────────────────────────────────
        now_ts = time.time()
        last_submit = session.get("last_invoice_submit_time", 0)

        if now_ts - last_submit < 10:
            flash("Invoice creation is already in progress. Duplicate request ignored.", "warning")
            return redirect(url_for("sme_invoices"))

        session["last_invoice_submit_time"] = now_ts

        client_name = request.form.get("client_name", "").strip()
        client_email = request.form.get("client_email", "").strip().lower()
        due_date = request.form.get("due_date", "").strip()
        notes = request.form.get("notes", "").strip()

        descriptions = request.form.getlist("item_description")
        qtys = request.form.getlist("item_qty")
        prices = request.form.getlist("item_unit_price")

        if not client_name or not client_email:
            session.pop("last_invoice_submit_time", None)
            flash("Client name and email are required.", "danger")
            return redirect(url_for("sme_invoices"))

        line_items = []
        for desc, qty, price in zip(descriptions, qtys, prices):
            desc = desc.strip()
            if not desc:
                continue
            try:
                qty_f, price_f = float(qty or 0), float(price or 0)
            except ValueError:
                continue
            if qty_f <= 0 or price_f < 0:
                continue
            line_items.append({"description": desc, "qty": qty_f, "unit_price": price_f})

        if not line_items:
            session.pop("last_invoice_submit_time", None)
            flash("Please add at least one valid line item.", "danger")
            return redirect(url_for("sme_invoices"))

        total = round(sum(i["qty"] * i["unit_price"] for i in line_items), 2)
        issue_date = datetime.utcnow().strftime("%Y-%m-%d")

        # ── Database Deduplication Check ────────────────────────────────────
        existing_invoices = list(db.collection("users").document(uid).collection("invoices").stream())
        recent_cutoff = datetime.utcnow() - timedelta(seconds=60)

        for inv in existing_invoices:
            data = inv.to_dict()
            created_at = data.get("created_at")
            if created_at and hasattr(created_at, "tzinfo") and created_at.tzinfo is not None:
                created_at = created_at.replace(tzinfo=None)

            if (data.get("client_email") == client_email and
                    data.get("due_date") == due_date and
                    data.get("total") == total and
                    created_at and created_at >= recent_cutoff):
                flash(f"An identical invoice for {client_name} was just created. Duplicate request blocked.", "info")
                return redirect(url_for("sme_invoices"))

        invoice_no = f"INV-{uid[:4].upper()}-{len(existing_invoices) + 1:04d}"

        invoice_record = {
            "invoice_no": invoice_no,
            "client_name": client_name,
            "client_email": client_email,
            "issue_date": issue_date,
            "due_date": due_date,
            "line_items": line_items,
            "total": total,
            "amount": total,
            "notes": notes,
            "status": "unpaid",
            "created_at": datetime.utcnow(),
            "email_sent": False,
        }

        issuer = {
            "name": session["user"].get("name", ""),
            "company": session["user"].get("company", ""),
            "email": session["user"].get("email", ""),
            "company_reg": session["user"].get("company_reg", ""),
            "company_address": session["user"].get("company_address", ""),
        }

        # ── Write Record & Send Email ─────────────────────────────────────────
        _, invoice_ref = db.collection("users").document(uid).collection("invoices").add(invoice_record)

        payment_url = None
        try:
            from billplz_gateway import create_bill, BillplzNotConfigured
            bill = create_bill(
                amount=total,
                email=client_email,
                name=client_name,
                description=f"Invoice {invoice_no}",
                callback_url=(os.getenv("PUBLIC_BASE_URL", "").rstrip("/") + "/webhooks/billplz")
                if os.getenv("PUBLIC_BASE_URL")
                else url_for("billplz_webhook", _external=True),
            )
            payment_url = bill["url"]
            db.collection("billplzBills").document(bill["id"]).set({
                "data_uid": uid, "invoice_id": invoice_ref.id,
            })
            invoice_ref.update({"billplz_bill_id": bill["id"], "payment_url": payment_url})
            invoice_record["payment_url"] = payment_url
        except BillplzNotConfigured:
            pass
        except Exception as e:
            flash(f"Invoice {invoice_no} saved, but the payment link couldn't be created: {e}", "warning")

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            pdf_path = tmp.name
        try:
            generate_invoice_pdf(invoice_record, pdf_path, issuer)

            try:
                send_invoice_email(client_email, client_name, invoice_no, total, due_date,
                                   pdf_path, from_email=issuer["email"], from_name=issuer["name"],
                                   payment_url=payment_url)
                invoice_ref.update({"email_sent": True})
                flash(f"Invoice {invoice_no} created and emailed to {client_email}.", "success")
            except EmailNotConfigured:
                flash(f"Invoice {invoice_no} created and saved, but email wasn't sent — "
                      f"SendGrid isn't configured yet.", "warning")
            except Exception as e:
                flash(f"Invoice {invoice_no} created and saved, but sending the email failed: {e}", "warning")

        finally:
            if os.path.exists(pdf_path):
                os.remove(pdf_path)

    finally:
        # Always release the lock when the first request completes or fails
        invoice_lock.release()

    return redirect(url_for("sme_invoices"))

@app.route("/sme/invoices/<invoice_id>/pdf")
@login_required
@role_required("sme")
def sme_invoice_pdf(invoice_id):
    from invoice_generator import generate_invoice_pdf
    from flask import send_file
    import tempfile

    uid = session["user"]["uid"]
    doc = db.collection("users").document(uid).collection("invoices").document(invoice_id).get()
    if not doc.exists:
        flash("Invoice not found.", "danger")
        return redirect(url_for("sme_invoices"))

    invoice_record = doc.to_dict()
    issuer = {
        "name":            session["user"].get("name", ""),
        "company":         session["user"].get("company", ""),
        "email":           session["user"].get("email", ""),
        "company_reg":     session["user"].get("company_reg", ""),
        "company_address": session["user"].get("company_address", ""),
    }

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        pdf_path = tmp.name
    generate_invoice_pdf(invoice_record, pdf_path, issuer)
    return send_file(pdf_path, as_attachment=True,
                     download_name=f"{invoice_record.get('invoice_no', 'invoice')}.pdf")


@csrf.exempt   # Billplz posts this server-to-server, with no CSRF token
@app.route("/webhooks/billplz", methods=["POST"])
def billplz_webhook():
    from billplz_gateway import verify_callback_signature, get_bill, BillplzNotConfigured

    form_data = request.form.to_dict()
    bill_id   = form_data.get("id")
    if not bill_id:
        return "Missing bill id", 400

    try:
        sig_ok = verify_callback_signature(form_data)
        if not sig_ok:
            print(f"Billplz webhook: signature did not match for bill {bill_id} "
                  f"-- proceeding anyway based on direct API confirmation.")
    except BillplzNotConfigured:
        pass

    # Authoritative check: ask Billplz directly whether this bill is
    # actually paid, rather than trusting the callback body alone.
    try:
        bill = get_bill(bill_id)
    except Exception as e:
        print(f"Billplz webhook: couldn't verify bill {bill_id} via API: {e}")
        return "Verification failed", 502

    if not bill.get("paid"):
        return "OK", 200   # not actually paid according to Billplz itself

    lookup = db.collection("billplzBills").document(bill_id).get()
    if not lookup.exists:
        return "Unknown bill", 404

    ref = lookup.to_dict()
    db.collection("users").document(ref["data_uid"])\
      .collection("invoices").document(ref["invoice_id"])\
      .update({"status": "paid", "paid_via": "billplz", "billplz_bill_id": bill_id})

    return "OK", 200


# ── CFO routes ────────────────────────────────────────────────────────────────
@app.route("/cfo/dashboard")
@login_required
@role_required("cfo")
def cfo_dashboard():
    from calculations import compute_summary, compute_monthly_series
    uid          = session["user"]["uid"]
    data_uid     = _get_data_uid(uid)
    records      = [r.to_dict() for r in
                    db.collection("users").document(data_uid)
                      .collection("financialRecords").stream()]
    invoices     = _get_overdue_invoices(data_uid)
    summary = compute_summary(records,
                              total_invoices=_get_outstanding_invoices_total(data_uid),
                              balance_inputs=_get_balance_inputs(records))
    monthly      = compute_monthly_series(records)
    linked_co    = session["user"].get("linked_sme_company", "")
    return render_template("cfo/dashboard.html", user=session["user"],
                           summary=summary, invoices=invoices,
                           monthly=monthly, linked_company=linked_co)

@app.route("/cfo/projection")
@login_required
@role_required("cfo")
def cfo_projection():
    return render_template("cfo/projection.html", user=session["user"])

@app.route("/cfo/profit-loss")
@login_required
@role_required("cfo")
def cfo_profit_loss():
    from calculations import compute_profit_loss
    uid      = session["user"]["uid"]
    data_uid = _get_data_uid(uid)
    records  = [r.to_dict() for r in
                db.collection("users").document(data_uid)
                  .collection("financialRecords").stream()]
    period_start = request.args.get("start", "").strip()
    period_end   = request.args.get("end", "").strip()
    pl = compute_profit_loss(records, period_start or None, period_end or None)
    return render_template("cfo/profit_loss.html", user=session["user"], pl=pl)

@app.route("/cfo/invoices")
@login_required
@role_required("cfo")
def cfo_invoices():
    from datetime import date, datetime
    uid = session["user"]["uid"]
    data_uid = _get_data_uid(uid)
    from reminder_engine import annotate_invoices

    today = date.today()
    all_docs = db.collection("users").document(data_uid) \
        .collection("invoices") \
        .order_by("created_at", direction=firestore.Query.DESCENDING) \
        .stream()

    all_invoices = []
    overdue_invoices = []

    for d in all_docs:
        inv = d.to_dict() | {"id": d.id}
        inv["amount"] = float(inv.get("total") or inv.get("amount") or 0)

        # Calculate days overdue
        due_raw = inv.get("due_date", "")
        days_overdue = 0
        for fmt in ("%Y-%m-%d", "%Y-%m"):
            try:
                due_date = datetime.strptime(due_raw, fmt).date()
                days_overdue = max((today - due_date).days, 0)
                break
            except (ValueError, TypeError):
                continue
        inv["days_overdue"] = days_overdue

        # Standardize status
        status = inv.get("status", "unpaid")
        if status != "paid" and days_overdue > 0:
            inv["status"] = "overdue"

        all_invoices.append(inv)
        if inv["status"] == "overdue":
            overdue_invoices.append(inv)

    # Annotate overdue invoices with reminder statuses/stages
    overdue_annotated = annotate_invoices(db, uid, overdue_invoices)

    return render_template("cfo/invoices.html",
                           user=session["user"],
                           invoices=overdue_annotated,
                           all_invoices=all_invoices)

@app.route("/cfo/reminders")
@login_required
@role_required("cfo")
def cfo_reminders():
    from reminder_engine import ensure_default_rules, build_queue
    uid      = session["user"]["uid"]
    data_uid = _get_data_uid(uid)
    ensure_default_rules(db, uid)   # first visit: seed the 4 defaults as real, editable rows
    rules = [r.to_dict() | {"id": r.id} for r in
             db.collection("users").document(uid)
               .collection("reminderRules").stream()]
    rules = sorted(rules, key=lambda r: r.get("days", 0))
    logs  = [r.to_dict() for r in
             db.collection("users").document(uid)
               .collection("reminderLogs")
               .order_by("sent_at", direction=firestore.Query.DESCENDING)
               .limit(20).stream()]

    queue = build_queue(db, uid, _get_overdue_invoices(data_uid))
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    stats = {
        "ready":   sum(1 for i in queue if i["ready"]),
        "waiting": sum(1 for i in queue if not i["ready"]),
        "overdue": sum(i["amount"] for i in queue),
        "sent_7d": sum(1 for l in logs if l.get("sent_at", "") >= week_ago),
    }
    return render_template("cfo/reminders.html", user=session["user"],
                           rules=rules, logs=logs, queue=queue, stats=stats)

@app.route("/cfo/run-reminders", methods=["POST"])
@login_required
@role_required("cfo")
def cfo_run_reminders():
    from reminder_engine import run_reminder_check
    uid      = session["user"]["uid"]
    data_uid = _get_data_uid(uid)
    result   = run_reminder_check(db, cfo_uid=uid, data_uid=data_uid,
                                  issuer=session["user"])
    if result["sent"]:
        flash(f"Sent {result['sent']} reminder(s). "
              f"{result['skipped']} invoice(s) didn't need one yet.", "success")
    else:
        flash(f"No reminders needed right now. "
              f"{result['skipped']} overdue invoice(s) checked.", "info")
    for err in result["errors"]:
        flash(err, "warning")
    return redirect(url_for("cfo_reminders"))

@app.route("/cfo/mark-paid/<invoice_id>", methods=["POST"])
@login_required
@role_required("cfo")
def cfo_mark_paid(invoice_id):
    uid      = session["user"]["uid"]
    data_uid = _get_data_uid(uid)
    db.collection("users").document(data_uid)\
      .collection("invoices").document(invoice_id)\
      .update({"status": "paid"})
    flash("Invoice marked as paid.", "success")
    return redirect(_safe_next("cfo_invoices"))

@app.route("/cfo/delete-rule/<rule_id>", methods=["POST"])
@login_required
@role_required("cfo")
def cfo_delete_rule(rule_id):
    uid = session["user"]["uid"]
    db.collection("users").document(uid)\
      .collection("reminderRules").document(rule_id).delete()
    flash("Reminder rule deleted.", "info")
    return redirect(url_for("cfo_reminders"))


# ── Admin routes ──────────────────────────────────────────────────────────────
@app.route("/admin/accounts")
@login_required
@role_required("admin")
def admin_accounts():
    users     = [u.to_dict() | {"uid": u.id} for u in
                 db.collection("users").where("role", "in", ["sme", "cfo"]).stream()]
    total     = len(users)
    active    = sum(1 for u in users if u.get("status") == "active")
    suspended = sum(1 for u in users if u.get("status") == "suspended")
    return render_template("admin/accounts.html", user=session["user"],
                           users=users, total=total,
                           active=active, suspended=suspended)

@app.route("/admin/suspend/<uid>", methods=["POST"])
@login_required
@role_required("admin")
def admin_suspend(uid):
    if uid == session["user"]["uid"]:
        flash("You cannot suspend your own account.", "danger")
        return redirect(url_for("admin_accounts"))
    db.collection("users").document(uid).update({"status": "suspended"})
    auth.update_user(uid, disabled=True)
    auth.revoke_refresh_tokens(uid)  # kill active sessions immediately
    flash("Account suspended.", "warning")
    return redirect(url_for("admin_accounts"))

@app.route("/admin/restore/<uid>", methods=["POST"])
@login_required
@role_required("admin")
def admin_restore(uid):
    db.collection("users").document(uid).update({"status": "active"})
    auth.update_user(uid, disabled=False)
    flash("Account restored.", "success")
    return redirect(url_for("admin_accounts"))

@app.route("/admin/delete/<uid>", methods=["POST"])
@login_required
@role_required("admin")
def admin_delete(uid):
    if uid == session["user"]["uid"]:
        flash("You cannot delete your own account.", "danger")
        return redirect(url_for("admin_accounts"))
    db.collection("users").document(uid).delete()
    auth.delete_user(uid)
    flash("Account deleted.", "danger")
    return redirect(url_for("admin_accounts"))

@app.route("/admin/add-account", methods=["GET", "POST"])
@login_required
@role_required("admin")
def admin_add_account():
    sme_accounts = [u.to_dict() | {"uid": u.id} for u in
                    db.collection("users").where("role", "==", "sme").stream()]
    if request.method == "POST":
        email            = request.form.get("email", "").strip()
        password         = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        name             = request.form.get("name", "").strip()
        company          = request.form.get("company", "").strip()
        company_reg      = request.form.get("company_reg", "").strip()
        company_address  = request.form.get("company_address", "").strip()
        role             = request.form.get("role", "sme")
        linked_sme_uid   = request.form.get("linked_sme_uid", "")
        registered       = request.form.get("registered", "")

        if password != confirm_password:
            flash("Passwords do not match.", "danger")
            return render_template("admin/add_account.html",
                                   user=session["user"], sme_accounts=sme_accounts)
        missing = validate_password(password)
        if missing:
            flash("Password must contain " + ", ".join(missing) + ".", "danger")
            return render_template("admin/add_account.html",
                                   user=session["user"], sme_accounts=sme_accounts)
        try:
            new_user  = auth.create_user(email=email, password=password,
                                         display_name=name)
            user_data = {
                "name":        name,
                "email":       email,
                "company":     company,
                "company_reg": company_reg,
                "company_address": company_address,
                "role":        role,
                "status":      "active",
                "registered":  registered,
            }
            if role == "cfo" and linked_sme_uid:
                sme_doc = db.collection("users").document(linked_sme_uid).get()
                if sme_doc.exists:
                    user_data["linked_sme_uid"]     = linked_sme_uid
                    user_data["linked_sme_company"] = sme_doc.to_dict().get("company", "")
            db.collection("users").document(new_user.uid).set(user_data)
            flash(f"Account created for {email}.", "success")
            return redirect(url_for("admin_accounts"))
        except Exception as e:
            flash(f"Error creating account: {str(e)}", "danger")
            return render_template("admin/add_account.html",
                                   user=session["user"], sme_accounts=sme_accounts)
    return render_template("admin/add_account.html",
                           user=session["user"], sme_accounts=sme_accounts)

@app.route("/admin/analytics")
@login_required
@role_required("admin")
def admin_analytics():
    users     = [u.to_dict() for u in
                 db.collection("users").where("role", "in", ["sme", "cfo"]).stream()]
    total     = len(users)
    active    = sum(1 for u in users if u.get("status") == "active")
    suspended = sum(1 for u in users if u.get("status") == "suspended")
    sme_count = sum(1 for u in users if u.get("role") == "sme")
    cfo_count = sum(1 for u in users if u.get("role") == "cfo")
    return render_template("admin/analytics.html", user=session["user"],
                           total=total, active=active, suspended=suspended,
                           sme_count=sme_count, cfo_count=cfo_count)


# ── API routes ────────────────────────────────────────────────────────────────
@app.route("/api/forecast")
@login_required
@role_required("cfo")
def api_forecast():
    from forecasting import get_forecast
    uid      = session["user"]["uid"]
    data_uid = _get_data_uid(uid)
    records  = [r.to_dict() for r in
                db.collection("users").document(data_uid)
                  .collection("financialRecords").stream()]
    horizon  = int(request.args.get("days", 30))
    invoices = _get_receivables(data_uid)   # unpaid invoices, for the known-receivables panel
    return get_forecast(records, horizon, invoices)

@csrf.exempt   # called by JS fetch() — CSRF token sent via X-CSRFToken header
@app.route("/api/save-reminder-rule", methods=["POST"])
@login_required
@role_required("cfo")
def api_save_reminder_rule():
    uid  = session["user"]["uid"]
    data = request.get_json()
    db.collection("users").document(uid).collection("reminderRules").add({
        "days":     int(data.get("days", 7)),
        "stage":    data.get("stage", "Follow-up"),
        "template": data.get("template", ""),
    })
    return jsonify({"status": "ok"})


@app.route("/cfo/send-reminder/<invoice_id>", methods=["POST"])
@login_required
@role_required("cfo")
def cfo_send_reminder(invoice_id):
    from reminder_engine import send_reminder_now
    uid      = session["user"]["uid"]
    data_uid = _get_data_uid(uid)
    ok, msg  = send_reminder_now(db, cfo_uid=uid, data_uid=data_uid,
                                 issuer=session["user"], invoice_id=invoice_id,
                                 receivables=_get_receivables(data_uid))
    flash(msg, "success" if ok else "warning")
    return redirect(_safe_next("cfo_invoices"))


def run_all_reminder_checks():
    """Daily job: run the reminder check for every active CFO."""
    from reminder_engine import run_reminder_check
    try:
        for cfo_doc in db.collection("users").where("role", "==", "cfo").stream():
            issuer = _load_user(cfo_doc.id)
            if not issuer:                      # suspended or deleted
                continue
            data_uid = issuer.get("linked_sme_uid") or cfo_doc.id
            result = run_reminder_check(db, cfo_uid=cfo_doc.id, data_uid=data_uid,
                                        issuer=issuer,
                                        receivables=_get_receivables(data_uid))
            print(f"[reminders] {issuer['email']}: sent={result['sent']} "
                  f"skipped={result['skipped']} errors={result['errors']}")
    except Exception as e:
        print(f"[reminders] scheduled run failed: {e}")


def start_reminder_scheduler():
    from apscheduler.schedulers.background import BackgroundScheduler
    scheduler = BackgroundScheduler(timezone="Asia/Kuala_Lumpur")
    scheduler.add_job(run_all_reminder_checks, "cron", hour=9, minute=0,
                      id="daily_reminders", replace_existing=True)
    scheduler.start()


if __name__ == "__main__":
    debug = not IS_PRODUCTION
    if not debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        start_reminder_scheduler()
    app.run(debug=debug)