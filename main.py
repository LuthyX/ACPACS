"""
main.py
ACPACS — FastAPI Backend (report Chapter 3)

v2.0 — aligned to the methodology chapter.

Changes from v1.1:
  1. 1:1 template verification (S3.3.2.2). The template captured at entry
     is stored in the session row as a BLOB and compared pairwise against
     the exit scan. One single-row lookup, independent of occupancy.
  2. Capacity management (S3.4.2). Occupancy is counted from active
     sessions and entry is refused at the configured maximum n.
  3. Payments table (S3.4.1 Table 3.4) replaces transactions.
  4. Optional driver name (Table 3.3).

API Endpoints:
  POST /entry           — car enters, plate detected, session created
  POST /exit            — car exits, 1:1 verification, bill calculated
  GET  /sessions        — vehicles currently in the facility
  GET  /occupancy       — real-time occupancy against capacity (S3.4.2)
  GET  /payments        — completed billing records
  GET  /session/{plate} — current session for a specific plate
  GET  /dashboard       — HTML dashboard
"""

import sqlite3
from datetime import datetime

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from database import get_connection, init_db
from billing import calculate_bill, format_duration
from biometrics import verify

app = FastAPI(
    title="ACPACS API",
    description="Automated Car Park Access Control System — Backend API",
    version="2.0.0"
)

init_db()

# Report S3.4.2: maximum permitted occupancy n, a configurable constant.
# Set this to the number of bays in the facility. Drop it to 1 or 2 when
# demonstrating the CAR PARK FULL path.
MAX_OCCUPANCY = 5


# --- Request/Response Models ---

class EntryRequest(BaseModel):
    plate: str                    # from ALPR e.g. "KSF487HN"
    fingerprint_template: str     # hex-encoded minutiae template from the scan
    fingerprint_confidence: int = 0
    name: str | None = None       # optional, no effect on auth (Table 3.3)


class ExitRequest(BaseModel):
    plate: str
    fingerprint_template: str     # hex-encoded template from the EXIT scan
    fingerprint_confidence: int = 0


class EntryResponse(BaseModel):
    message: str
    session_id: int
    plate: str
    entry_time: str
    occupancy: int
    capacity: int


class ExitResponse(BaseModel):
    message: str
    plate: str
    duration: str
    billable_hours: int
    amount_naira: float
    payment_id: int
    match_score: float


# --- Helpers ---

def _decode_template(hex_string: str) -> bytes:
    """Hex from the microcontroller to raw template bytes for the BLOB column."""
    try:
        raw = bytes.fromhex(hex_string.strip())
    except ValueError:
        raise HTTPException(status_code=422, detail="Malformed fingerprint template")
    if not raw:
        raise HTTPException(status_code=422, detail="Empty fingerprint template")
    return raw


def _current_occupancy(cursor) -> int:
    """Report Equation 3.3."""
    return cursor.execute(
        "SELECT COUNT(*) AS n FROM sessions WHERE status = 'active'"
    ).fetchone()["n"]


# --- API Routes ---

@app.post("/entry", response_model=EntryResponse)
def vehicle_entry(req: EntryRequest):
    """
    Entry gate. Creates the session and binds the driver's template to the
    plate for the lifetime of that session (S3.3.3).
    """
    plate = req.plate.upper().replace(" ", "")
    template = _decode_template(req.fingerprint_template)

    conn = get_connection()
    cursor = conn.cursor()

    try:
        # Duplicate entry check (S3.3.3.1)
        existing = cursor.execute(
            "SELECT id FROM sessions WHERE plate = ? AND status = 'active'",
            (plate,)
        ).fetchone()

        if existing:
            raise HTTPException(
                status_code=400,
                detail=f"Plate {plate} already has an active parking session "
                       f"(Session ID: {existing['id']})"
            )

        # Capacity check (S3.4.2, Equation 3.4)
        occupancy = _current_occupancy(cursor)
        if occupancy >= MAX_OCCUPANCY:
            raise HTTPException(
                status_code=409,
                detail=f"CAR PARK FULL — {occupancy}/{MAX_OCCUPANCY} bays occupied"
            )

        entry_time = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        try:
            cursor.execute(
                """INSERT INTO sessions
                   (plate, fingerprint_template, name, entry_time, status)
                   VALUES (?, ?, ?, ?, 'active')""",
                (plate, template, req.name, entry_time)
            )
        except sqlite3.IntegrityError:
            # The unique partial index caught a concurrent duplicate that the
            # SELECT above missed — the multi-lane race S3.5 anticipates.
            raise HTTPException(
                status_code=400,
                detail=f"Plate {plate} already has an active parking session"
            )

        session_id = cursor.lastrowid
        conn.commit()

        return EntryResponse(
            message=f"Entry recorded. Welcome! Session started for {plate}.",
            session_id=session_id,
            plate=plate,
            entry_time=entry_time,
            occupancy=occupancy + 1,
            capacity=MAX_OCCUPANCY
        )
    finally:
        conn.close()


@app.post("/exit", response_model=ExitResponse)
def vehicle_exit(req: ExitRequest):
    """
    Exit gate. Performs the 1:1 verification described in S3.3.2.2: one
    session row is loaded, its bound template is compared pairwise against
    the live scan, and the bill is only raised if the score clears the
    threshold. No search is performed over any other session.
    """
    plate = req.plate.upper().replace(" ", "")
    live_template = _decode_template(req.fingerprint_template)

    conn = get_connection()
    cursor = conn.cursor()

    try:
        session = cursor.execute(
            "SELECT * FROM sessions WHERE plate = ? AND status = 'active'",
            (plate,)
        ).fetchone()

        if not session:
            raise HTTPException(
                status_code=404,
                detail=f"No active session found for plate {plate}"
            )

        # 1:1 verification — the single question S3.3.2.2 poses.
        passed, score = verify(session["fingerprint_template"], live_template)

        if not passed:
            raise HTTPException(
                status_code=403,
                detail=f"BIOMETRIC MISMATCH - ACCESS DENIED "
                       f"(score {score:.2f}, threshold 0.80)"
            )

        exit_time = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

        # Never let a billing failure escape as an unhandled exception.
        # Starlette answers an unhandled error with PLAIN TEXT, not JSON,
        # which crashes any client that calls .json() on the response.
        try:
            bill = calculate_bill(session["entry_time"], exit_time)
        except ValueError as e:
            raise HTTPException(status_code=500, detail=f"Billing error: {e}")

        cursor.execute(
            "UPDATE sessions SET exit_time = ?, status = 'closed' WHERE id = ?",
            (exit_time, session["id"])
        )

        cursor.execute(
            """INSERT INTO payments
               (session_id, plate, entry_time, exit_time, duration_minutes,
                billable_hours, fee_amount, payment_mode, paid_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'CASHLESS', ?)""",
            (
                session["id"], plate, session["entry_time"], exit_time,
                bill["duration_minutes"], bill["billable_hours"],
                bill["amount_naira"], exit_time
            )
        )
        payment_id = cursor.lastrowid
        conn.commit()

        return ExitResponse(
            message=f"Exit recorded for {plate}. Thank you!",
            plate=plate,
            duration=format_duration(bill["duration_minutes"]),
            billable_hours=bill["billable_hours"],
            amount_naira=bill["amount_naira"],
            payment_id=payment_id,
            match_score=round(score, 4)
        )
    finally:
        conn.close()


@app.get("/occupancy")
def get_occupancy():
    """Real-time occupancy against capacity (S3.4.2)."""
    conn = get_connection()
    try:
        occupancy = _current_occupancy(conn.cursor())
        return {
            "occupancy": occupancy,
            "capacity": MAX_OCCUPANCY,
            "spaces_available": max(0, MAX_OCCUPANCY - occupancy),
            "full": occupancy >= MAX_OCCUPANCY
        }
    finally:
        conn.close()


@app.get("/sessions")
def get_active_sessions():
    """Vehicles currently in the facility."""
    conn = get_connection()
    sessions = conn.execute(
        "SELECT * FROM sessions WHERE status = 'active' ORDER BY entry_time DESC"
    ).fetchall()
    conn.close()

    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    result = []
    for s in sessions:
        bill = calculate_bill(s["entry_time"], now)
        result.append({
            "session_id": s["id"],
            "plate": s["plate"],
            "name": s["name"],
            "entry_time": s["entry_time"],
            "duration_so_far": format_duration(bill["duration_minutes"]),
            "estimated_bill": f"₦{bill['amount_naira']:,.0f}"
        })
    return result


@app.get("/payments")
def get_payments():
    """Completed billing records (Table 3.4)."""
    conn = get_connection()
    rows = conn.execute("SELECT * FROM payments ORDER BY paid_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/session/{plate}")
def get_session_by_plate(plate: str):
    """Current session status for a specific plate."""
    plate = plate.upper().replace(" ", "")
    conn = get_connection()
    session = conn.execute(
        "SELECT * FROM sessions WHERE plate = ? AND status = 'active'",
        (plate,)
    ).fetchone()
    conn.close()

    if not session:
        raise HTTPException(status_code=404, detail=f"No active session for {plate}")

    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    bill = calculate_bill(session["entry_time"], now)
    return {
        "plate": plate,
        "entry_time": session["entry_time"],
        "duration_so_far": format_duration(bill["duration_minutes"]),
        "current_bill": f"₦{bill['amount_naira']:,.0f}"
    }


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    """Serves the HTML dashboard."""
    with open("templates/dashboard.html") as f:
        return f.read()