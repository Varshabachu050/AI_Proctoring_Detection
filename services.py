"""Business logic shared by the student, API and examiner blueprints."""
import json
import math

import scoring
from utils import now_utc, parse_iso, to_iso


# ------------------------------------------------------------ questions
def load_questions(db, exam_id, include_answers=False):
    rows = db.all("SELECT * FROM questions WHERE exam_id=? ORDER BY position, id", (exam_id,))
    out = []
    for r in rows:
        q = {
            "id": r["id"], "qtype": r["qtype"], "text": r["text"],
            "options": json.loads(r["options"] or "[]"), "marks": r["marks"],
            "position": r["position"],
        }
        if include_answers:
            q["correct"] = r["correct"]
            q["tolerance"] = r["tolerance"]
        out.append(q)
    return out


# -------------------------------------------------------------- grading
def grade_auto(q_row, answer):
    """Return marks for MCQ/numeric, or None when manual grading is needed."""
    answer = (answer or "").strip()
    if q_row["qtype"] == "mcq":
        return q_row["marks"] if answer != "" and answer == str(q_row["correct"]).strip() else 0.0
    if q_row["qtype"] == "numeric":
        try:
            a = float(answer.replace(",", ""))
            c = float(q_row["correct"])
        except ValueError:
            return 0.0
        if not (math.isfinite(a) and math.isfinite(c)):
            return 0.0
        return q_row["marks"] if abs(a - c) <= float(q_row["tolerance"] or 0) + 1e-9 else 0.0
    return None


def refresh_score(db, attempt_id):
    row = db.one(
        """SELECT COALESCE(SUM(COALESCE(aa.manual_marks, aa.auto_marks, 0)), 0) AS score,
                  COALESCE(SUM(CASE WHEN q.qtype='text' AND aa.manual_marks IS NULL THEN 1 ELSE 0 END), 0) AS pending
           FROM attempt_answers aa JOIN questions q ON q.id = aa.question_id
           WHERE aa.attempt_id=?""", (attempt_id,))
    db.execute("UPDATE attempts SET score=?, pending=? WHERE id=?",
               (float(row["score"]), int(row["pending"]), attempt_id))


def recompute_misuse(db, attempt_id):
    rows = db.all("SELECT vtype FROM violations WHERE attempt_id=?", (attempt_id,))
    counts = scoring.counts_from(rows)
    _, pct, level = scoring.compute_misuse(counts)
    db.execute("UPDATE attempts SET misuse_pct=?, risk_level=?, violation_count=? WHERE id=?",
               (pct, level, len(rows), attempt_id))
    return pct, level, len(rows)


def finalize_attempt(db, attempt_id, status="submitted"):
    """Grade everything, fill blanks for unanswered questions, close the attempt."""
    att = db.one("SELECT * FROM attempts WHERE id=?", (attempt_id,))
    if not att or att["status"] != "in_progress":
        return att
    questions = db.all("SELECT * FROM questions WHERE exam_id=?", (att["exam_id"],))
    saved = {r["question_id"]: r for r in
             db.all("SELECT * FROM attempt_answers WHERE attempt_id=?", (attempt_id,))}
    for q in questions:
        ans = saved[q["id"]]["answer"] if q["id"] in saved else ""
        auto = grade_auto(q, ans)
        manual = 0.0 if (q["qtype"] == "text" and not ans.strip()) else None
        db.execute(
            """INSERT INTO attempt_answers(attempt_id, question_id, answer, auto_marks, manual_marks)
               VALUES (?,?,?,?,?)
               ON CONFLICT(attempt_id, question_id) DO UPDATE
               SET auto_marks=excluded.auto_marks, manual_marks=excluded.manual_marks""",
            (attempt_id, q["id"], ans, auto, manual))
    db.execute("UPDATE attempts SET status=?, submitted_at=? WHERE id=?",
               (status, to_iso(now_utc()), attempt_id))
    refresh_score(db, attempt_id)
    recompute_misuse(db, attempt_id)
    return db.one("SELECT * FROM attempts WHERE id=?", (attempt_id,))


def finalize_expired(db, exam_id=None, student_id=None, grace_sec=15):
    """Close attempts whose timer ran out (e.g. the student closed the browser)."""
    from datetime import timedelta
    cutoff = to_iso(now_utc() - timedelta(seconds=grace_sec))
    sql = "SELECT id FROM attempts WHERE status='in_progress' AND deadline_at < ?"
    params = [cutoff]
    if exam_id is not None:
        sql += " AND exam_id=?"; params.append(exam_id)
    if student_id is not None:
        sql += " AND student_id=?"; params.append(student_id)
    for r in db.all(sql, params):
        finalize_attempt(db, r["id"], "auto_submitted")


# ---------------------------------------------------------- exam status
def exam_state(exam, attempt=None):
    """Label shown to students: upcoming/open/closed/in_progress/submitted."""
    if attempt:
        return "in_progress" if attempt["status"] == "in_progress" else "submitted"
    now = to_iso(now_utc())
    if now < exam["start_at"]:
        return "upcoming"
    if now > exam["end_at"]:
        return "closed"
    return "open"


# -------------------------------------------------- exam payload checks
def validate_exam_payload(db, data):
    """Returns (clean, errors). `clean` is ready to be written to the DB."""
    errs = []
    title = str(data.get("title", "")).strip()
    if not 3 <= len(title) <= 150:
        errs.append("Title must be 3-150 characters.")
    desc = str(data.get("description", "")).strip()[:2000]
    try:
        duration = int(data.get("duration_min"))
        if not 1 <= duration <= 600:
            raise ValueError
    except (TypeError, ValueError):
        errs.append("Duration must be between 1 and 600 minutes.")
        duration = 0
    start, end = parse_iso(data.get("start_at")), parse_iso(data.get("end_at"))
    if not start or not end:
        errs.append("Valid start and end date/time are required.")
    elif end <= start:
        errs.append("The end time must be after the start time.")
    try:
        max_v = int(data.get("max_violations") or 0)
        if not 0 <= max_v <= 100:
            raise ValueError
    except (TypeError, ValueError):
        errs.append("Auto-submit threshold must be between 0 and 100.")
        max_v = 0

    raw_students = data.get("student_ids") or []
    student_ids = []
    for s in raw_students:
        try:
            student_ids.append(int(s))
        except (TypeError, ValueError):
            pass
    student_ids = sorted(set(student_ids))
    if student_ids:
        ph = ",".join("?" * len(student_ids))
        valid = {r["id"] for r in db.all(
            f"SELECT id FROM users WHERE role='student' AND id IN ({ph})", student_ids)}
        student_ids = [s for s in student_ids if s in valid]

    questions = []
    raw_q = data.get("questions") or []
    if not isinstance(raw_q, list) or not raw_q:
        errs.append("Add at least one question.")
        raw_q = []
    if len(raw_q) > 100:
        errs.append("An exam can contain at most 100 questions.")
        raw_q = raw_q[:100]
    for i, q in enumerate(raw_q, 1):
        n = f"Question {i}: "
        qtype = q.get("qtype")
        text = str(q.get("text", "")).strip()
        if qtype not in ("mcq", "numeric", "text"):
            errs.append(n + "invalid type."); continue
        if not text or len(text) > 3000:
            errs.append(n + "question text is required (max 3000 chars).")
        try:
            marks = float(q.get("marks", 1))
            if not (0 < marks <= 100) or not math.isfinite(marks):
                raise ValueError
        except (TypeError, ValueError):
            errs.append(n + "marks must be between 0 and 100."); marks = 1.0
        options, correct, tol = [], "", 0.0
        if qtype == "mcq":
            options = [str(o).strip() for o in (q.get("options") or []) if str(o).strip()]
            if not 2 <= len(options) <= 6:
                errs.append(n + "provide 2-6 non-empty options.")
            try:
                ci = int(q.get("correct"))
                if not 0 <= ci < len(options):
                    raise ValueError
                correct = str(ci)
            except (TypeError, ValueError):
                errs.append(n + "select the correct option.")
        elif qtype == "numeric":
            try:
                cv = float(str(q.get("correct", "")).replace(",", ""))
                if not math.isfinite(cv):
                    raise ValueError
                correct = repr(cv)
            except ValueError:
                errs.append(n + "enter a numeric correct answer.")
            try:
                tol = float(q.get("tolerance") or 0)
                if tol < 0 or not math.isfinite(tol):
                    raise ValueError
            except ValueError:
                errs.append(n + "tolerance must be a number >= 0."); tol = 0.0
        else:
            correct = str(q.get("correct", "")).strip()[:3000]  # model answer (optional)
        questions.append({"qtype": qtype, "text": text, "options": options,
                          "correct": correct, "tolerance": tol, "marks": marks})

    clean = {
        "title": title, "description": desc, "duration_min": duration,
        "start_at": to_iso(start) if start else "", "end_at": to_iso(end) if end else "",
        "max_violations": max_v, "published": 1 if data.get("published") else 0,
        "student_ids": student_ids, "questions": questions,
    }
    if clean["published"] and not questions:
        errs.append("Cannot publish an exam without questions.")
    return clean, errs


def save_questions(db, exam_id, questions):
    db.execute("DELETE FROM questions WHERE exam_id=?", (exam_id,))
    for pos, q in enumerate(questions, 1):
        db.execute(
            """INSERT INTO questions(exam_id, position, qtype, text, options, correct, tolerance, marks)
               VALUES (?,?,?,?,?,?,?,?)""",
            (exam_id, pos, q["qtype"], q["text"], json.dumps(q["options"]),
             q["correct"], q["tolerance"], q["marks"]))


def save_assignments(db, exam_id, student_ids):
    keep = set(student_ids)
    # never un-assign a student who already has an attempt
    for r in db.all("SELECT student_id FROM attempts WHERE exam_id=?", (exam_id,)):
        keep.add(r["student_id"])
    db.execute("DELETE FROM assignments WHERE exam_id=?", (exam_id,))
    for sid in sorted(keep):
        db.execute("INSERT INTO assignments(exam_id, student_id) VALUES (?,?) ON CONFLICT DO NOTHING",
                   (exam_id, sid))
