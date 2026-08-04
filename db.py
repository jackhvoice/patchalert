import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "alerts.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS subscribers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    postcode TEXT NOT NULL,
    radius_km REAL DEFAULT 3.0,
    keywords TEXT NOT NULL,      -- comma separated, e.g. "extension,loft conversion,garage"
    frequency TEXT DEFAULT 'daily',
    subscription_status TEXT DEFAULT 'trial',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sent_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subscriber_id INTEGER NOT NULL,
    application_uid TEXT NOT NULL,
    sent_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(subscriber_id, application_uid),
    FOREIGN KEY (subscriber_id) REFERENCES subscribers (id)
);
"""


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_conn()
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


def add_subscriber(data: dict) -> int:
    conn = get_conn()
    cur = conn.execute(
        """INSERT INTO subscribers (name, email, postcode, radius_km, keywords, frequency)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(email) DO UPDATE SET
             postcode=excluded.postcode, radius_km=excluded.radius_km,
             keywords=excluded.keywords, frequency=excluded.frequency""",
        (
            data["name"], data["email"], data["postcode"],
            data.get("radius_km", 3.0), data["keywords"], data.get("frequency", "daily"),
        ),
    )
    conn.commit()
    cur = conn.execute("SELECT id FROM subscribers WHERE email = ?", (data["email"],))
    subscriber_id = cur.fetchone()["id"]
    conn.close()
    return subscriber_id


def get_all_subscribers() -> list[dict]:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM subscribers").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_subscriber(subscriber_id: int) -> dict | None:
    conn = get_conn()
    row = conn.execute("SELECT * FROM subscribers WHERE id = ?", (subscriber_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def already_sent(subscriber_id: int, application_uid: str) -> bool:
    conn = get_conn()
    row = conn.execute(
        "SELECT 1 FROM sent_alerts WHERE subscriber_id = ? AND application_uid = ?",
        (subscriber_id, application_uid),
    ).fetchone()
    conn.close()
    return row is not None


def mark_sent(subscriber_id: int, application_uid: str):
    conn = get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO sent_alerts (subscriber_id, application_uid) VALUES (?, ?)",
        (subscriber_id, application_uid),
    )
    conn.commit()
    conn.close()
