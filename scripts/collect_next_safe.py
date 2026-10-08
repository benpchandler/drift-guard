#!/usr/bin/env python3
"""Read-only actual hook observation collector; see drift_guard.benchmark_observations."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from drift_guard.benchmark_observations import main

if __name__ == "__main__":
    main()
