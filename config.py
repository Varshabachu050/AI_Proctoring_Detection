"""Central configuration. Everything sensitive comes from environment variables."""
import os
import secrets


def _bool(name, default=False):
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


class Config:
    # Render sets RENDER=true automatically on its services.
    ON_RENDER = _bool("RENDER")

    # In production SECRET_KEY MUST be set (render.yaml generates one).
    # Locally we fall back to a random key (sessions reset on restart).
    SECRET_KEY = os.environ.get("SECRET_KEY") or secrets.token_hex(32)

    DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///proctor.db")

    # If set, examiners must enter this code to register (recommended in prod).
    EXAMINER_INVITE_CODE = os.environ.get("EXAMINER_INVITE_CODE", "").strip()

    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = ON_RENDER or _bool("COOKIE_SECURE")
    PERMANENT_SESSION_LIFETIME = 60 * 60 * 12  # 12 hours

    # Violation POSTs carry a JPEG screenshot as base64; keep requests bounded.
    MAX_CONTENT_LENGTH = 1_500_000

    # Evidence limits (free Render Postgres has only 1 GB of storage).
    MAX_SCREENSHOT_BYTES = 200_000
    MAX_SCREENSHOTS_PER_ATTEMPT = 120

    # Server-side de-duplication window for the same violation type (seconds).
    VIOLATION_COOLDOWN_SEC = 4

    # Grace period after the deadline in which late saves/submits are accepted.
    DEADLINE_GRACE_SEC = 15
