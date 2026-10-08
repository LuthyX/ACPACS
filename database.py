"""
database.py
ACPACS — Database Setup

v2.0 — schema aligned to the report's methodology chapter.

Changes from v1.1:
  1. sessions.fingerprint_template is now a BLOB, per S3.4.1 Table 3.3.
     The binary minutiae template captured at entry lives in the session
     row itself, so the exit check is one single-row lookup and does not
     depend on how many vehicles are parked (S3.3.2.2).
  2. sessions.name — nullable driver name, per Table 3.3.
  3. transactions renamed to payments, per S3.4.1 Table 3.4.
     NOTE: Table 3.4 in the report lists only Payment_ID, Session_ID,
     Fee_Amount, Payment_Mode and Paid_At. duration_minutes and
     billable_hours are kept here because Chapter 4 needs them to
     evidence the billing metric. Update Table 3.4 to match.
  4. fingerprint_id and the old transactions table are gone. There is no
     migration path from v1.x — delete acpacs.db and start clean.

Tables (S3.4: "The only two database tables present"):
  - sessions: one row per parking visit, holds the bound template
  - payments: one row per closed session
"""

import sqlite3

DB_PATH = "acpacs.db"


def get_connection():
    """Get a database connection. Returns rows as dictionaries."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # access columns by name e.g. row["plate"]
    return conn


def init_db():
    """Create the two tables if they don't exist yet."""
    conn = get_connection()
    cursor = conn.cursor()

    # Parking sessions — report Table 3.3
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plate TEXT NOT NULL,
            fingerprint_template BLOB NOT NULL,  -- minutiae template bound at entry
            name TEXT,                           -- optional, no effect on auth
            entry_time TEXT NOT NULL,
            exit_time TEXT,
            status TEXT NOT NULL DEFAULT 'active'
        )
    """)

    # A plate may only hold one active session at a time. This is the
    # race-condition guard S3.5 relies on for multi-lane deployments:
    # the database refuses a duplicate rather than the application layer
    # having to win a check-then-insert race.
    cursor.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_session
        ON sessions (plate) WHERE status = 'active'
    """)

    # Payments — report Table 3.4
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            plate TEXT NOT NULL,
            entry_time TEXT NOT NULL,
            exit_time TEXT NOT NULL,
            duration_minutes INTEGER NOT NULL,
            billable_hours INTEGER NOT NULL,     -- ceil(duration_minutes / 60)
            fee_amount REAL NOT NULL,
            payment_mode TEXT NOT NULL DEFAULT 'CASHLESS',
            paid_at TEXT NOT NULL,
            FOREIGN KEY (session_id) REFERENCES sessions(id)
        )
    """)

    conn.commit()
    conn.close()
    print("Database initialized: acpacs.db")


if __name__ == "__main__":
    init_db()