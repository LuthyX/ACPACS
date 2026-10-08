"""
test_billing.py
ACPACS — Billing Algorithm Verification (report S3.4.3, metric S4.2)

S4.2 sets a target of 100% billing accuracy. This exercises the ceiling
formula against hand-computed expected values, concentrating on the hour
boundaries where a ceiling function is most likely to be wrong.

    python3 test_billing.py

Prints a results table suitable for Chapter 4.
"""

from billing import calculate_bill, format_duration, RATE_PER_HOUR_NAIRA

BASE = "2026-08-30T08:00:00"


def at(minutes, seconds=0):
    """Build an exit timestamp `minutes` and `seconds` after BASE."""
    from datetime import datetime, timedelta
    t = datetime.strptime(BASE, "%Y-%m-%dT%H:%M:%S")
    t += timedelta(minutes=minutes, seconds=seconds)
    return t.strftime("%Y-%m-%dT%H:%M:%S")


# (description, exit_time, expected billable hours, expected fee)
CASES = [
    ("Same second (instant exit)",       at(0),        1, 500.0),
    ("40 seconds",                       at(0, 40),    1, 500.0),
    ("1 minute",                         at(1),        1, 500.0),
    ("30 minutes",                       at(30),       1, 500.0),
    ("59 minutes",                       at(59),       1, 500.0),
    ("Exactly 1 hour",                   at(60),       1, 500.0),
    ("1 hour 1 minute (boundary)",       at(61),       2, 1000.0),
    ("1 hour 20 minutes",                at(80),       2, 1000.0),
    ("Exactly 2 hours",                  at(120),      2, 1000.0),
    ("2 hours 1 minute (boundary)",      at(121),      3, 1500.0),
    ("Exactly 5 hours",                  at(300),      5, 2500.0),
    ("12 hours 30 minutes",              at(750),     13, 6500.0),
]


def main():
    print(f"\nACPACS Billing Verification — rate NGN {RATE_PER_HOUR_NAIRA:,.0f}/hour")
    print(f"Formula (Eq. 3.5): fee = ceil(duration / 60 min) x rate\n")

    header = f"{'Case':<32}{'Duration':>12}{'Hours':>7}{'Fee (NGN)':>12}{'Result':>9}"
    print(header)
    print("-" * len(header))

    passed = 0
    for desc, exit_time, want_hours, want_fee in CASES:
        bill = calculate_bill(BASE, exit_time)
        ok = bill["billable_hours"] == want_hours and bill["amount_naira"] == want_fee
        passed += ok
        print(f"{desc:<32}"
              f"{format_duration(bill['duration_minutes']):>12}"
              f"{bill['billable_hours']:>7}"
              f"{bill['amount_naira']:>12,.0f}"
              f"{'PASS' if ok else 'FAIL':>9}")
        if not ok:
            print(f"{'':<32}  expected {want_hours}h / NGN {want_fee:,.0f}")

    # A clock running backwards must be rejected, not billed.
    print("-" * len(header))
    try:
        calculate_bill(at(60), BASE)
        rejected = False
    except ValueError:
        rejected = True
    print(f"{'Exit before entry rejected':<32}{'':>31}{'PASS' if rejected else 'FAIL':>9}")

    total = len(CASES) + 1
    passed += rejected
    print(f"\n{passed}/{total} cases passed — billing accuracy "
          f"{100 * passed / total:.1f}%")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
