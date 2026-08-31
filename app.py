from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
import firebase_admin
from firebase_admin import credentials, auth, firestore
from functools import wraps
from dotenv import load_dotenv
from datetime import timedelta
from flask_wtf.csrf import CSRFProtect
import os
import re

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "finman-secret-change-this")

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
SESSION_EXPIRY      = timedelta(days=5)

# ── Firebase Admin SDK ────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
cred     = credentials.Certificate(os.path.join(BASE_DIR, "serviceAccountKey.json"))
if not firebase_admin._apps:
    firebase_admin.initialize_app(cred)
db = firestore.client()


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
        "linked_sme_uid":  data.get("linked_sme_uid", ""),
        "linked_sme_company": data.get("linked_sme_company", ""),
    }

def _clear_session_cookie(resp):
    resp.delete_cookie(SESSION_COOKIE_NAME)
    return resp

# ── Password policy ───────────────────────────────────────────────────────────
# Minimum 8 characters, at least one uppercase, one lowercase, one special
# character. Enforced here (server-side) since this is the only place that
# actually matters -- any client-side check is just a UX nicety on top.
_PASSWORD_RULES = [
    (lambda p: len(p) >= 8,                         "at least 8 characters"),
    (lambda p: re.search(r"[A-Z]", p) is not None,  "one uppercase letter"),
    (lambda p: re.search(r"[a-z]", p) is not None,  "one lowercase letter"),
    (lambda p: re.search(r"[^A-Za-z0-9]", p) is not None, "one special character"),
]

def validate_password(password):
    """Returns a list of unmet requirements (empty list = password is valid)."""
    return [msg for check, msg in _PASSWORD_RULES if not check(password)]


def _get_data_uid(uid):
    """Returns the UID whose Firestore data the current user should read.
       For CFOs this is the linked SME; for everyone else it's their own."""
    user_doc = db.collection("users").document(uid).get()
    if user_doc.exists:
        linked = user_doc.to_dict().get("linked_sme_uid", "")
        if linked:
            return linked
    return uid


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
        user = _load_user(decoded["uid"])
        if user is None:
            flash("Account not found or suspended.", "danger")
            return _clear_session_cookie(redirect(url_for("login")))
        session["user"] = user
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

@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return _clear_session_cookie(redirect(url_for("login")))

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
    return render_template("sme/dashboard.html", user=session["user"], records=records)

@app.route("/sme/data-entry", methods=["GET", "POST"])
@login_required
@role_required("sme")
def sme_data_entry():
    uid = session["user"]["uid"]
    if request.method == "POST":
        record = {
            "income":        float(request.form.get("income",   0) or 0),
            "expenses":      float(request.form.get("expenses", 0) or 0),
            "invoices":      float(request.form.get("invoices", 0) or 0),
            "source":        request.form.get("source",        ""),
            "category":      request.form.get("category",      ""),
            "expense_name":  request.form.get("expense_name",  ""),
            "client_name":   request.form.get("client_name",   ""),
            "invoice_status":request.form.get("invoice_status","unpaid"),
            "period":        request.form.get("period",        ""),
            "status":        "saved",
        }
        db.collection("users").document(uid).collection("financialRecords").add(record)

        # Also save invoice as a separate document so CFO can see it
        invoice_status = request.form.get("invoice_status", "unpaid")
        invoice_amount = float(request.form.get("invoices", 0) or 0)
        if invoice_amount > 0:
            db.collection("users").document(uid).collection("invoices").add({
                "client_name":   request.form.get("client_name", ""),
                "amount":        invoice_amount,
                "status":        invoice_status,
                "invoice_no":    f"INV-{uid[:4].upper()}-{len(record['period'].replace('-',''))}",
                "due_date":      request.form.get("period", ""),
                "days_overdue":  0,
            })

        flash("Financial data saved successfully.", "success")
        return redirect(url_for("sme_summary"))
    return render_template("sme/data_entry.html", user=session["user"])

@app.route("/sme/summary")
@login_required
@role_required("sme")
def sme_summary():
    from calculations import compute_summary
    uid     = session["user"]["uid"]
    records = [r.to_dict() for r in
               db.collection("users").document(uid)
                 .collection("financialRecords").stream()]
    summary = compute_summary(records)
    return render_template("sme/summary.html", user=session["user"], summary=summary)

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
    summary = compute_summary(records)
    return render_template("sme/report.html", user=session["user"],
                           summary=summary, now=datetime.today().strftime("%d %B %Y"))


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
    invoices     = [r.to_dict() | {"id": r.id} for r in
                    db.collection("users").document(data_uid)
                      .collection("invoices")
                      .where("status", "==", "overdue").stream()]
    summary      = compute_summary(records)
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

@app.route("/cfo/invoices")
@login_required
@role_required("cfo")
def cfo_invoices():
    uid      = session["user"]["uid"]
    data_uid = _get_data_uid(uid)
    invoices = [r.to_dict() | {"id": r.id} for r in
                db.collection("users").document(data_uid)
                  .collection("invoices")
                  .where("status", "==", "overdue").stream()]
    return render_template("cfo/invoices.html", user=session["user"], invoices=invoices)

@app.route("/cfo/reminders")
@login_required
@role_required("cfo")
def cfo_reminders():
    uid   = session["user"]["uid"]
    rules = [r.to_dict() | {"id": r.id} for r in
             db.collection("users").document(uid)
               .collection("reminderRules").stream()]
    logs  = [r.to_dict() for r in
             db.collection("users").document(uid)
               .collection("reminderLogs")
               .order_by("sent_at", direction=firestore.Query.DESCENDING)
               .limit(20).stream()]
    return render_template("cfo/reminders.html", user=session["user"],
                           rules=rules, logs=logs)

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
    return redirect(url_for("cfo_invoices"))

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
    return get_forecast(records, horizon)

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


if __name__ == "__main__":
    app.run(debug=not IS_PRODUCTION)
