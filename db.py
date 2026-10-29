"""
Tiny database layer that runs on SQLite (local dev) and PostgreSQL (Render).

SQL is written once with `?` placeholders; for PostgreSQL they are translated
to `%s`. Rows come back as plain dicts. Inserts use `RETURNING id`, which works
on both engines (SQLite >= 3.35).
"""
import sqlite3
from urllib.parse import urlparse

from flask import current_app, g

try:  # psycopg2 is only needed on PostgreSQL
    import psycopg2
    import psycopg2.extras
    import psycopg2.pool
except ImportError:  # pragma: no cover
    psycopg2 = None

_pool = None
_is_pg = False
_sqlite_path = "proctor.db"


SCHEMA = [
    """CREATE TABLE IF NOT EXISTS users(
        id {PK},
        name TEXT NOT NULL,
        email TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('student','examiner')),
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS exams(
        id {PK},
        examiner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        title TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        duration_min INTEGER NOT NULL,
        start_at TEXT NOT NULL,
        end_at TEXT NOT NULL,
        published INTEGER NOT NULL DEFAULT 0,
        max_violations INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS questions(
        id {PK},
        exam_id INTEGER NOT NULL REFERENCES exams(id) ON DELETE CASCADE,
        position INTEGER NOT NULL,
        qtype TEXT NOT NULL CHECK(qtype IN ('mcq','numeric','text')),
        text TEXT NOT NULL,
        options TEXT NOT NULL DEFAULT '[]',
        correct TEXT NOT NULL DEFAULT '',
        tolerance {REAL} NOT NULL DEFAULT 0,
        marks {REAL} NOT NULL DEFAULT 1
    )""",
    """CREATE TABLE IF NOT EXISTS assignments(
        exam_id INTEGER NOT NULL REFERENCES exams(id) ON DELETE CASCADE,
        student_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        PRIMARY KEY (exam_id, student_id)
    )""",
    """CREATE TABLE IF NOT EXISTS attempts(
        id {PK},
        exam_id INTEGER NOT NULL REFERENCES exams(id) ON DELETE CASCADE,
        student_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        started_at TEXT NOT NULL,
        deadline_at TEXT NOT NULL,
        submitted_at TEXT,
        status TEXT NOT NULL DEFAULT 'in_progress',
        score {REAL} NOT NULL DEFAULT 0,
        pending INTEGER NOT NULL DEFAULT 0,
        misuse_pct {REAL} NOT NULL DEFAULT 0,
        risk_level TEXT NOT NULL DEFAULT 'Low',
        violation_count INTEGER NOT NULL DEFAULT 0,
        UNIQUE (exam_id, student_id)
    )""",
    """CREATE TABLE IF NOT EXISTS attempt_answers(
        attempt_id INTEGER NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
        question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
        answer TEXT NOT NULL DEFAULT '',
        auto_marks {REAL},
        manual_marks {REAL},
        feedback TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (attempt_id, question_id)
    )""",
    """CREATE TABLE IF NOT EXISTS violations(
        id {PK},
        attempt_id INTEGER NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
        vtype TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT '',
        occurred_at TEXT NOT NULL,
        offset_sec INTEGER NOT NULL DEFAULT 0,
        screenshot {BLOB}
    )""",
    "CREATE INDEX IF NOT EXISTS ix_exams_examiner ON exams(examiner_id)",
    "CREATE INDEX IF NOT EXISTS ix_questions_exam ON questions(exam_id, position)",
    "CREATE INDEX IF NOT EXISTS ix_assign_student ON assignments(student_id)",
    "CREATE INDEX IF NOT EXISTS ix_attempts_exam ON attempts(exam_id)",
    "CREATE INDEX IF NOT EXISTS ix_violations_attempt ON violations(attempt_id, occurred_at)",
]


def _normalize_pg_url(url):
    # Render/Heroku historically hand out postgres://, psycopg2 wants postgresql://
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return url


class DB:
    """Per-request wrapper around a raw DB-API connection."""

    def __init__(self, conn, is_pg):
        self.conn = conn
        self.is_pg = is_pg

    def _prep(self, sql):
        return sql.replace("?", "%s") if self.is_pg else sql

    def _cursor(self):
        if self.is_pg:
            return self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        return self.conn.cursor()

    @staticmethod
    def _clean(row):
        if row is None:
            return None
        d = dict(row)
        for k, v in d.items():
            if isinstance(v, memoryview):
                d[k] = bytes(v)
        return d

    def execute(self, sql, params=()):
        cur = self._cursor()
        cur.execute(self._prep(sql), tuple(params))
        return cur

    def one(self, sql, params=()):
        cur = self.execute(sql, params)
        row = cur.fetchone()
        cur.close()
        return self._clean(row)

    def all(self, sql, params=()):
        cur = self.execute(sql, params)
        rows = cur.fetchall()
        cur.close()
        return [self._clean(r) for r in rows]

    def insert(self, sql, params=()):
        """INSERT ... and return the new row id."""
        row = self.one(sql + " RETURNING id", params)
        return row["id"] if row else None

    def commit(self):
        self.conn.commit()

    def rollback(self):
        try:
            self.conn.rollback()
        except Exception:  # pragma: no cover
            pass


def _open_sqlite():
    conn = sqlite3.connect(_sqlite_path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _checkout_pg():
    """Get a live PG connection from the pool (Render drops idle ones)."""
    for _ in range(3):
        conn = _pool.getconn()
        try:
            if conn.closed:
                raise psycopg2.OperationalError("closed")
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.close()
            conn.rollback()
            return conn
        except Exception:
            try:
                _pool.putconn(conn, close=True)
            except Exception:  # pragma: no cover
                pass
    raise RuntimeError("Could not obtain a database connection")


def get_db():
    if "db" not in g:
        conn = _checkout_pg() if _is_pg else _open_sqlite()
        g.db = DB(conn, _is_pg)
    return g.db


def _close(exc):
    db = g.pop("db", None)
    if db is None:
        return
    if _is_pg:
        db.rollback()  # no-op after commit; clears aborted txns
        try:
            _pool.putconn(db.conn)
        except Exception:  # pragma: no cover
            pass
    else:
        if exc is not None:
            db.rollback()
        db.conn.close()


def _commit_after_request(response):
    db = g.get("db")
    if db is not None:
        if response.status_code < 400:
            db.commit()
        else:
            db.rollback()
    return response


def init_schema():
    pk = "SERIAL PRIMARY KEY" if _is_pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
    blob = "BYTEA" if _is_pg else "BLOB"
    real = "DOUBLE PRECISION" if _is_pg else "REAL"
    conn = _checkout_pg() if _is_pg else _open_sqlite()
    try:
        cur = conn.cursor()
        for stmt in SCHEMA:
            cur.execute(stmt.format(PK=pk, BLOB=blob, REAL=real))
        conn.commit()
        cur.close()
    finally:
        if _is_pg:
            _pool.putconn(conn)
        else:
            conn.close()


def init_app(app):
    global _pool, _is_pg, _sqlite_path
    url = app.config["DATABASE_URL"]
    if url.startswith(("postgres://", "postgresql://")):
        if psycopg2 is None:
            raise RuntimeError("psycopg2-binary is required for PostgreSQL")
        _is_pg = True
        _pool = psycopg2.pool.ThreadedConnectionPool(
            1, int(app.config.get("DB_POOL_MAX", 8)), _normalize_pg_url(url),
            connect_timeout=10, keepalives=1, keepalives_idle=30,
            keepalives_interval=10, keepalives_count=5,
        )
    elif url.startswith("sqlite"):
        _is_pg = False
        parsed = urlparse(url)
        # sqlite:///proctor.db -> relative 'proctor.db'; sqlite:////abs/x.db -> '/abs/x.db'
        path = url.split("sqlite:///", 1)[-1] or "proctor.db"
        _sqlite_path = path if parsed.scheme else "proctor.db"
    else:
        raise RuntimeError(f"Unsupported DATABASE_URL: {url.split(':')[0]}")

    app.teardown_appcontext(_close)
    app.after_request(_commit_after_request)
    init_schema()
