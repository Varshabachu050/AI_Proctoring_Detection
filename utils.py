"""Time helpers, CSRF, auth decorators, rate limiting, security headers."""
import hmac
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from functools import wraps
from urllib.parse import urlparse

from flask import abort, g, jsonify, redirect, request, session, url_for

import db as dbm

ISO_FMT = "%Y-%m-%dT%H:%M:%S"


# ------------------------------------------------------------------ time
def now_utc():
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def to_iso(dt):
    return dt.strftime(ISO_FMT)


def parse_iso(s):
    """Parse 'YYYY-MM-DDTHH:MM:SS' (optionally with trailing Z / millis)."""
    if not s:
        return None
    s = str(s).strip().rstrip("Z")
    s = s.split(".")[0]
    try:
        return datetime.strptime(s[:19], ISO_FMT)
    except ValueError:
        try:
            return datetime.strptime(s[:16], "%Y-%m-%dT%H:%M")
        except ValueError:
            return None


def iso_z(s):
    """Stored UTC string -> JS-friendly ISO with Z."""
    return (s or "")[:19] + "Z" if s else ""


def fmt_utc(s):
    dt = parse_iso(s)
    return dt.strftime("%d %b %Y, %H:%M UTC") if dt else "-"


def fmt_duration(seconds):
    seconds = max(int(seconds or 0), 0)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h}h {m}m {s}s" if h else f"{m}m {s}s"


# ------------------------------------------------------------------ CSRF
def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def csrf_protect():
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        sent = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token") or ""
        expected = session.get("csrf", "")
        if not expected or not hmac.compare_digest(sent, expected):
            if request.path.startswith(("/api/", "/examiner/api/")):
                return jsonify(error="Invalid or missing CSRF token. Refresh the page."), 400
            abort(400, "Invalid or missing CSRF token. Please go back, refresh the page and try again.")


# ------------------------------------------------------------------ auth
def current_user():
    if "user" in g:
        return g.user
    g.user = None
    uid = session.get("user_id")
    if uid:
        g.user = dbm.get_db().one(
            "SELECT id, name, email, role FROM users WHERE id=?", (uid,))
        if g.user is None:
            session.clear()
    return g.user


def _wants_json():
    return request.path.startswith(("/api/", "/examiner/api/"))


def login_required(role=None):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            user = current_user()
            if not user:
                if _wants_json():
                    return jsonify(error="Please log in again."), 401
                return redirect(url_for("auth.login", next=request.path))
            if role and user["role"] != role:
                if _wants_json():
                    return jsonify(error="Forbidden"), 403
                abort(403)
            return fn(*a, **kw)
        return wrapper
    return deco


def safe_next(target):
    """Only allow same-site relative redirects."""
    if not target:
        return None
    p = urlparse(target)
    if p.scheme or p.netloc or not target.startswith("/") or target.startswith("//"):
        return None
    return target


# ---------------------------------------------------------- rate limiting
_FAILS = {}
MAX_FAILS = 8
WINDOW_SEC = 600


def _client_ip():
    return request.remote_addr or "?"


def login_blocked(email):
    key = (_client_ip(), (email or "").lower())
    now = time.time()
    hits = [t for t in _FAILS.get(key, []) if now - t < WINDOW_SEC]
    _FAILS[key] = hits
    return len(hits) >= MAX_FAILS


def login_failed(email):
    key = (_client_ip(), (email or "").lower())
    _FAILS.setdefault(key, []).append(time.time())


def login_ok(email):
    _FAILS.pop((_client_ip(), (email or "").lower()), None)


# ------------------------------------------------------------- validation
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    # camera + microphone are required by the exam page, only for our own origin
    resp.headers.setdefault("Permissions-Policy", "camera=(self), microphone=(self), fullscreen=(self)")
    if request.path.startswith(("/api/", "/examiner/api/", "/evidence/")) or request.endpoint in (None,):
        resp.headers["Cache-Control"] = "no-store"
    return resp
