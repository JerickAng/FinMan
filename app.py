from flask import Flask, render_template, request, redirect, url_for, session, flash
import firebase_admin
from firebase_admin import credentials, auth, firestore
from functools import wraps
from dotenv import load_dotenv
import os

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "finman-secret-change-this")

# ── Firebase Admin SDK init ───────────────────────────────────────────────────
# Replace "serviceAccountKey.json" with your downloaded Firebase service account file
cred = credentials.Certificate("serviceAccountKey.json")
firebase_admin.initialize_app(cred)
db = firestore.client()

# ── Role-based access decorators ─────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user" not in session:
            flash("Please log in to continue.", "warning")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

def role_required(*roles):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if "user" not in session:
                return redirect(url_for("login"))
            if session["user"].get("role") not in roles:
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
    if "user" in session:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        id_token = request.form.get("id_token")
        try:
            decoded = auth.verify_id_token(id_token)
            uid     = decoded["uid"]
            email   = decoded.get("email", "")

            # Fetch role from Firestore
            user_doc = db.collection("users").document(uid).get()
            if not user_doc.exists:
                flash("Account not found. Contact your administrator.", "danger")
                return redirect(url_for("login"))

            user_data = user_doc.to_dict()
            role      = user_data.get("role", "sme")

            session["user"] = {
                "uid":     uid,
                "email":   email,
                "role":    role,
                "name":    user_data.get("name", email),
                "company": user_data.get("company", ""),
            }
            return redirect(url_for("dashboard"))

        except Exception as e:
            flash(f"Login failed: {str(e)}", "danger")
            return redirect(url_for("login"))

    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("login"))

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

@app.route("/admin/suspend/<uid>")
@login_required
@role_required("admin")
def admin_suspend(uid):
    db.collection("users").document(uid).update({"status": "suspended"})
    auth.update_user(uid, disabled=True)
    flash("Account suspended.", "warning")
    return redirect(url_for("admin_accounts"))

@app.route("/admin/restore/<uid>")
@login_required
@role_required("admin")
def admin_restore(uid):
    db.collection("users").document(uid).update({"status": "active"})
    auth.update_user(uid, disabled=False)
    flash("Account restored.", "success")
    return redirect(url_for("admin_accounts"))

@app.route("/admin/delete/<uid>")
@login_required
@role_required("admin")
def admin_delete(uid):
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

if __name__ == "__main__":
    app.run(debug=True)
