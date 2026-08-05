"""
Data layer for PatchAlert.

Durability note: this now supports a real Postgres database (via a
DATABASE_URL environment variable — e.g. a free Render Postgres instance)
and falls back to the original local SQLite file if DATABASE_URL isn't
set. This matters because a web service's own local disk on most free
hosting tiers (including Render's free plan) is NOT guaranteed to survive
every redeploy — your subscriber list should not live only there. Local
SQLite is kept as the fallback so this still runs with zero setup while
you're testing, but before you have real paying subscribers, set
DATABASE_URL to a real Postgres instance and this switches over
automatically with no code changes needed.

To set this up: in Render, create a new "Postgres" instance (free tier is
fine to start), then add its "Internal Database URL" as the DATABASE_URL
environment variable on this web service. Nothing else to do — the first
request after that will create all the tables fresh.
"""

import os
import secrets
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

DATABASE_URL = os.environ.get("DATABASE_URL")
USE_POSTGRES = bool(DATABASE_URL)

if USE_POSTGRES:
    import psycopg2
    import psycopg2.extras

DB_PATH = Path(__file__).parent / "alerts.db"

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS subscribers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    postcode TEXT NOT NULL,
    radius_km REAL DEFAULT 3.0,
    keywords TEXT NOT NULL,
    frequency TEXT DEFAULT 'daily',
    subscription_status TEXT DEFAULT 'trial',
    plan TEXT DEFAULT 'basic',
    access_token TEXT,
    referred_by TEXT,
    referred_count INTEGER DEFAULT 0,
    phone TEXT,
    sms_opt_in TEXT DEFAULT '0',
    sizes TEXT DEFAULT 'small,medium,large',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS patches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subscriber_id INTEGER NOT NULL,
    postcode TEXT NOT NULL,
    radius_km REAL DEFAULT 3.0,
    keywords TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (subscriber_id) REFERENCES subscribers (id)
);

CREATE TABLE IF NOT EXISTS sent_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subscriber_id INTEGER NOT NULL,
    application_uid TEXT NOT NULL,
    address TEXT,
    description TEXT,
    link TEXT,
    stage TEXT,
    sent_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(subscriber_id, application_uid),
    FOREIGN KEY (subscriber_id) REFERENCES subscribers (id)
);

CREATE TABLE IF NOT EXISTS lead_status (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subscriber_id INTEGER NOT NULL,
    application_uid TEXT NOT NULL,
    status TEXT DEFAULT 'new',
    note TEXT DEFAULT '',
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(subscriber_id, application_uid)
);

CREATE TABLE IF NOT EXISTS request_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ip TEXT NOT NULL,
    route TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""

POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS subscribers (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    postcode TEXT NOT NULL,
    radius_km REAL DEFAULT 3.0,
    keywords TEXT NOT NULL,
    frequency TEXT DEFAULT 'daily',
    subscription_status TEXT DEFAULT 'trial',
    plan TEXT DEFAULT 'basic',
    access_token TEXT,
    referred_by TEXT,
    referred_count INTEGER DEFAULT 0,
    phone TEXT,
    sms_opt_in TEXT DEFAULT '0',
    sizes TEXT DEFAULT 'small,medium,large',
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS patches (
    id SERIAL PRIMARY KEY,
    subscriber_id INTEGER NOT NULL REFERENCES subscribers (id),
    postcode TEXT NOT NULL,
    radius_km REAL DEFAULT 3.0,
    keywords TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS sent_alerts (
    id SERIAL PRIMARY KEY,
    subscriber_id INTEGER NOT NULL REFERENCES subscribers (id),
    application_uid TEXT NOT NULL,
    address TEXT,
    description TEXT,
    link TEXT,
    stage TEXT,
    sent_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(subscriber_id, application_uid)
);

CREATE TABLE IF NOT EXISTS lead_status (
    id SERIAL PRIMARY KEY,
    subscriber_id INTEGER NOT NULL,
    application_uid TEXT NOT NULL,
    status TEXT DEFAULT 'new',
    note TEXT DEFAULT '',
    updated_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(subscriber_id, application_uid)
);

CREATE TABLE IF NOT EXISTS request_log (
    id SERIAL PRIMARY KEY,
    ip TEXT NOT NULL,
    route TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT NOW()
);
"""

# Columns added after the original launch. SQLite has no "ADD COLUMN IF NOT
# EXISTS", so these are applied one at a time and any "already exists"
# error is silently ignored — this lets an existing alerts.db (from before
# this update) pick up the new columns without losing its data. Postgres
# supports "IF NOT EXISTS" directly, so a fresh Postgres database created
# from POSTGRES_SCHEMA above never even hits this path with real work to do.
MIGRATIONS = [
    "ALTER TABLE subscribers ADD COLUMN plan TEXT DEFAULT 'basic'",
    "ALTER TABLE subscribers ADD COLUMN access_token TEXT",
    "ALTER TABLE subscribers ADD COLUMN referred_by TEXT",
    "ALTER TABLE subscribers ADD COLUMN referred_count INTEGER DEFAULT 0",
    "ALTER TABLE subscribers ADD COLUMN phone TEXT",
    "ALTER TABLE subscribers ADD COLUMN sms_opt_in TEXT DEFAULT '0'",
    "ALTER TABLE subscribers ADD COLUMN sizes TEXT DEFAULT 'small,medium,large'",
    "ALTER TABLE sent_alerts ADD COLUMN address TEXT",
    "ALTER TABLE sent_alerts ADD COLUMN description TEXT",
    "ALTER TABLE sent_alerts ADD COLUMN link TEXT",
    "ALTER TABLE sent_alerts ADD COLUMN stage TEXT",
]

# The primary patch (postcode/radius_km/keywords stored directly on the
# subscriber row, set at signup) is always included and doesn't count
# against this — this caps how many EXTRA patches a "pro" subscriber can
# add via /account/<token>.
MAX_ADDITIONAL_PATCHES = 4


def get_conn():
    if USE_POSTGRES:
        return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _exec(conn, sql, params=()):
    """Runs a query on either backend. Queries in this file are written
    with '?' placeholders (SQLite style); on Postgres they're translated
    to '%s' automatically so there's only one copy of each query to
    maintain."""
    if USE_POSTGRES:
        sql = sql.replace("?", "%s")
    cur = conn.cursor()
    cur.execute(sql, params)
    return cur


def parse_timestamp(value):
    """Returns a datetime whether the stored value came back as a string
    (SQLite) or already as a datetime object (Postgres)."""
    if value is None:
        return None
    if isinstance(value, str):
        return datetime.strptime(value.split(".")[0], "%Y-%m-%d %H:%M:%S")
    return value


def init_db():
    conn = get_conn()
    conn.executescript(SQLITE_SCHEMA) if not USE_POSTGRES else _run_postgres_schema(conn)
    if not USE_POSTGRES:
        for stmt in MIGRATIONS:
            try:
                conn.execute(stmt)
                conn.commit()
            except sqlite3.OperationalError:
                pass  # column already exists from a previous run
    else:
        # Postgres: apply the same columns with IF NOT EXISTS, harmless if
        # POSTGRES_SCHEMA already created them.
        for stmt in [
            "ALTER TABLE subscribers ADD COLUMN IF NOT EXISTS plan TEXT DEFAULT 'basic'",
            "ALTER TABLE subscribers ADD COLUMN IF NOT EXISTS access_token TEXT",
            "ALTER TABLE subscribers ADD COLUMN IF NOT EXISTS referred_by TEXT",
            "ALTER TABLE subscribers ADD COLUMN IF NOT EXISTS referred_count INTEGER DEFAULT 0",
            "ALTER TABLE subscribers ADD COLUMN IF NOT EXISTS phone TEXT",
            "ALTER TABLE subscribers ADD COLUMN IF NOT EXISTS sms_opt_in TEXT DEFAULT '0'",
            "ALTER TABLE subscribers ADD COLUMN IF NOT EXISTS sizes TEXT DEFAULT 'small,medium,large'",
            "ALTER TABLE sent_alerts ADD COLUMN IF NOT EXISTS address TEXT",
            "ALTER TABLE sent_alerts ADD COLUMN IF NOT EXISTS description TEXT",
            "ALTER TABLE sent_alerts ADD COLUMN IF NOT EXISTS link TEXT",
            "ALTER TABLE sent_alerts ADD COLUMN IF NOT EXISTS stage TEXT",
        ]:
            cur = conn.cursor()
            cur.execute(stmt)
        conn.commit()
    conn.close()


def _run_postgres_schema(conn):
    cur = conn.cursor()
    cur.execute(POSTGRES_SCHEMA)
    conn.commit()


def add_subscriber(data: dict) -> int:
    conn = get_conn()
    new_token = secrets.token_urlsafe(16)
    # Deliberately NOT setting referred_by here even on first insert — it's
    # left NULL and picked up by the "credit a referral once" block below
    # instead, which is what actually increments the referrer's count. If
    # this INSERT set it directly, the block below would always see it as
    # "already credited" and silently never increment anyone's count.
    _exec(
        conn,
        """INSERT INTO subscribers
               (name, email, postcode, radius_km, keywords, frequency, plan, access_token, sizes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(email) DO UPDATE SET
             postcode=excluded.postcode, radius_km=excluded.radius_km,
             keywords=excluded.keywords, frequency=excluded.frequency,
             sizes=excluded.sizes""",
        (
            data["name"], data["email"], data["postcode"],
            data.get("radius_km", 3.0), data["keywords"], data.get("frequency", "daily"),
            data.get("plan", "basic"), new_token, data.get("sizes", "small,medium,large"),
        ),
    )
    conn.commit()

    cur = _exec(conn, "SELECT id, access_token, referred_by FROM subscribers WHERE email = ?", (data["email"],))
    row = dict(cur.fetchone())
    subscriber_id = row["id"]

    # Backfill a token for any subscriber created before access_token existed.
    if not row.get("access_token"):
        _exec(conn, "UPDATE subscribers SET access_token = ? WHERE id = ?", (new_token, subscriber_id))
        conn.commit()

    # Credit a referral once, the first time this subscriber signs up.
    referred_by = data.get("referred_by")
    if referred_by and not row.get("referred_by") and referred_by != row.get("access_token"):
        _exec(conn, "UPDATE subscribers SET referred_by = ? WHERE id = ?", (referred_by, subscriber_id))
        _exec(conn, "UPDATE subscribers SET referred_count = referred_count + 1 WHERE access_token = ?", (referred_by,))
        conn.commit()

    conn.close()
    return subscriber_id


def get_all_subscribers() -> list:
    """Only returns subscribers who haven't unsubscribed — this is what
    the daily digest job iterates over."""
    conn = get_conn()
    cur = _exec(conn, "SELECT * FROM subscribers WHERE subscription_status != 'unsubscribed'")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def get_subscriber(subscriber_id) -> dict | None:
    conn = get_conn()
    cur = _exec(conn, "SELECT * FROM subscribers WHERE id = ?", (subscriber_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def get_subscriber_by_token(token: str) -> dict | None:
    conn = get_conn()
    cur = _exec(conn, "SELECT * FROM subscribers WHERE access_token = ?", (token,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def unsubscribe(token: str) -> bool:
    conn = get_conn()
    cur = _exec(conn, "UPDATE subscribers SET subscription_status = 'unsubscribed' WHERE access_token = ?", (token,))
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


def set_plan(subscriber_id, plan: str, status: str = "active"):
    conn = get_conn()
    _exec(conn, "UPDATE subscribers SET plan = ?, subscription_status = ? WHERE id = ?", (plan, status, subscriber_id))
    conn.commit()
    conn.close()


def set_plan_by_email(email: str, plan: str, status: str = "active"):
    """Used by the Stripe webhook — at checkout time we only reliably know
    the customer's email, not their subscriber id."""
    conn = get_conn()
    _exec(conn, "UPDATE subscribers SET plan = ?, subscription_status = ? WHERE email = ?", (plan, status, email))
    conn.commit()
    conn.close()


def get_additional_patches(subscriber_id) -> list:
    """Extra patches beyond the subscriber's original signup search (which
    stays on the subscribers row itself as their "primary" patch)."""
    conn = get_conn()
    cur = _exec(
        conn,
        "SELECT id, postcode, radius_km, keywords FROM patches WHERE subscriber_id = ? ORDER BY id",
        (subscriber_id,),
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def count_additional_patches(subscriber_id) -> int:
    conn = get_conn()
    cur = _exec(conn, "SELECT COUNT(*) AS c FROM patches WHERE subscriber_id = ?", (subscriber_id,))
    row = dict(cur.fetchone())
    conn.close()
    return row["c"]


def add_patch(subscriber_id, postcode: str, radius_km: float, keywords: str) -> bool:
    """Returns False without inserting anything if the subscriber is
    already at MAX_ADDITIONAL_PATCHES — enforced here (not just in the
    route) so this stays safe to call from anywhere."""
    if count_additional_patches(subscriber_id) >= MAX_ADDITIONAL_PATCHES:
        return False
    conn = get_conn()
    _exec(
        conn,
        "INSERT INTO patches (subscriber_id, postcode, radius_km, keywords) VALUES (?, ?, ?, ?)",
        (subscriber_id, postcode, radius_km, keywords),
    )
    conn.commit()
    conn.close()
    return True


def delete_patch(patch_id, subscriber_id):
    """Scoped to subscriber_id too, so one subscriber can't delete another's
    patch by guessing an id."""
    conn = get_conn()
    _exec(conn, "DELETE FROM patches WHERE id = ? AND subscriber_id = ?", (patch_id, subscriber_id))
    conn.commit()
    conn.close()


def update_notification_settings(subscriber_id, phone: str, sms_opt_in: bool):
    conn = get_conn()
    _exec(
        conn,
        "UPDATE subscribers SET phone = ?, sms_opt_in = ? WHERE id = ?",
        (phone, "1" if sms_opt_in else "0", subscriber_id),
    )
    conn.commit()
    conn.close()


def already_sent(subscriber_id, application_uid: str) -> bool:
    conn = get_conn()
    cur = _exec(
        conn,
        "SELECT 1 FROM sent_alerts WHERE subscriber_id = ? AND application_uid = ?",
        (subscriber_id, application_uid),
    )
    row = cur.fetchone()
    conn.close()
    return row is not None


def mark_sent(subscriber_id, application_uid: str, address: str = None, description: str = None,
              link: str = None, stage: str = None):
    """address/description/link/stage are a snapshot of the application at
    the moment it was sent, stored here so the /leads dashboard can show
    something useful without needing to re-query PlanIt by uid (which
    isn't something the API supports directly — it only searches by
    location). All optional so older call sites still work."""
    conn = get_conn()
    if USE_POSTGRES:
        _exec(
            conn,
            """INSERT INTO sent_alerts (subscriber_id, application_uid, address, description, link, stage)
               VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING""",
            (subscriber_id, application_uid, address, description, link, stage),
        )
    else:
        _exec(
            conn,
            """INSERT OR IGNORE INTO sent_alerts (subscriber_id, application_uid, address, description, link, stage)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (subscriber_id, application_uid, address, description, link, stage),
        )
    conn.commit()
    conn.close()


def get_sent_alerts(subscriber_id) -> list:
    """Full history of what's been sent to a subscriber, newest first —
    backs the /leads dashboard. Entries sent before this snapshot feature
    existed will have NULL address/description/link/stage."""
    conn = get_conn()
    cur = _exec(
        conn,
        """SELECT application_uid, address, description, link, stage, sent_at
           FROM sent_alerts WHERE subscriber_id = ? ORDER BY id DESC""",
        (subscriber_id,),
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def get_lead_statuses(subscriber_id) -> dict:
    """Returns {application_uid: {"status": ..., "note": ...}} for a subscriber."""
    conn = get_conn()
    cur = _exec(conn, "SELECT application_uid, status, note FROM lead_status WHERE subscriber_id = ?", (subscriber_id,))
    result = {r["application_uid"]: {"status": r["status"], "note": r["note"]} for r in cur.fetchall()}
    conn.close()
    return result


def set_lead_status(subscriber_id, application_uid: str, status: str, note: str = ""):
    conn = get_conn()
    if USE_POSTGRES:
        _exec(
            conn,
            """INSERT INTO lead_status (subscriber_id, application_uid, status, note)
               VALUES (?, ?, ?, ?)
               ON CONFLICT (subscriber_id, application_uid) DO UPDATE SET status=excluded.status, note=excluded.note""",
            (subscriber_id, application_uid, status, note),
        )
    else:
        _exec(
            conn,
            """INSERT INTO lead_status (subscriber_id, application_uid, status, note)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(subscriber_id, application_uid) DO UPDATE SET status=excluded.status, note=excluded.note""",
            (subscriber_id, application_uid, status, note),
        )
    conn.commit()
    conn.close()


def check_rate_limit(ip: str, route: str, max_requests: int = 20, window_minutes: int = 60) -> bool:
    """Returns True if this ip is still within the allowed request rate for
    this route. Deliberately simple (no SQL date-window syntax, which
    differs between SQLite and Postgres) — pulls the most recent rows and
    counts how many fall inside the window in plain Python."""
    conn = get_conn()
    cur = _exec(
        conn,
        "SELECT created_at FROM request_log WHERE ip = ? AND route = ? ORDER BY id DESC LIMIT 200",
        (ip, route),
    )
    rows = cur.fetchall()
    conn.close()
    if not rows:
        return True
    # Both CURRENT_TIMESTAMP (SQLite) and NOW() (Postgres, UTC on Render)
    # store naive UTC values, so comparing against a naive utcnow() cutoff
    # directly (rather than converting through .timestamp(), which assumes
    # local time for naive datetimes) avoids a timezone-offset bug here.
    cutoff = datetime.utcnow() - timedelta(minutes=window_minutes)
    recent = 0
    for r in rows:
        ts = parse_timestamp(dict(r)["created_at"])
        if ts and ts > cutoff:
            recent += 1
    return recent < max_requests


def log_request(ip: str, route: str):
    conn = get_conn()
    _exec(conn, "INSERT INTO request_log (ip, route) VALUES (?, ?)", (ip, route))
    conn.commit()
    conn.close()
