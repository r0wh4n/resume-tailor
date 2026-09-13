#!/usr/bin/env python3
"""Sessions, rate limits and usage accounting.

Runs on SQLite by default (self-hosting: zero setup, one file) and on Postgres when
DATABASE_URL is set (Supabase, Render Postgres, anything). The SQL here is small and
plain enough that supporting both costs ~20 lines rather than an ORM.

Never stores an API key. BYOK keys live in memory in app.py and die with the process,
because writing someone else's OpenAI key to disk is not a thing worth doing.
"""
import os
import pathlib
import time

DATABASE_URL = os.environ.get("DATABASE_URL", "")
PG = DATABASE_URL.startswith(("postgres://", "postgresql://"))
DB = pathlib.Path(os.environ.get("DB_PATH", pathlib.Path(__file__).parent / "data.db"))

FREE_RESUMES = int(os.environ.get("FREE_RESUMES", 1))      # lifetime, per account
PAID_PER_DAY = int(os.environ.get("PAID_PER_DAY", 60))     # sanity cap, not a product limit
SESSION_TTL_DAYS = int(os.environ.get("SESSION_TTL_DAYS", 30))
PLAN_DAYS = int(os.environ.get("PLAN_DAYS", 30))           # how long one payment lasts

if PG:
    import psycopg
    from psycopg.rows import dict_row

    DDL = """
    CREATE TABLE IF NOT EXISTS sessions (
      sid TEXT PRIMARY KEY, facts TEXT NOT NULL, filename TEXT,
      created DOUBLE PRECISION NOT NULL, updated DOUBLE PRECISION NOT NULL);
    CREATE TABLE IF NOT EXISTS events (
      id BIGSERIAL PRIMARY KEY, sid TEXT NOT NULL, kind TEXT NOT NULL,
      ts DOUBLE PRECISION NOT NULL, calls INTEGER DEFAULT 0,
      tok_in INTEGER DEFAULT 0, tok_out INTEGER DEFAULT 0, score INTEGER);
    CREATE INDEX IF NOT EXISTS events_sid_ts ON events(sid, ts);
    CREATE TABLE IF NOT EXISTS users (
      uid TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT,
      plan TEXT NOT NULL DEFAULT 'free', paid_until DOUBLE PRECISION DEFAULT 0,
      created DOUBLE PRECISION NOT NULL);
    CREATE TABLE IF NOT EXISTS payments (
      id TEXT PRIMARY KEY, uid TEXT NOT NULL, amount INTEGER, currency TEXT,
      status TEXT NOT NULL, ts DOUBLE PRECISION NOT NULL);
    ALTER TABLE sessions ADD COLUMN IF NOT EXISTS uid TEXT;
    ALTER TABLE events ADD COLUMN IF NOT EXISTS uid TEXT;
    CREATE INDEX IF NOT EXISTS events_uid_ts ON events(uid, ts);
    """
else:
    import sqlite3

    DDL = """
    CREATE TABLE IF NOT EXISTS sessions (
      sid TEXT PRIMARY KEY, facts TEXT NOT NULL, filename TEXT,
      created REAL NOT NULL, updated REAL NOT NULL, uid TEXT);
    CREATE TABLE IF NOT EXISTS events (
      id INTEGER PRIMARY KEY AUTOINCREMENT, sid TEXT NOT NULL, kind TEXT NOT NULL,
      ts REAL NOT NULL, calls INTEGER DEFAULT 0,
      tok_in INTEGER DEFAULT 0, tok_out INTEGER DEFAULT 0, score INTEGER, uid TEXT);
    CREATE INDEX IF NOT EXISTS events_sid_ts ON events(sid, ts);
    CREATE INDEX IF NOT EXISTS events_uid_ts ON events(uid, ts);
    CREATE TABLE IF NOT EXISTS users (
      uid TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT,
      plan TEXT NOT NULL DEFAULT 'free', paid_until REAL DEFAULT 0,
      created REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS payments (
      id TEXT PRIMARY KEY, uid TEXT NOT NULL, amount INTEGER, currency TEXT,
      status TEXT NOT NULL, ts REAL NOT NULL);
    """


def connect():
    if PG:
        return psycopg.connect(DATABASE_URL, row_factory=dict_row, autocommit=True)
    c = sqlite3.connect(DB, timeout=15)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=15000")
    c.row_factory = sqlite3.Row
    return c


def run(sql, args=(), fetch=None):
    """One query. SQL is written with '?' and translated for Postgres."""
    if PG:
        sql = sql.replace("?", "%s")
    with connect() as c:
        cur = c.cursor()
        cur.execute(sql, args)
        if fetch == "one":
            row = cur.fetchone()
            return dict(row) if row else None
        if fetch == "all":
            return [dict(r) for r in cur.fetchall()]
        return None


def init():
    with connect() as c:
        if PG:
            with c.cursor() as cur:
                for stmt in filter(str.strip, DDL.split(";")):
                    cur.execute(stmt)
        else:
            c.executescript(DDL)


# ---------------------------------------------------------------- accounts

def upsert_user(uid, email, name):
    """Find or create by email. The provider's subject id is only used for new rows,
    so someone who signs in with Google today and another provider later keeps one
    account and one quota."""
    row = run("SELECT uid, plan, paid_until FROM users WHERE email=?", (email,), "one")
    if row:
        run("UPDATE users SET name=? WHERE uid=?", (name, row["uid"]))
        return row["uid"]
    run("INSERT INTO users (uid, email, name, plan, paid_until, created) VALUES (?,?,?,?,?,?)",
        (uid, email, name, "free", 0, time.time()))
    return uid


def get_user(uid):
    if not uid:
        return None
    return run("SELECT uid, email, name, plan, paid_until FROM users WHERE uid=?", (uid,), "one")


def is_paid(user):
    return bool(user) and user["plan"] == "paid" and (user["paid_until"] or 0) > time.time()


def mark_paid(uid, days=None):
    until = time.time() + (days or PLAN_DAYS) * 86400
    run("UPDATE users SET plan='paid', paid_until=? WHERE uid=?", (until, uid))
    return until


def record_payment(pid, uid, amount, currency, status):
    """Idempotent: a webhook that fires twice must not extend the plan twice."""
    if run("SELECT id FROM payments WHERE id=?", (pid,), "one"):
        return False
    run("INSERT INTO payments (id, uid, amount, currency, status, ts) VALUES (?,?,?,?,?,?)",
        (pid, uid, amount, currency, status, time.time()))
    return True


def resumes_used(uid):
    """Lifetime résumés generated by this account - the free tier is not a daily one."""
    row = run("SELECT COUNT(*) AS n FROM events WHERE uid=? AND kind IN ('tailor','fix')",
              (uid,), "one")
    return row["n"] if row else 0


def quota(user, own_key=False):
    """(allowed, used, limit, reason). Own key = you pay OpenAI, so no limit here."""
    if own_key:
        return True, 0, None, ""
    if not user:
        return False, 0, FREE_RESUMES, "sign-in"
    if is_paid(user):
        used = run("SELECT COUNT(*) AS n FROM events WHERE uid=? AND kind IN ('tailor','fix') AND ts > ?",
                   (user["uid"], time.time() - 86400), "one")["n"]
        if used >= PAID_PER_DAY:
            return False, used, PAID_PER_DAY, "daily"
        return True, used, PAID_PER_DAY, ""
    used = resumes_used(user["uid"])
    if used >= FREE_RESUMES:
        return False, used, FREE_RESUMES, "upgrade"
    return True, used, FREE_RESUMES, ""


# ---------------------------------------------------------------- sessions

def put_facts(sid, facts, filename, uid=None):
    now = time.time()
    run("""INSERT INTO sessions (sid, facts, filename, created, updated, uid)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(sid) DO UPDATE SET facts=excluded.facts,
             filename=excluded.filename, updated=excluded.updated, uid=excluded.uid""",
        (sid, facts, filename, now, now, uid))


def get_facts(sid):
    if not sid:
        return None
    return run("SELECT facts, filename FROM sessions WHERE sid=?", (sid,), "one")


def append_fact(sid, line):
    run("UPDATE sessions SET facts = facts || ?, updated = ? WHERE sid = ?",
        (f"\n- {line}", time.time(), sid))


def drop(sid):
    if sid:
        run("DELETE FROM sessions WHERE sid=?", (sid,))


def record(sid, kind, calls=0, tok_in=0, tok_out=0, score=None, uid=None):
    run("""INSERT INTO events (sid, kind, ts, calls, tok_in, tok_out, score, uid)
           VALUES (?,?,?,?,?,?,?,?)""",
        (sid, kind, time.time(), calls, tok_in, tok_out, score, uid))


def totals(days=1):
    return run("""SELECT COUNT(*) AS events, COUNT(DISTINCT sid) AS sessions,
                         COALESCE(SUM(calls),0) AS calls,
                         COALESCE(SUM(tok_in),0) AS tok_in,
                         COALESCE(SUM(tok_out),0) AS tok_out
                  FROM events WHERE ts > ?""", (time.time() - days * 86400,), "one")


def sweep():
    """Drop expired sessions. Résumés are personal data; don't hoard them."""
    cutoff = time.time() - SESSION_TTL_DAYS * 86400
    run("DELETE FROM sessions WHERE updated < ?", (cutoff,))
    run("DELETE FROM events WHERE ts < ?", (cutoff,))


if __name__ == "__main__":
    import sys
    init()
    print("backend:", "postgres" if PG else f"sqlite ({DB})")
    if "sweep" in sys.argv:
        sweep()
        print("expired sessions removed")
    print("users:", run("SELECT COUNT(*) AS n FROM users", (), "one"))
    print("today:", totals(1))
    print("30d:  ", totals(30))
