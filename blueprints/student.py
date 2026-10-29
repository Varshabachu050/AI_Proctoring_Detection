from flask import Blueprint, abort, render_template

from db import get_db
from services import exam_state, finalize_expired
from utils import current_user, login_required

bp = Blueprint("student", __name__)


def _assigned_exam(db, exam_id, student_id):
    return db.one(
        """SELECT e.*, u.name AS examiner_name FROM exams e
           JOIN assignments a ON a.exam_id=e.id AND a.student_id=?
           JOIN users u ON u.id=e.examiner_id
           WHERE e.id=? AND e.published=1""", (student_id, exam_id))


@bp.route("/student")
@login_required("student")
def dashboard():
    db, user = get_db(), current_user()
    finalize_expired(db, student_id=user["id"])
    rows = db.all(
        """SELECT e.*, u.name AS examiner_name, t.id AS attempt_id, t.status AS attempt_status,
                  t.score, t.pending,
                  (SELECT COALESCE(SUM(marks),0) FROM questions q WHERE q.exam_id=e.id) AS total_marks,
                  (SELECT COUNT(*) FROM questions q WHERE q.exam_id=e.id) AS n_questions
           FROM assignments a
           JOIN exams e ON e.id=a.exam_id AND e.published=1
           JOIN users u ON u.id=e.examiner_id
           LEFT JOIN attempts t ON t.exam_id=e.id AND t.student_id=a.student_id
           WHERE a.student_id=? ORDER BY e.start_at DESC""", (user["id"],))
    for r in rows:
        att = {"status": r["attempt_status"]} if r["attempt_id"] else None
        r["state"] = exam_state(r, att)
    return render_template("student/dashboard.html", exams=rows)


@bp.route("/exam/<int:exam_id>/take")
@login_required("student")
def take(exam_id):
    db, user = get_db(), current_user()
    finalize_expired(db, student_id=user["id"])
    exam = _assigned_exam(db, exam_id, user["id"])
    if not exam:
        abort(404)
    attempt = db.one("SELECT * FROM attempts WHERE exam_id=? AND student_id=?", (exam_id, user["id"]))
    state = exam_state(exam, attempt)
    n_q = db.one("SELECT COUNT(*) AS n FROM questions WHERE exam_id=?", (exam_id,))["n"]
    return render_template("student/take.html", exam=exam, state=state, n_questions=n_q, attempt=attempt)


@bp.route("/student/attempts/<int:attempt_id>")
@login_required("student")
def result(attempt_id):
    db, user = get_db(), current_user()
    att = db.one(
        """SELECT t.*, e.title, e.description,
                  (SELECT COALESCE(SUM(marks),0) FROM questions q WHERE q.exam_id=e.id) AS total_marks
           FROM attempts t JOIN exams e ON e.id=t.exam_id
           WHERE t.id=? AND t.student_id=?""", (attempt_id, user["id"]))
    if not att:
        abort(404)
    return render_template("student/result.html", att=att)
