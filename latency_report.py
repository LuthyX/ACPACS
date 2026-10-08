"""
latency_report.py
ACPACS — Latency Analysis (report metric S4.2)

S4.2 sets a target of 2.5 seconds. serial_bridge.py logs each gate cycle
decomposed into its segments; this summarises them.

    python3 latency_report.py

Report BOTH the end-to-end cycle and the system processing latency. The
end-to-end figure includes the driver presenting a finger, which is not a
property of the software and cannot be under 2.5s for any system. Quoting
only the processing figure looks like moving the goalposts; quoting only
the end-to-end figure understates the software. Quote both, labelled.
"""

import csv
import os
import statistics
import sys

LOG = "latency_log.csv"
TARGET = 2.5


def stats(values):
    if not values:
        return None
    return {
        "n": len(values),
        "mean": statistics.mean(values),
        "median": statistics.median(values),
        "min": min(values),
        "max": max(values),
        "stdev": statistics.stdev(values) if len(values) > 1 else 0.0,
    }


def row(label, values, target=None):
    s = stats(values)
    if not s:
        return
    line = (f"  {label:<22}{s['mean']:>8.2f}{s['median']:>9.2f}"
            f"{s['min']:>8.2f}{s['max']:>8.2f}{s['stdev']:>9.2f}")
    if target is not None:
        under = sum(1 for v in values if v <= target)
        line += f"{under:>8}/{s['n']}"
    print(line)


def section(title, rows):
    if not rows:
        return
    print(f"\n{title}  (n = {len(rows)})")
    print(f"  {'Segment':<22}{'Mean':>8}{'Median':>9}{'Min':>8}{'Max':>8}"
          f"{'Stdev':>9}  Within target")
    print("  " + "-" * 72)
    row("ALPR inference", [r["alpr_s"] for r in rows])
    row("Backend call", [r["backend_s"] for r in rows])
    row("Driver wait", [r["driver_wait_s"] for r in rows])
    print("  " + "-" * 72)
    row("SYSTEM (S4.2)", [r["system_s"] for r in rows], TARGET)
    row("End-to-end cycle", [r["total_s"] for r in rows])


def main():
    if not os.path.exists(LOG):
        print(f"No {LOG} yet. Run a few gate cycles first.")
        return 1

    entries, exits = [], []
    with open(LOG, newline="") as f:
        reader = csv.DictReader(f)
        if "system_s" not in (reader.fieldnames or []):
            print(f"{LOG} is from an older bridge version and has no segment")
            print("columns. Delete it and re-run the rehearsal to collect")
            print("properly decomposed timings.")
            return 1
        for r in reader:
            try:
                parsed = {k: float(r[k]) for k in
                          ("alpr_s", "backend_s", "driver_wait_s",
                           "system_s", "total_s")}
            except (KeyError, ValueError):
                continue
            (entries if r.get("event") == "entry" else exits).append(parsed)

    if not entries and not exits:
        print(f"{LOG} has no usable rows.")
        return 1

    print(f"ACPACS Latency Analysis — S4.2 target {TARGET}s, all values seconds")
    section("ENTRY CYCLES", entries)
    section("EXIT CYCLES", exits)

    print("\nReporting guidance:")
    print("  SYSTEM is the figure the 2.5s target applies to. It excludes the")
    print("  driver presenting a finger, and for exits also the 4s fare dwell")
    print("  on the LCD. Report the end-to-end cycle alongside it, with that")
    print("  decomposition stated, so nothing looks concealed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())