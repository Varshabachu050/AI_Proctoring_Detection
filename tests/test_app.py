"""End-to-end backend tests (SQLite). Run:  python -m unittest discover -s tests -v"""
import base64
import io
import os
import sys
import tempfile
import unittest
from datetime import timedelta

_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_tmp, 'import.db')}"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image  # noqa: E402

import scoring  # noqa: E402
from app import create_app  # noqa: E402
from utils import now_utc, to_iso  # noqa: E402


def jpeg_b64(color=(200, 50, 50), size=(320, 240)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG", quality=60)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


class Base(unittest.TestCase):
    def setUp(self):
        self.dbfile = os.path.join(tempfile.mkdtemp(), "t.db")
        self.app = create_app({"TESTING": True, "DATABASE_URL": f"sqlite:///{self.dbfile}",
                               "SECRET_KEY": "test", "VIOLATION_COOLDOWN_SEC": 0})
        self.ex = self.app.test_client()
        self.st = self.app.test_client()

    # -- helpers
    def token(self, c):
        """A browser gets its CSRF token from any rendered page; mimic that."""
        with c.session_transaction() as s:
            if "csrf" not in s:
                s["csrf"] = "tok-" + os.urandom(8).hex()
            return s["csrf"]

    def register(self, c, name, email, role, pw="password123"):
        t = self.token(c)
        return c.post("/register", data=dict(csrf_token=t, name=name, email=email, role=role,
                                             password=pw, confirm=pw), follow_redirects=False)

    def login(self, c, email, pw="password123"):
        t = self.token(c)
        return c.post("/login", data=dict(csrf_token=t, email=email, password=pw))

    def jpost(self, c, url, payload):
        return c.post(url, json=payload, headers={"X-CSRF-Token": self.token(c)})

    def make_users(self):
        self.register(self.ex, "Prof X", "prof@x.com", "examiner")
        self.register(self.st, "Alice", "alice@x.com", "student")
        self.login(self.ex, "prof@x.com"); self.login(self.st, "alice@x.com")
        with self.app.app_context():
            from db import get_db
            self.alice_id = get_db().one("SELECT id FROM users WHERE email='alice@x.com'")["id"]

    def exam_payload(self, **kw):
        now = now_utc()
        p = dict(title="Physics Mid-term", description="d", duration_min=30,
                 start_at=to_iso(now - timedelta(minutes=5)), end_at=to_iso(now + timedelta(hours=2)),
                 max_violations=0, published=True, student_ids=[self.alice_id],
                 questions=[
                     dict(qtype="mcq", text="2+2?", marks=2, options=["3", "4", "5"], correct=1),
                     dict(qtype="numeric", text="g?", marks=3, correct="9.81", tolerance=0.05),
                     dict(qtype="text", text="Explain gravity", marks=5, correct="model"),
                 ])
        p.update(kw)
        return p

    def create_exam(self, **kw):
        r = self.jpost(self.ex, "/examiner/api/exams", self.exam_payload(**kw))
        self.assertEqual(r.status_code, 200, r.get_json())
        return int(r.get_json()["redirect"].rstrip("/").split("/")[-1])

    def start(self, exam_id):
        r = self.jpost(self.st, f"/api/exams/{exam_id}/start", {})
        self.assertEqual(r.status_code, 200, r.get_json())
        return r.get_json()


class AuthTests(Base):
    def test_register_login_logout_and_roles(self):
        self.make_users()
        self.assertEqual(self.ex.get("/examiner").status_code, 200)
        self.assertEqual(self.st.get("/student").status_code, 200)
        self.assertEqual(self.st.get("/examiner").status_code, 403)
        self.assertEqual(self.ex.get("/student").status_code, 403)
        t = self.token(self.st)
        self.st.post("/logout", data=dict(csrf_token=t))
        self.assertEqual(self.st.get("/student").status_code, 302)

    def test_password_hashed_not_plain(self):
        self.make_users()
        with self.app.app_context():
            from db import get_db
            h = get_db().one("SELECT password_hash FROM users WHERE email='alice@x.com'")["password_hash"]
        self.assertNotIn("password123", h)

    def test_duplicate_and_validation(self):
        self.register(self.ex, "A", "dup@x.com", "student")
        r = self.register(self.ex, "A", "dup@x.com", "student")
        self.assertEqual(r.status_code, 400)
        r = self.register(self.ex, "Bob", "b@x.com", "student", pw="short")
        self.assertEqual(r.status_code, 400)

    def test_csrf_required(self):
        r = self.ex.post("/login", data=dict(email="a@b.c", password="x"))
        self.assertEqual(r.status_code, 400)

    def test_wrong_password_and_open_redirect_blocked(self):
        self.register(self.ex, "Prof", "p@x.com", "examiner")
        r = self.login(self.ex, "p@x.com", "wrongpass1")
        self.assertEqual(r.status_code, 401)
        t = self.token(self.ex)
        r = self.ex.post("/login?next=//evil.com", data=dict(csrf_token=t, email="p@x.com", password="password123"))
        self.assertEqual(r.status_code, 302)
        self.assertNotIn("evil.com", r.headers["Location"])

    def test_examiner_invite_code(self):
        app = create_app({"TESTING": True, "DATABASE_URL": f"sqlite:///{self.dbfile}2",
                          "EXAMINER_INVITE_CODE": "letmein", "SECRET_KEY": "t"})
        c = app.test_client(); c.get("/login")
        with c.session_transaction() as s: t = s["csrf"]
        r = c.post("/register", data=dict(csrf_token=t, name="Eve", email="e@x.com", role="examiner",
                                          password="password123", confirm="password123", invite_code="nope"))
        self.assertEqual(r.status_code, 400)
        r = c.post("/register", data=dict(csrf_token=t, name="Eve", email="e@x.com", role="examiner",
                                          password="password123", confirm="password123", invite_code="letmein"))
        self.assertEqual(r.status_code, 302)

    def test_healthz(self):
        self.assertEqual(self.ex.get("/healthz").get_json(), {"status": "ok"})


class ExamFlowTests(Base):
    def test_create_validation(self):
        self.make_users()
        r = self.jpost(self.ex, "/examiner/api/exams", self.exam_payload(title="x", questions=[]))
        self.assertEqual(r.status_code, 400)
        self.assertTrue(r.get_json()["errors"])
        bad_q = [dict(qtype="mcq", text="q", marks=1, options=["only"], correct=0)]
        r = self.jpost(self.ex, "/examiner/api/exams", self.exam_payload(questions=bad_q))
        self.assertEqual(r.status_code, 400)

    def test_full_flow_scores_and_misuse(self):
        self.make_users()
        eid = self.create_exam()
        d = self.start(eid)
        # no correct answers leak to the student
        for q in d["questions"]:
            self.assertNotIn("correct", q)
        qs = {q["qtype"]: q["id"] for q in d["questions"]}
        aid = d["attempt_id"]
        self.assertEqual(self.jpost(self.st, f"/api/attempts/{aid}/answer", dict(question_id=qs["mcq"], answer="1")).status_code, 200)
        self.assertEqual(self.jpost(self.st, f"/api/attempts/{aid}/answer", dict(question_id=qs["numeric"], answer="9.8")).status_code, 200)

        for vt in ("phone", "multiple_faces", "looking_away", "tab_switch"):
            r = self.jpost(self.st, f"/api/attempts/{aid}/violation",
                           dict(type=vt, detail="test", screenshot=jpeg_b64() if vt != "tab_switch" else None))
            self.assertEqual(r.status_code, 200)
        r = self.jpost(self.st, f"/api/attempts/{aid}/violation", dict(type="bogus"))
        self.assertEqual(r.status_code, 400)

        r = self.jpost(self.st, f"/api/attempts/{aid}/submit", dict(answers={str(qs["text"]): "Mass attracts mass"}))
        self.assertEqual(r.status_code, 200)

        with self.app.app_context():
            from db import get_db
            a = get_db().one("SELECT * FROM attempts WHERE id=?", (aid,))
        self.assertEqual(a["status"], "submitted")
        self.assertEqual(a["score"], 5.0)            # mcq 2 + numeric 3, text pending
        self.assertEqual(a["pending"], 1)
        pts = 15 + 10 + 4 + 5
        _, pct, lvl = scoring.compute_misuse({"phone": 1, "multiple_faces": 1, "looking_away": 1, "tab_switch": 1})
        self.assertEqual(a["misuse_pct"], pct)
        self.assertEqual(a["violation_count"], 4)
        self.assertEqual(lvl, a["risk_level"])
        self.assertGreater(pct, 0)

        # Student can't answer after submit
        r = self.jpost(self.st, f"/api/attempts/{aid}/answer", dict(question_id=qs["mcq"], answer="0"))
        self.assertEqual(r.status_code, 409)
        # Student can't restart
        r = self.jpost(self.st, f"/api/exams/{eid}/start", {})
        self.assertEqual(r.status_code, 409)

        # examiner dashboards
        self.assertEqual(self.ex.get("/examiner").status_code, 200)
        r = self.ex.get(f"/examiner/exams/{eid}"); self.assertEqual(r.status_code, 200)
        self.assertIn(b"Alice", r.data)
        r = self.ex.get(f"/examiner/attempts/{aid}"); self.assertEqual(r.status_code, 200)
        self.assertIn(b"Evidence timeline", r.data)
        self.assertIn(b"Mobile phone detected", r.data)

        # evidence image served to examiner only
        with self.app.app_context():
            vid = get_db().one("SELECT id FROM violations WHERE screenshot IS NOT NULL LIMIT 1")["id"]
        r = self.ex.get(f"/evidence/{vid}.jpg"); self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data[:3], b"\xff\xd8\xff")
        self.assertEqual(self.st.get(f"/evidence/{vid}.jpg").status_code, 403)

        # manual grading of the descriptive answer
        with self.app.app_context():
            tq = get_db().one("SELECT id FROM questions WHERE qtype='text'")["id"]
        r = self.ex.post(f"/examiner/attempts/{aid}/grade", data={"csrf_token": self.token(self.ex),
                         f"marks_{tq}": "4", f"feedback_{tq}": "Good"})
        self.assertEqual(r.status_code, 302)
        with self.app.app_context():
            a = get_db().one("SELECT score, pending FROM attempts WHERE id=?", (aid,))
        self.assertEqual((a["score"], a["pending"]), (9.0, 0))

        # PDFs
        r = self.ex.get(f"/examiner/attempts/{aid}/report.pdf")
        self.assertEqual(r.status_code, 200); self.assertEqual(r.data[:5], b"%PDF-")
        self.assertGreater(len(r.data), 3000)
        r = self.ex.get(f"/examiner/exams/{eid}/report.pdf")
        self.assertEqual(r.status_code, 200); self.assertEqual(r.data[:5], b"%PDF-")

        # student result page hides misuse info
        r = self.st.get(f"/student/attempts/{aid}"); self.assertEqual(r.status_code, 200)
        self.assertNotIn(b"misuse", r.data.lower())

        # retake
        r = self.ex.post(f"/examiner/attempts/{aid}/reset", data={"csrf_token": self.token(self.ex)})
        self.assertEqual(r.status_code, 302)
        self.start(eid)

    def test_isolation_between_examiners_and_students(self):
        self.make_users()
        eid = self.create_exam()
        other = self.app.test_client()
        self.register(other, "Prof Y", "y@x.com", "examiner"); self.login(other, "y@x.com")
        self.assertEqual(other.get(f"/examiner/exams/{eid}").status_code, 404)
        self.assertEqual(other.get(f"/examiner/exams/{eid}/report.pdf").status_code, 404)
        # unassigned student cannot see/start the exam
        s2 = self.app.test_client()
        self.register(s2, "Bob", "bob@x.com", "student"); self.login(s2, "bob@x.com")
        self.assertEqual(s2.get(f"/exam/{eid}/take").status_code, 404)
        r = self.jpost(s2, f"/api/exams/{eid}/start", {}); self.assertEqual(r.status_code, 404)
        # Bob cannot touch Alice's attempt
        aid = self.start(eid)["attempt_id"]
        r = self.jpost(s2, f"/api/attempts/{aid}/violation", dict(type="phone")); self.assertEqual(r.status_code, 404)

    def test_unpublished_upcoming_closed(self):
        self.make_users()
        now = now_utc()
        e1 = self.create_exam(published=False)
        self.assertEqual(self.jpost(self.st, f"/api/exams/{e1}/start", {}).status_code, 404)
        e2 = self.create_exam(start_at=to_iso(now + timedelta(hours=1)), end_at=to_iso(now + timedelta(hours=3)))
        self.assertEqual(self.jpost(self.st, f"/api/exams/{e2}/start", {}).status_code, 403)
        e3 = self.create_exam(start_at=to_iso(now - timedelta(hours=3)), end_at=to_iso(now - timedelta(hours=1)))
        self.assertEqual(self.jpost(self.st, f"/api/exams/{e3}/start", {}).status_code, 403)

    def test_resume_keeps_deadline_and_answers(self):
        self.make_users()
        eid = self.create_exam()
        d1 = self.start(eid)
        q = d1["questions"][0]["id"]
        self.jpost(self.st, f"/api/attempts/{d1['attempt_id']}/answer", dict(question_id=q, answer="1"))
        d2 = self.start(eid)
        self.assertEqual(d1["attempt_id"], d2["attempt_id"])
        self.assertEqual(d1["deadline"], d2["deadline"])
        self.assertEqual(d2["answers"][str(q)], "1")

    def test_expired_attempt_auto_submitted(self):
        self.make_users()
        eid = self.create_exam()
        d = self.start(eid)
        with self.app.app_context():
            from db import get_db
            db = get_db()
            db.execute("UPDATE attempts SET deadline_at=? WHERE id=?", (to_iso(now_utc() - timedelta(minutes=5)), d["attempt_id"]))
            db.commit()
        r = self.jpost(self.st, f"/api/attempts/{d['attempt_id']}/answer", dict(question_id=d["questions"][0]["id"], answer="1"))
        self.assertEqual(r.status_code, 409)
        with self.app.app_context():
            st = get_db().one("SELECT status FROM attempts WHERE id=?", (d["attempt_id"],))["status"]
        self.assertEqual(st, "auto_submitted")

    def test_examiner_view_finalizes_abandoned_attempt(self):
        self.make_users()
        eid = self.create_exam()
        d = self.start(eid)
        with self.app.app_context():
            from db import get_db
            db = get_db()
            db.execute("UPDATE attempts SET deadline_at=? WHERE id=?", (to_iso(now_utc() - timedelta(minutes=5)), d["attempt_id"]))
            db.commit()
        self.assertEqual(self.ex.get(f"/examiner/exams/{eid}").status_code, 200)
        with self.app.app_context():
            st = get_db().one("SELECT status FROM attempts WHERE id=?", (d["attempt_id"],))["status"]
        self.assertEqual(st, "auto_submitted")

    def test_violation_limit_terminates(self):
        self.make_users()
        eid = self.create_exam(max_violations=2)
        d = self.start(eid); aid = d["attempt_id"]
        r1 = self.jpost(self.st, f"/api/attempts/{aid}/violation", dict(type="phone")).get_json()
        self.assertFalse(r1["terminated"])
        r2 = self.jpost(self.st, f"/api/attempts/{aid}/violation", dict(type="talking")).get_json()
        self.assertTrue(r2["terminated"])
        with self.app.app_context():
            from db import get_db
            self.assertEqual(get_db().one("SELECT status FROM attempts WHERE id=?", (aid,))["status"], "terminated")

    def test_server_side_dedupe_and_bad_screenshot(self):
        self.make_users()
        app = create_app({"TESTING": True, "DATABASE_URL": f"sqlite:///{self.dbfile}", "SECRET_KEY": "t",
                          "VIOLATION_COOLDOWN_SEC": 60})
        st = app.test_client(); ex = app.test_client()
        self.login(ex, "prof@x.com"); self.login(st, "alice@x.com")
        r = ex.post("/examiner/api/exams", json=self.exam_payload(), headers={"X-CSRF-Token": self.token(ex)})
        eid = int(r.get_json()["redirect"].rstrip("/").split("/")[-1])
        aid = st.post(f"/api/exams/{eid}/start", json={}, headers={"X-CSRF-Token": self.token(st)}).get_json()["attempt_id"]
        h = {"X-CSRF-Token": self.token(st)}
        self.assertNotIn("deduped", st.post(f"/api/attempts/{aid}/violation", json=dict(type="phone"), headers=h).get_json())
        self.assertTrue(st.post(f"/api/attempts/{aid}/violation", json=dict(type="phone"), headers=h).get_json()["deduped"])
        # garbage "screenshot" is dropped, violation still recorded
        r = st.post(f"/api/attempts/{aid}/violation", json=dict(type="talking", screenshot="data:image/jpeg;base64,AAAA"), headers=h)
        self.assertEqual(r.status_code, 200)
        with app.app_context():
            from db import get_db
            row = get_db().one("SELECT screenshot FROM violations WHERE vtype='talking'")
        self.assertIsNone(row["screenshot"])

    def test_edit_locked_after_attempts(self):
        self.make_users()
        eid = self.create_exam()
        self.start(eid)
        p = self.exam_payload(title="Renamed", questions=[dict(qtype="text", text="new", marks=1)])
        r = self.jpost(self.ex, f"/examiner/api/exams/{eid}", p)
        self.assertEqual(r.status_code, 200)
        with self.app.app_context():
            from db import get_db
            n = get_db().one("SELECT COUNT(*) AS n FROM questions WHERE exam_id=?", (eid,))["n"]
            title = get_db().one("SELECT title FROM exams WHERE id=?", (eid,))["title"]
        self.assertEqual(n, 3)  # questions untouched
        self.assertEqual(title, "Renamed")
        self.assertEqual(self.ex.get(f"/examiner/exams/{eid}/edit").status_code, 200)

    def test_delete_exam_cascades(self):
        self.make_users()
        eid = self.create_exam()
        aid = self.start(eid)["attempt_id"]
        self.jpost(self.st, f"/api/attempts/{aid}/violation", dict(type="phone", screenshot=jpeg_b64()))
        r = self.ex.post(f"/examiner/exams/{eid}/delete", data={"csrf_token": self.token(self.ex)})
        self.assertEqual(r.status_code, 302)
        with self.app.app_context():
            from db import get_db
            for t in ("exams", "questions", "attempts", "violations", "attempt_answers", "assignments"):
                self.assertEqual(get_db().one(f"SELECT COUNT(*) AS n FROM {t}")["n"], 0, t)

    def test_pages_render(self):
        self.make_users()
        eid = self.create_exam()
        for c, url in [(self.ex, "/examiner/exams/new"), (self.ex, f"/examiner/exams/{eid}/edit"),
                       (self.st, f"/exam/{eid}/take"), (self.ex, "/"), (self.ex, f"/examiner/api/exams/{eid}/live")]:
            self.assertIn(c.get(url).status_code, (200, 302), url)
        self.assertEqual(self.app.test_client().get("/").status_code, 200)


class ScoringTests(unittest.TestCase):
    def test_monotonic_bounded_and_capped(self):
        prev = -1
        for n in range(0, 50):
            _, pct, _ = scoring.compute_misuse({"phone": n})
            self.assertGreaterEqual(pct, prev); prev = pct
            self.assertLessEqual(pct, 100)
        self.assertEqual(scoring.compute_misuse({"phone": 4})[1], scoring.compute_misuse({"phone": 40})[1])  # cap
        self.assertEqual(scoring.compute_misuse({})[1], 0)
        self.assertEqual(scoring.compute_misuse({"unknown": 5})[1], 0)

    def test_levels(self):
        self.assertEqual(scoring.risk_level(10), "Low")
        self.assertEqual(scoring.risk_level(25), "Medium")
        self.assertEqual(scoring.risk_level(50), "High")
        self.assertEqual(scoring.compute_misuse({"phone": 1})[2], "Low")       # 22%
        self.assertEqual(scoring.compute_misuse({"phone": 2})[2], "Medium")    # 39%
        self.assertEqual(scoring.compute_misuse({"phone": 4})[2], "High")      # 63%


if __name__ == "__main__":
    unittest.main()
