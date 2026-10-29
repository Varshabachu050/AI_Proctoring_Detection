"""AI Online Proctoring System - Flask application entry point.

Run locally :  python app.py
Production  :  gunicorn app:app   (see render.yaml)
"""
import logging

from flask import Flask, jsonify, redirect, render_template, request, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

import db
import scoring
from config import Config
from utils import (csrf_protect, csrf_token, current_user, fmt_duration, fmt_utc,
                   iso_z, security_headers)


def create_app(config_overrides=None):
    app = Flask(__name__)
    app.config.from_object(Config)
    if config_overrides:
        app.config.update(config_overrides)
    if not app.config["ON_RENDER"] and not app.testing:
        logging.getLogger(__name__).warning(
            "Running locally. Camera access works on http://localhost; any other host needs HTTPS.")

    # Render terminates TLS in front of us; trust its X-Forwarded-* headers.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    db.init_app(app)

    from blueprints import api, auth, examiner, student
    for m in (auth, student, examiner, api):
        app.register_blueprint(m.bp)

    app.before_request(csrf_protect)
    app.after_request(security_headers)

    app.jinja_env.globals.update(csrf_token=csrf_token, current_user=current_user,
                                 vlabel=scoring.label, vcolor=scoring.color,
                                 VIOLATION_TYPES=scoring.VIOLATION_TYPES)
    app.jinja_env.filters["utc"] = fmt_utc
    app.jinja_env.filters["duration"] = fmt_duration

    @app.template_filter("localtime")
    def localtime(s):
        """Render a stored UTC time so the browser shows it in the viewer's timezone."""
        from markupsafe import Markup, escape
        if not s:
            return "-"
        return Markup(f'<time class="lt" datetime="{escape(iso_z(s))}">{escape(fmt_utc(s))}</time>')

    @app.route("/")
    def index():
        user = current_user()
        if user:
            return redirect(url_for("examiner.dashboard" if user["role"] == "examiner" else "student.dashboard"))
        return render_template("index.html")

    @app.route("/healthz")
    def healthz():
        db.get_db().one("SELECT 1 AS ok")
        return jsonify(status="ok")

    def _err(code, title, msg):
        if request.path.startswith(("/api/", "/examiner/api/")):
            return jsonify(error=msg), code
        return render_template("error.html", code=code, title=title, msg=msg), code

    @app.errorhandler(400)
    def e400(e): return _err(400, "Bad request", getattr(e, "description", "Bad request"))

    @app.errorhandler(403)
    def e403(e): return _err(403, "Forbidden", "You don't have permission to view this page.")

    @app.errorhandler(404)
    def e404(e): return _err(404, "Not found", "That page doesn't exist, or you don't have access to it.")

    @app.errorhandler(413)
    def e413(e): return _err(413, "Too large", "The request was too large.")

    @app.errorhandler(500)
    def e500(e): return _err(500, "Server error", "Something went wrong on our side. Please try again.")

    return app


app = create_app()

if __name__ == "__main__":
    app.run(debug=True, port=5000)
