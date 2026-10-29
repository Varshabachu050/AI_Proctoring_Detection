"""JSON API used by the exam page (answers, violations, submit)."""
import base64
import binascii
from datetime import timedelta

from flask import Blueprint, current_app, jsonify, request

import scoring
from db import get_db
from services import exam_state, finalize_attempt, finalize_expired, load_questions, recompute_misuse
from utils import current_user, iso_z, login_required, now_utc, parse_iso, to_iso

bp = Blueprint("api", __name__, url_prefix="/api")


def _own_active_attempt(db, attempt_id, user):
    """Return (attempt, error_response). Allows a short grace after the deadline."""
    att = db.one("SELECT * FROM attempts WHERE id=? AND student_id=?", (attempt_id, user["id"]))
    if not att:
        return None, (jsonify(error="Attempt not found"), 404)
    if att["status"] != "in_progress":
        return att, (jsonify(error="This exam has already been submitted.", submitted=True), 409)
    grace = current_app.config["DEADLINE_GRACE_SEC"]
    if now_utc() > parse_iso(att["deadline_at"]) + timedelta(seconds=grace):
        finalize_attempt(db, att["id"], "auto_submitted")
        # the 409 response would normally roll back; commit explicitly
        db.commit()
        return att, (jsonify(error="Time is up. Your exam was submitted automatically.", submitted=True), 409)
    return att, None


@bp.route("/exams/<int:exam_id>/start", methods=["POST"])
@login_required("student")
def start(exam_id):
    db, user = get_db(), current_user()
    exam = db.one(
        """SELECT e.* FROM exams e JOIN assignments a ON a.exam_id=e.id AND a.student_id=?
           WHERE e.id=? AND e.published=1""", (user["id"], exam_id))
    if not exam:
        return jsonify(error="Exam not found or not assigned to you."), 404

    finalize_expired(db, exam_id=exam_id, student_id=user["id"])
    att = db.one("SELECT * FROM attempts WHERE exam_id=? AND student_id=?", (exam_id, user["id"]))
    if att and att["status"] != "in_progress":
        db.commit()
        return jsonify(error="You have already completed this exam.", submitted=True), 409
    if not att:
        state = exam_state(exam)
        if state == "upcoming":
            return jsonify(error="This exam has not started yet."), 403
        if state == "closed":
            return jsonify(error="The exam window has closed."), 403
        now = now_utc()
        deadline = now + timedelta(minutes=exam["duration_min"])
        db.one(
            """INSERT INTO attempts(exam_id, student_id, started_at, deadline_at)
               VALUES (?,?,?,?) ON CONFLICT(exam_id, student_id) DO NOTHING RETURNING id""",
            (exam_id, user["id"], to_iso(now), to_iso(deadline)))
        att = db.one("SELECT * FROM attempts WHERE exam_id=? AND student_id=?", (exam_id, user["id"]))

    saved = {str(r["question_id"]): r["answer"] for r in
             db.all("SELECT question_id, answer FROM attempt_answers WHERE attempt_id=?", (att["id"],))}
    return jsonify(
        attempt_id=att["id"],
        title=exam["title"],
        deadline=iso_z(att["deadline_at"]),
        server_now=iso_z(to_iso(now_utc())),
        max_violations=exam["max_violations"],
        violations=att["violation_count"],
        questions=load_questions(db, exam_id),
        answers=saved,
    )


def _save_answer(db, attempt, exam_id, qid, answer):
    q = db.one("SELECT id FROM questions WHERE id=? AND exam_id=?", (qid, exam_id))
    if not q:
        return False
    db.execute(
        """INSERT INTO attempt_answers(attempt_id, question_id, answer) VALUES (?,?,?)
           ON CONFLICT(attempt_id, question_id) DO UPDATE SET answer=excluded.answer""",
        (attempt["id"], qid, str(answer)[:10000]))
    return True


@bp.route("/attempts/<int:attempt_id>/answer", methods=["POST"])
@login_required("student")
def answer(attempt_id):
    db, user = get_db(), current_user()
    att, err = _own_active_attempt(db, attempt_id, user)
    if err:
        return err
    data = request.get_json(silent=True) or {}
    try:
        qid = int(data.get("question_id"))
    except (TypeError, ValueError):
        return jsonify(error="Bad question id"), 400
    if not _save_answer(db, att, att["exam_id"], qid, data.get("answer", "")):
        return jsonify(error="Unknown question"), 404
    return jsonify(ok=True)


@bp.route("/attempts/<int:attempt_id>/violation", methods=["POST"])
@login_required("student")
def violation(attempt_id):
    db, user = get_db(), current_user()
    att, err = _own_active_attempt(db, attempt_id, user)
    if err:
        return err
    data = request.get_json(silent=True) or {}
    vtype = data.get("type")
    if vtype not in scoring.VIOLATION_TYPES:
        return jsonify(error="Unknown violation type"), 400
    detail = str(data.get("detail", ""))[:300]

    # Server-side de-duplication so a buggy/modified client can't flood the log.
    now = now_utc()
    last = db.one("SELECT occurred_at FROM violations WHERE attempt_id=? AND vtype=? ORDER BY id DESC LIMIT 1",
                  (att["id"], vtype))
    if last and (now - parse_iso(last["occurred_at"])).total_seconds() < current_app.config["VIOLATION_COOLDOWN_SEC"]:
        return jsonify(ok=True, deduped=True, count=att["violation_count"])

    shot = None
    raw = data.get("screenshot")
    n_shots = db.one("SELECT COUNT(*) AS n FROM violations WHERE attempt_id=? AND screenshot IS NOT NULL",
                     (att["id"],))["n"]
    if raw and n_shots < current_app.config["MAX_SCREENSHOTS_PER_ATTEMPT"]:
        try:
            b64 = raw.split(",", 1)[1] if raw.startswith("data:") else raw
            blob = base64.b64decode(b64, validate=True)
            if blob[:3] == b"\xff\xd8\xff" and len(blob) <= current_app.config["MAX_SCREENSHOT_BYTES"]:
                shot = blob
        except (binascii.Error, ValueError, IndexError):
            shot = None

    offset = int((now - parse_iso(att["started_at"])).total_seconds())
    db.insert(
        """INSERT INTO violations(attempt_id, vtype, detail, occurred_at, offset_sec, screenshot)
           VALUES (?,?,?,?,?,?)""",
        (att["id"], vtype, detail, to_iso(now), max(offset, 0), shot))
    _, _, count = recompute_misuse(db, att["id"])

    terminated = False
    exam = db.one("SELECT max_violations FROM exams WHERE id=?", (att["exam_id"],))
    if exam["max_violations"] and count >= exam["max_violations"]:
        finalize_attempt(db, att["id"], "terminated")
        terminated = True
    return jsonify(ok=True, count=count, terminated=terminated)


@bp.route("/attempts/<int:attempt_id>/submit", methods=["POST"])
@login_required("student")
def submit(attempt_id):
    db, user = get_db(), current_user()
    att = db.one("SELECT * FROM attempts WHERE id=? AND student_id=?", (attempt_id, user["id"]))
    if not att:
        return jsonify(error="Attempt not found"), 404
    if att["status"] != "in_progress":
        return jsonify(ok=True, redirect=f"/student/attempts/{att['id']}")
    data = request.get_json(silent=True) or {}
    for qid, ans in (data.get("answers") or {}).items():
        try:
            _save_answer(db, att, att["exam_id"], int(qid), ans)
        except (TypeError, ValueError):
            continue
    finalize_attempt(db, att["id"], "submitted")
    return jsonify(ok=True, redirect=f"/student/attempts/{att['id']}")
