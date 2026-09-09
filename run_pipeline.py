"""
One-command pipeline: synthetic data -> ETL/validation -> SLA escalation
agent -> Excel scorecard -> HTML dashboard -> full test suite.

Run: python3 run_pipeline.py
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STEPS = [
    ("Generating synthetic data", [sys.executable, "data/generate_data.py"]),
    ("Running staging-to-core ETL + validation", [sys.executable, "etl/staging_to_core.py"]),
    ("Running PA intake & SLA escalation agent", [sys.executable, "agent/umguard.py"]),
    ("Building Excel scorecard", [sys.executable, "excel/build_workbook.py"]),
    ("Building HTML dashboard", [sys.executable, "dashboard/build_dashboard.py"]),
    ("Running test suite", [sys.executable, "-m", "pytest", "tests/", "-q"]),
]


def main() -> int:
    for label, cmd in STEPS:
        print(f"\n=== {label} ===")
        t0 = time.time()
        result = subprocess.run(cmd, cwd=ROOT)
        elapsed = time.time() - t0
        if result.returncode != 0:
            print(f"FAILED: {label} (exit {result.returncode}, {elapsed:.1f}s)")
            return result.returncode
        print(f"OK ({elapsed:.1f}s)")
    print("\nPipeline complete. See output/ for escalations.json, audit_log.json, "
          "pa_sla_scorecard.xlsx, and dashboard.html.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
