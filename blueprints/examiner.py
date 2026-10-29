import json

from flask import (Blueprint, Response, abort, flash, jsonify, redirect,
                   render_template, request, url_for)

import report
import scoring
from db import get_db
from services import (finalize_expired, load_questions, refresh_score, save_assignments,
                      save_questions, validate_exam_payload)
from utils import current_user, fmt_utc, iso_z, login_required, now_utc, parse_iso, to_iso

bp = Blueprint("examiner", __name__)


# ------------------------------------------------------------------ helpers
def _own_exam(db, exam_id):
    exam = db.one("SELECT * FROM exams WHERE id=? AND examiner_id=?", (exam_id, current_user()["id"]))
    if not exam:
        abort(404)
    return exam


def _own_attempt(db, attempt_id):
    att = db.one(
        """SELECT t.*, u.name AS student_name, u.email AS student_email
           FROM attempts t JOIN exams e ON e.id=t.exam_id JOIN users u ON u.id=t.student_id
           WHERE t.id=? AND e.examiner_id=?""", (attempt_id, current_user()["id"]))
    if not att:
        abort(404)
    return att


def _total_marks(db, exam_id):
    return db.one("SELECT COALESCE(SUM(marks),0) AS m FROM questions WHERE exam_id=?", (exam_id,))["m"]


def _duration_used(att):
    end = parse_iso(att["submitted_at"]) or now_utc()
    return max(int((end - parse_iso(att["started_at"])).total_seconds()), 0)


def _attempt_context(db, att, with_images=False):
    exam = db.one("SELECT * FROM exams WHERE id=?", (att["exam_id"],))
    cols = "id, vtype, detail, occurred_at, offset_sec, (screenshot IS NOT NULL) AS has_shot"
    if with_images:
        cols += ", screenshot"
    vios = db.all(f"SELECT {cols} FROM violations WHERE attempt_id=? ORDER BY occurred_at, id", (att["id"],))
    counts = scoring.counts_from(vios)
    duration = _duration_used(att)
    full_dur = max(duration, 1) if att["status"] != "in_progress" else exam["duration_min"] * 60
    answers = db.all(
        """SELECT q.id AS qid, q.qtype, q.text, q.options, q.correct, q.tolerance, q.marks, q.position,
                  aa.answer, aa.auto_marks, aa.manual_marks, aa.feedback
           FROM questions q LEFT JOIN attempt_answers aa ON aa.question_id=q.id AND aa.attempt_id=?
           WHERE q.exam_id=? ORDER BY q.position, q.id""", (att["id"], att["exam_id"]))
    for a in answers:
        a["options"] = json.loads(a["options"] or "[]")
        a["answer"] = a["answer"] or ""
        a["feedback"] = a["feedback"] or ""
    total = _total_marks(db, att["exam_id"])
    return {
        "exam": exam,
        "student": {"name": att["student_name"], "email": att["student_email"]},
        "attempt": att, "total_marks": total, "answers": answers, "violations": vios,
        "breakdown": scoring.breakdown(counts),
        "analysis": scoring.analyze(vios, full_dur, att["misuse_pct"], att["risk_level"], att["pending"]),
        "buckets": scoring.timeline_buckets(vios, full_dur),
        "duration_sec": duration,
    }


def _exam_rows(db, exam_id):
    return db.all(
        """SELECT t.*, u.name, u.email FROM attempts t JOIN users u ON u.id=t.student_id
           WHERE t.exam_id=? ORDER BY t.misuse_pct DESC, u.name""", (exam_id,))


# ---------------------------------------------------------------- dashboard
@bp.route("/examiner")
@login_required("examiner")
def dashboard():
    db, user = get_db(), current_user()
    finalize_expired(db)
    exams = db.all(
        """SELECT e.*,
            (SELECT COUNT(*) FROM questions q WHERE q.exam_id=e.id) AS n_questions,
            (SELECT COUNT(*) FROM assignments a WHERE a.exam_id=e.id) AS n_assigned,
            (SELECT COUNT(*) FROM attempts t WHERE t.exam_id=e.id) AS n_attempts,
            (SELECT COUNT(*) FROM attempts t WHERE t.exam_id=e.id AND t.status='in_progress') AS n_live,
            (SELECT COUNT(*) FROM attempts t WHERE t.exam_id=e.id AND t.risk_level='High') AS n_high,
            (SELECT AVG(t.misuse_pct) FROM attempts t WHERE t.exam_id=e.id AND t.status<>'in_progress') AS avg_misuse
           FROM exams e WHERE e.examiner_id=? ORDER BY e.id DESC""", (user["id"],))
    return render_template("examiner/dashboard.html", exams=exams)


# ------------------------------------------------------------ create / edit
def _students(db):
    return db.all("SELECT id, name, email FROM users WHERE role='student' ORDER BY name")


@bp.route("/examiner/exams/new")
@login_required("examiner")
def new_exam():
    return render_template("examiner/exam_form.html", students=_students(get_db()), exam=None,
                           init={"questions": [], "student_ids": [], "locked": False})


@bp.route("/examiner/exams/<int:exam_id>/edit")
@login_required("examiner")
def edit_exam(exam_id):
    db = get_db()
    exam = _own_exam(db, exam_id)
    qs = load_questions(db, exam_id, include_answers=True)
    questions = []
    for q in qs:
        item = {"qtype": q["qtype"], "text": q["text"], "marks": q["marks"], "options": q["options"],
                "correct": "", "tolerance": q["tolerance"]}
        if q["qtype"] == "mcq":
            item["correct"] = int(q["correct"]) if str(q["correct"]).isdigit() else 0
        else:
            item["correct"] = q["correct"]
        questions.append(item)
    sids = [r["student_id"] for r in db.all("SELECT student_id FROM assignments WHERE exam_id=?", (exam_id,))]
    locked = db.one("SELECT COUNT(*) AS n FROM attempts WHERE exam_id=?", (exam_id,))["n"] > 0
    init = {"title": exam["title"], "description": exam["description"], "duration_min": exam["duration_min"],
            "start_at": iso_z(exam["start_at"]), "end_at": iso_z(exam["end_at"]),
            "max_violations": exam["max_violations"], "published": bool(exam["published"]),
            "questions": questions, "student_ids": sids, "locked": locked}
    return render_template("examiner/exam_form.html", students=_students(db), exam=exam, init=init)


@bp.route("/examiner/api/exams", methods=["POST"])
@bp.route("/examiner/api/exams/<int:exam_id>", methods=["POST"])
@login_required("examiner")
def save_exam(exam_id=None):
    db, user = get_db(), current_user()
    data = request.get_json(silent=True) or {}
    clean, errors = validate_exam_payload(db, data)
    locked = False
    if exam_id is not None:
        _own_exam(db, exam_id)
        locked = db.one("SELECT COUNT(*) AS n FROM attempts WHERE exam_id=?", (exam_id,))["n"] > 0
        if locked:  # questions can't change once students have attempted
            errors = [e for e in errors if not e.startswith(("Question", "Add at least", "An exam can"))]
    if errors:
        return jsonify(errors=errors), 400

    if exam_id is None:
        exam_id = db.insert(
            """INSERT INTO exams(examiner_id,title,description,duration_min,start_at,end_at,published,max_violations,created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (user["id"], clean["title"], clean["description"], clean["duration_min"], clean["start_at"],
             clean["end_at"], clean["published"], clean["max_violations"], to_iso(now_utc())))
    else:
        db.execute(
            """UPDATE exams SET title=?, description=?, duration_min=?, start_at=?, end_at=?, published=?, max_violations=?
               WHERE id=?""",
            (clean["title"], clean["description"], clean["duration_min"], clean["start_at"], clean["end_at"],
             clean["published"], clean["max_violations"], exam_id))
    if not locked:
        save_questions(db, exam_id, clean["questions"])
    save_assignments(db, exam_id, clean["student_ids"])
    flash("Exam saved and published." if clean["published"] else "Exam saved as draft.", "success")
    return jsonify(ok=True, redirect=url_for("examiner.exam_detail", exam_id=exam_id))


@bp.route("/examiner/exams/<int:exam_id>/publish", methods=["POST"])
@login_required("examiner")
def toggle_publish(exam_id):
    db = get_db()
    exam = _own_exam(db, exam_id)
    if not exam["published"]:
        if not db.one("SELECT 1 AS x FROM questions WHERE exam_id=? LIMIT 1", (exam_id,)):
            flash("Add questions before publishing.", "error")
            return redirect(url_for("examiner.exam_detail", exam_id=exam_id))
    db.execute("UPDATE exams SET published=? WHERE id=?", (0 if exam["published"] else 1, exam_id))
    flash("Exam unpublished." if exam["published"] else "Exam published to assigned students.", "success")
    return redirect(url_for("examiner.exam_detail", exam_id=exam_id))


@bp.route("/examiner/exams/<int:exam_id>/delete", methods=["POST"])
@login_required("examiner")
def delete_exam(exam_id):
    db = get_db()
    _own_exam(db, exam_id)
    db.execute("DELETE FROM exams WHERE id=?", (exam_id,))
    flash("Exam deleted.", "success")
    return redirect(url_for("examiner.dashboard"))


# --------------------------------------------------------------- exam detail
@bp.route("/examiner/exams/<int:exam_id>")
@login_required("examiner")
def exam_detail(exam_id):
    db = get_db()
    exam = _own_exam(db, exam_id)
    finalize_expired(db, exam_id=exam_id)
    total = _total_marks(db, exam_id)
    rows = _exam_rows(db, exam_id)
    done = [r for r in rows if r["status"] != "in_progress"]
    assigned = db.all(
        """SELECT u.id, u.name, u.email FROM assignments a JOIN users u ON u.id=a.student_id
           WHERE a.exam_id=? ORDER BY u.name""", (exam_id,))
    attempted_ids = {r["student_id"] for r in rows}
    not_started = [s for s in assigned if s["id"] not in attempted_ids]
    stats = {
        "assigned": len(assigned), "started": len(rows), "completed": len(done),
        "live": len(rows) - len(done),
        "avg_score_pct": (sum(r["score"] for r in done) / len(done) / total * 100) if done and total else 0.0,
        "avg_misuse": (sum(r["misuse_pct"] for r in done) / len(done)) if done else 0.0,
        "high": sum(1 for r in rows if r["risk_level"] == "High"),
        "medium": sum(1 for r in rows if r["risk_level"] == "Medium"),
        "low": sum(1 for r in rows if r["risk_level"] == "Low"),
    }
    vt = db.all(
        """SELECT v.vtype, COUNT(*) AS n FROM violations v JOIN attempts t ON t.id=v.attempt_id
           WHERE t.exam_id=? GROUP BY v.vtype ORDER BY n DESC""", (exam_id,))
    vtotals = []
    for r in vt:
        meta = scoring.VIOLATION_TYPES.get(r["vtype"])
        if meta:
            vtotals.append({"label": meta[0], "count": r["n"], "color": meta[3]})
    n_q = db.one("SELECT COUNT(*) AS n FROM questions WHERE exam_id=?", (exam_id,))["n"]
    return render_template("examiner/exam_detail.html", exam=exam, rows=rows, total=total, stats=stats,
                           not_started=not_started, vtotals=vtotals, n_questions=n_q,
                           max_vt=max([v["count"] for v in vtotals] or [1]))


@bp.route("/examiner/api/exams/<int:exam_id>/live")
@login_required("examiner")
def live(exam_id):
    db = get_db()
    _own_exam(db, exam_id)
    finalize_expired(db, exam_id=exam_id)
    rows = _exam_rows(db, exam_id)
    return jsonify([{
        "id": r["id"], "status": r["status"], "misuse_pct": r["misuse_pct"], "risk": r["risk_level"],
        "violations": r["violation_count"], "score": r["score"], "pending": r["pending"],
    } for r in rows])


# ------------------------------------------------------------ attempt detail
@bp.route("/examiner/attempts/<int:attempt_id>")
@login_required("examiner")
def attempt_detail(attempt_id):
    db = get_db()
    att = _own_attempt(db, attempt_id)
    finalize_expired(db, exam_id=att["exam_id"])
    att = _own_attempt(db, attempt_id)
    ctx = _attempt_context(db, att)
    ctx["vlabel"] = scoring.label
    ctx["vcolor"] = scoring.color
    ctx["max_bucket"] = max(ctx["buckets"] or [1]) or 1
    ctx["max_count"] = max([b["count"] for b in ctx["breakdown"]] or [1])
    return render_template("examiner/attempt_detail.html", **ctx)


@bp.route("/examiner/attempts/<int:attempt_id>/grade", methods=["POST"])
@login_required("examiner")
def grade(attempt_id):
    db = get_db()
    att = _own_attempt(db, attempt_id)
    if att["status"] == "in_progress":
        flash("Wait until the student submits before grading.", "error")
        return redirect(url_for("examiner.attempt_detail", attempt_id=attempt_id))
    qs = db.all("SELECT id, marks FROM questions WHERE exam_id=? AND qtype='text'", (att["exam_id"],))
    for q in qs:
        raw = request.form.get(f"marks_{q['id']}", "").strip()
        feedback = request.form.get(f"feedback_{q['id']}", "").strip()[:1000]
        if raw == "":
            continue
        try:
            m = float(raw)
        except ValueError:
            continue
        m = max(0.0, min(m, float(q["marks"])))
        db.execute(
            """INSERT INTO attempt_answers(attempt_id, question_id, answer, manual_marks, feedback)
               VALUES (?,?,'',?,?)
               ON CONFLICT(attempt_id, question_id) DO UPDATE SET manual_marks=excluded.manual_marks,
               feedback=excluded.feedback""", (attempt_id, q["id"], m, feedback))
    refresh_score(db, attempt_id)
    flash("Grades saved.", "success")
    return redirect(url_for("examiner.attempt_detail", attempt_id=attempt_id))


@bp.route("/examiner/attempts/<int:attempt_id>/reset", methods=["POST"])
@login_required("examiner")
def reset_attempt(attempt_id):
    db = get_db()
    att = _own_attempt(db, attempt_id)
    db.execute("DELETE FROM attempts WHERE id=?", (attempt_id,))
    flash(f"Attempt deleted - {att['student_name']} can take the exam again.", "success")
    return redirect(url_for("examiner.exam_detail", exam_id=att["exam_id"]))


# ------------------------------------------------------------------ evidence
@bp.route("/evidence/<int:violation_id>.jpg")
@login_required("examiner")
def evidence(violation_id):
    db = get_db()
    row = db.one(
        """SELECT v.screenshot FROM violations v JOIN attempts t ON t.id=v.attempt_id
           JOIN exams e ON e.id=t.exam_id WHERE v.id=? AND e.examiner_id=?""",
        (violation_id, current_user()["id"]))
    if not row or not row["screenshot"]:
        abort(404)
    return Response(row["screenshot"], mimetype="image/jpeg",
                    headers={"Cache-Control": "private, max-age=3600"})


# ---------------------------------------------------------------------- PDFs
def _safe_name(s):
    return "".join(c if c.isalnum() else "_" for c in s)[:40] or "report"


@bp.route("/examiner/attempts/<int:attempt_id>/report.pdf")
@login_required("examiner")
def attempt_pdf(attempt_id):
    db = get_db()
    att = _own_attempt(db, attempt_id)
    ctx = _attempt_context(db, att, with_images=True)
    pdf = report.build_attempt_pdf(ctx)
    name = f"proctoring_{_safe_name(att['student_name'])}_{_safe_name(ctx['exam']['title'])}.pdf"
    return Response(pdf, mimetype="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@bp.route("/examiner/exams/<int:exam_id>/report.pdf")
@login_required("examiner")
def exam_pdf(exam_id):
    db = get_db()
    exam = _own_exam(db, exam_id)
    finalize_expired(db, exam_id=exam_id)
    total = _total_marks(db, exam_id)
    rows = _exam_rows(db, exam_id)
    done = [r for r in rows if r["status"] != "in_progress"]
    assigned = db.one("SELECT COUNT(*) AS n FROM assignments WHERE exam_id=?", (exam_id,))["n"]
    vt = db.all("""SELECT v.vtype, COUNT(*) AS n FROM violations v JOIN attempts t ON t.id=v.attempt_id
                   WHERE t.exam_id=? GROUP BY v.vtype ORDER BY n DESC""", (exam_id,))
    vtotals = [{"label": scoring.label(r["vtype"]), "count": r["n"], "color": scoring.color(r["vtype"])} for r in vt]
    stats = {
        "assigned": assigned, "completed": len(done),
        "avg_score_pct": (sum(r["score"] for r in done) / len(done) / total * 100) if done and total else 0.0,
        "avg_misuse": (sum(r["misuse_pct"] for r in done) / len(done)) if done else 0.0,
        "high": sum(1 for r in rows if r["risk_level"] == "High"),
        "medium": sum(1 for r in rows if r["risk_level"] == "Medium"),
    }
    pdf = report.build_exam_pdf({"exam": exam, "total_marks": total, "rows": rows, "stats": stats,
                                 "violation_totals": vtotals})
    return Response(pdf, mimetype="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="summary_{_safe_name(exam["title"])}.pdf"'})
