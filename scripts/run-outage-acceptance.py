#!/usr/bin/env python3
"""Run actual 30-minute API outage and real <=5-minute recovery acceptance."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests/e2e"))
from long_run_runtime import main

if __name__ == "__main__":
    raise SystemExit(main("CX-19"))
