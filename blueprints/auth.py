import hmac

from flask import (Blueprint, current_app, flash, redirect, render_template,
                   request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from db import get_db
from utils import EMAIL_RE, current_user, login_blocked, login_failed, login_ok, now_utc, safe_next, to_iso

bp = Blueprint("auth", __name__)

# Verified against when the email is unknown, so timing doesn't reveal accounts.
_DUMMY_HASH = generate_password_hash("not-a-real-password")


def _home_for(user):
    return url_for("examiner.dashboard" if user["role"] == "examiner" else "student.dashboard")


@bp.route("/register", methods=["GET", "POST"])
def register():
    if current_user():
        return redirect(_home_for(current_user()))
    invite_required = bool(current_app.config["EXAMINER_INVITE_CODE"])
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        role = request.form.get("role", "student")
        invite = request.form.get("invite_code", "").strip()
        errors = []
        if not 2 <= len(name) <= 80:
            errors.append("Please enter your full name (2-80 characters).")
        if not EMAIL_RE.match(email) or len(email) > 120:
            errors.append("Please enter a valid email address.")
        if len(password) < 8:
            errors.append("Password must be at least 8 characters.")
        if password != confirm:
            errors.append("Passwords do not match.")
        if role not in ("student", "examiner"):
            errors.append("Choose a valid role.")
        if role == "examiner" and invite_required and not hmac.compare_digest(
                invite, current_app.config["EXAMINER_INVITE_CODE"]):
            errors.append("A valid examiner invite code is required.")
        db = get_db()
        if not errors and db.one("SELECT id FROM users WHERE email=?", (email,)):
            errors.append("An account with this email already exists.")
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("auth/register.html", invite_required=invite_required,
                                   form=request.form), 400
        db.insert("INSERT INTO users(name,email,password_hash,role,created_at) VALUES (?,?,?,?,?)",
                  (name, email, generate_password_hash(password), role, to_iso(now_utc())))
        flash("Account created. Please log in.", "success")
        return redirect(url_for("auth.login"))
    return render_template("auth/register.html", invite_required=invite_required, form={})


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user():
        return redirect(_home_for(current_user()))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if login_blocked(email):
            flash("Too many failed attempts. Please wait 10 minutes and try again.", "error")
            return render_template("auth/login.html"), 429
        user = get_db().one("SELECT * FROM users WHERE email=?", (email,))
        ok = check_password_hash(user["password_hash"] if user else _DUMMY_HASH, password)
        if user and ok:
            login_ok(email)
            session.clear()
            session["user_id"] = user["id"]
            session.permanent = True
            nxt = safe_next(request.args.get("next"))
            return redirect(nxt or _home_for(user))
        login_failed(email)
        flash("Incorrect email or password.", "error")
        return render_template("auth/login.html"), 401
    return render_template("auth/login.html")


@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("auth.login"))
