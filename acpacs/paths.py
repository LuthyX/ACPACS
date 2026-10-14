"""
paths.py
ACPACS — Project paths

Every file the system reads or writes is resolved from the project root
here, so the scripts behave the same whichever directory they are run from.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Source assets
TEMPLATES_DIR = ROOT / "acpacs" / "templates"
MODEL_PATH    = ROOT / "models" / "nigerian_plate_best.pt"
SAMPLES_DIR   = ROOT / "data" / "samples"
OCR_TEST_DIR  = ROOT / "data" / "ocr_test"

# Evaluation outputs worth keeping (tracked in git)
RESULTS_DIR   = ROOT / "results"
FIGURES_DIR   = RESULTS_DIR / "figures"
LATENCY_LOG   = RESULTS_DIR / "latency_log.csv"

# Runtime state and per-run debug images (ignored by git)
RUNTIME_DIR   = ROOT / "runtime"
DB_PATH       = RUNTIME_DIR / "acpacs.db"

for _d in (RESULTS_DIR, FIGURES_DIR, RUNTIME_DIR):
    _d.mkdir(parents=True, exist_ok=True)
