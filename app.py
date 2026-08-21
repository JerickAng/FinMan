from flask import Flask, render_template, request, redirect, url_for, session, flash
import firebase_admin
from firebase_admin import credentials, auth, firestore
from functools import wraps
from dotenv import load_dotenv
from datetime import timedelta
from flask_wtf.csrf import CSRFProtect
import os

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "finman-secret-change-this")

# Protects every POST/PUT/DELETE route against CSRF by requiring a per-session
# token. Templates need a hidden `csrf_token()` field in every <form>, and any
# JS fetch()/XHR POST needs the token sent as the X-CSRFToken header.
csrf = CSRFProtect(app)

# ── Cookie / session hardening ────────────────────────────────────────────────
IS_PRODUCTION = os.getenv("FLASK_ENV") == "production"
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=IS_PRODUCTION,   # requires HTTPS in production
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
)

# Name of the cookie that holds the signed, revocable Firebase session token.
# This is what actually proves identity on every request -- the Flask
# `session` dict is just a same-request cache derived from it, never the
# source of truth.
SESSION_COOKIE_NAME = "finman_session"
SESSION_EXPIRY = timedelta(days=5)

# ── Firebase Admin SDK init ───────────────────────────────────────────────────
# Replace "serviceAccountKey.json" with your downloaded Firebase service account file
cred = credentials.Certificate("ServiceAccountKey.json")
firebase_admin.initialize_app(cred)
db = firestore.client()


def _load_user(uid):
    """
    Always re-reads role/status fresh from Firestore for the given uid.
    Never trust a role or uid that came from the client (cookie, form,
    query string, etc.) -- the uid here comes only from a Firebase-verified
    token, and everything else is re-derived from the database on the spot.
    """
    doc = db.collection("users").document(uid).get()
    if not doc.exists:
        return None
    data = doc.to_dict()
    if data.get("status") == "suspended":
        return None
    return {
        "uid":     uid,
        "email":   data.get("email", ""),
        "role":    data.get("role", "sme"),
        "name":    data.get("name", data.get("email", "")),
        "company": data.get("company", ""),
    }


def _clear_session_cookie(resp):
    resp.delete_cookie(SESSION_COOKIE_NAME)
    return resp


# ── Role-based access decorators ─────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        cookie = request.cookies.get(SESSION_COOKIE_NAME)
        if not cookie:
            flash("Please log in to continue.", "warning")
            return redirect(url_for("login"))
        try:
            # check_revoked=True re-checks with Firebase on every request --
            # this is what makes admin suspend/delete take effect immediately
            # instead of waiting for the cookie to expire.
            decoded = auth.verify_session_cookie(cookie, check_revoked=True)
        except (auth.InvalidSessionCookieError, auth.RevokedSessionCookieError, ValueError):
            flash("Your session has expired. Please log in again.", "warning")
            return _clear_session_cookie(redirect(url_for("login")))

        user = _load_user(decoded["uid"])
        if user is None:
            flash("Account not found or suspended.", "danger")
            return _clear_session_cookie(redirect(url_for("login")))

        # Rebuilt from verified data on every single request -- nothing here
        # can be influenced by the client, so there's no way to reach another
        # user's data by editing a cookie, a URL, or a form field.
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
    if "user" in session:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.cookies.get(SESSION_COOKIE_NAME):
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        id_token = request.form.get("id_token")
        try:
            # check_revoked catches tokens for users who were just disabled/deleted.
            decoded = auth.verify_id_token(id_token, check_revoked=True)
            uid = decoded["uid"]

            user = _load_user(uid)
            if user is None:
                flash("Account not found or suspended. Contact your administrator.", "danger")
                return redirect(url_for("login"))

            # Firebase requires the ID token to be < 5 minutes old to mint a
            # session cookie -- this is a good thing, it means a stolen/stale
            # token can't be replayed later to establish a long-lived session.
            session_cookie = auth.create_session_cookie(id_token, expires_in=SESSION_EXPIRY)

            resp = redirect(url_for("dashboard"))
            resp.set_cookie(
                SESSION_COOKIE_NAME,
                session_cookie,
                max_age=int(SESSION_EXPIRY.total_seconds()),
                httponly=True,
                secure=IS_PRODUCTION,
                samesite="Lax",
            )
            return resp

        except Exception:
            # Deliberately generic -- don't leak whether the email exists,
            # the token was malformed, expired, etc.
            flash("Login failed. Please check your credentials and try again.", "danger")
            return redirect(url_for("login"))

    return render_template("login.html")

@app.route("/logout")
def logout():
    # NOTE: deliberately does NOT call auth.revoke_refresh_tokens() here.
    # That call invalidates the account's Firebase credential everywhere
    # (all devices/tabs), which is the right move for a forced admin
    # suspend/delete, but wrong for a routine logout -- it stomps on the
    # client-side Firebase SDK's own cached session and causes a stale,
    # revoked refresh token to linger in the browser (IndexedDB), which
    # then surfaces as auth/user-token-expired the next time the login
    # page loads and Firebase tries to use that cached session.
    session.clear()
    flash("You have been logged out.", "info")
    return _clear_session_cookie(redirect(url_for("login")))

# ── Dashboard router (redirects by role) ─────────────────────────────────────
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
    records = db.collection("users").document(uid).collection("financialRecords").stream()
    data    = [r.to_dict() for r in records]
    return render_template("sme/dashboard.html", user=session["user"], records=data)

@app.route("/sme/data-entry", methods=["GET", "POST"])
@login_required
@role_required("sme")
def sme_data_entry():
    uid = session["user"]["uid"]
    if request.method == "POST":
        record = {
            "income":   float(request.form.get("income",   0) or 0),
            "expenses": float(request.form.get("expenses", 0) or 0),
            "invoices": float(request.form.get("invoices", 0) or 0),
            "source":   request.form.get("source",   ""),
            "category": request.form.get("category", ""),
            "period":   request.form.get("period",   ""),
            "status":   "saved",
        }
        db.collection("users").document(uid).collection("financialRecords").add(record)
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
    uid     = session["user"]["uid"]
    records = [r.to_dict() for r in
               db.collection("users").document(uid)
                 .collection("financialRecords").stream()]
    summary = compute_summary(records)
    return render_template("sme/report.html", user=session["user"], summary=summary)

# ── CFO routes ────────────────────────────────────────────────────────────────
@app.route("/cfo/dashboard")
@login_required
@role_required("cfo")
def cfo_dashboard():
    from calculations import compute_summary
    uid     = session["user"]["uid"]
    records = [r.to_dict() for r in
               db.collection("users").document(uid)
                 .collection("financialRecords").stream()]
    summary = compute_summary(records)
    return render_template("cfo/dashboard.html", user=session["user"], summary=summary)

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
    invoices = [r.to_dict() | {"id": r.id} for r in
                db.collection("users").document(uid)
                  .collection("invoices").stream()]
    overdue  = [i for i in invoices if i.get("status") == "overdue"]
    return render_template("cfo/invoices.html", user=session["user"], invoices=overdue)

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
               .collection("reminderLogs").order_by("sent_at", direction=firestore.Query.DESCENDING).limit(20).stream()]
    return render_template("cfo/reminders.html", user=session["user"], rules=rules, logs=logs)

# ── Admin routes ──────────────────────────────────────────────────────────────
@app.route("/admin/accounts")
@login_required
@role_required("admin")
def admin_accounts():
    users = [u.to_dict() | {"uid": u.id} for u in
             db.collection("users").where("role", "in", ["sme", "cfo"]).stream()]
    total     = len(users)
    active    = sum(1 for u in users if u.get("status") == "active")
    suspended = sum(1 for u in users if u.get("status") == "suspended")
    return render_template("admin/accounts.html",
                           user=session["user"], users=users,
                           total=total, active=active, suspended=suspended)

@app.route("/admin/suspend/<uid>", methods=["POST"])
@login_required
@role_required("admin")
def admin_suspend(uid):
    if uid == session["user"]["uid"]:
        flash("You can't suspend your own account.", "danger")
        return redirect(url_for("admin_accounts"))
    db.collection("users").document(uid).update({"status": "suspended"})
    auth.update_user(uid, disabled=True)
    # Kill any session that user already has open, right now, rather than
    # relying on it to naturally expire.
    auth.revoke_refresh_tokens(uid)
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
        flash("You can't delete your own account.", "danger")
        return redirect(url_for("admin_accounts"))
    db.collection("users").document(uid).delete()
    auth.delete_user(uid)
    flash("Account deleted.", "danger")
    return redirect(url_for("admin_accounts"))

@app.route("/admin/add-account", methods=["GET", "POST"])
@login_required
@role_required("admin")
def admin_add_account():
    if request.method == "POST":
        email    = request.form.get("email")
        password = request.form.get("password")
        name     = request.form.get("name")
        company  = request.form.get("company")
        role     = request.form.get("role", "sme")
        try:
            new_user = auth.create_user(email=email, password=password, display_name=name)
            db.collection("users").document(new_user.uid).set({
                "name": name, "email": email, "company": company,
                "role": role, "status": "active",
            })
            flash(f"Account created for {email}.", "success")
        except Exception as e:
            flash(f"Error creating account: {str(e)}", "danger")
        return redirect(url_for("admin_accounts"))
    return render_template("admin/add_account.html", user=session["user"])

# ── API routes (called by JS) ─────────────────────────────────────────────────
@app.route("/api/forecast")
@login_required
@role_required("cfo")
def api_forecast():
    from forecasting import get_forecast
    uid     = session["user"]["uid"]
    records = [r.to_dict() for r in
               db.collection("users").document(uid)
                 .collection("financialRecords").stream()]
    horizon = int(request.args.get("days", 30))
    result  = get_forecast(records, horizon)
    return result

@app.route("/api/save-reminder-rule", methods=["POST"])
@login_required
@role_required("cfo")
def api_save_reminder_rule():
    from flask import jsonify
    uid  = session["user"]["uid"]
    data = request.get_json()
    db.collection("users").document(uid).collection("reminderRules").add({
        "days":     int(data.get("days", 7)),
        "stage":    data.get("stage", "Follow-up"),
        "template": data.get("template", ""),
    })
    return jsonify({"status": "ok"})

@app.route("/admin/analytics")
@login_required
@role_required("admin")
def admin_analytics():
    users     = [u.to_dict() for u in
                 db.collection("users").where("role", "in", ["sme","cfo"]).stream()]
    total     = len(users)
    active    = sum(1 for u in users if u.get("status") == "active")
    suspended = sum(1 for u in users if u.get("status") == "suspended")
    sme_count = sum(1 for u in users if u.get("role") == "sme")
    cfo_count = sum(1 for u in users if u.get("role") == "cfo")
    return render_template("admin/analytics.html",
                           user=session["user"],
                           total=total, active=active,
                           suspended=suspended,
                           sme_count=sme_count, cfo_count=cfo_count)

if __name__ == "__main__":
    app.run(debug=not IS_PRODUCTION)