"""
billing.py
ACPACS — Billing Logic (Section 3.4.3 of report)

v1.1 change (1 hunk):
  calculate_bill() no longer raises when entry and exit fall inside the
  same minute. The stated policy is a 1-hour minimum, so a sub-minute
  stay is now floored at 1 minute, which ceilings to 1 billable hour.

Billing uses the ceiling function:
  billable_hours = ceil(duration_minutes / 60)
  amount = billable_hours × rate_per_hour

Example:
  Parked for 1hr 20min (80 minutes) → ceil(80/60) = ceil(1.33) = 2 hours → 2 × ₦500 = ₦1,000
  Parked for exactly 1hr (60 minutes) → ceil(60/60) = ceil(1.0) = 1 hour → 1 × ₦500 = ₦500
  Parked for 30min → ceil(30/60) = ceil(0.5) = 1 hour → 1 × ₦500 = ₦500 (minimum 1 hour)
  Parked for 40 seconds → floored to 1 minute → 1 hour → ₦500 (minimum charge)
"""

import math
from datetime import datetime

# Configurable rate — change this to match your report's assumed rate
RATE_PER_HOUR_NAIRA = 500.0  # ₦500 per hour


def calculate_bill(entry_time: str, exit_time: str) -> dict:
    """
    Calculate parking bill given entry and exit times.

    Args:
        entry_time: ISO format string e.g. "2025-01-01T08:00:00"
        exit_time:  ISO format string e.g. "2025-01-01T09:20:00"

    Returns:
        dict with duration_minutes, billable_hours, amount_naira
    """
    fmt = "%Y-%m-%dT%H:%M:%S"
    entry = datetime.strptime(entry_time, fmt)
    exit  = datetime.strptime(exit_time, fmt)

    # ===== HUNK 1 ===================================================
    # Only a genuinely backwards clock is an error.
    if exit < entry:
        raise ValueError("Exit time must be after entry time")

    # Floor at 1 minute. Previously this computed 0 for any stay under
    # 60 seconds and raised ValueError, which 500'd the /exit endpoint
    # and denied the vehicle — i.e. it broke every live demonstration,
    # where entry and exit happen seconds apart. The documented policy
    # is a 1-hour minimum charge, and 1 minute ceilings to exactly that.
    duration_minutes = max(1, int((exit - entry).total_seconds() / 60))
    # ================================================================

    # Ceiling function — this is Section 3.4.3's billing formula
    billable_hours = math.ceil(duration_minutes / 60)

    # Final bill
    amount_naira = billable_hours * RATE_PER_HOUR_NAIRA

    return {
        "duration_minutes": duration_minutes,
        "billable_hours": billable_hours,
        "amount_naira": amount_naira,
        "rate_per_hour": RATE_PER_HOUR_NAIRA
    }


def format_duration(minutes: int) -> str:
    """Convert minutes to human-readable string e.g. '1hr 20min'"""
    hours = minutes // 60
    mins = minutes % 60
    if hours == 0:
        return f"{mins}min"
    elif mins == 0:
        return f"{hours}hr"
    else:
        return f"{hours}hr {mins}min"