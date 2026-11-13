import json

import numpy as np
import pandas as pd
import pytest

import diagnostics
import session_trimmer


def test_dt_stats_reports_actual_rate():
    dt_ms = pd.Series(np.full(1_000, 2.0))
    stats = diagnostics.dt_stats(dt_ms, expected_hz=500)
    assert stats["actual_hz"] == 500.0
    assert stats["p99_dt_ms"] == 2.0


def test_missing_samples_counts_gaps_and_duplicates():
    dt_ms = pd.Series([2.0] * 100 + [9.0, 0.01])
    assert diagnostics.missing_samples(dt_ms, 500) == {"gaps": 1, "duplicates": 1, "max_gap_ms": 9.0}


def test_run_writes_timing_report(session_dir):
    session_trimmer.run(session_dir)

    json_path = diagnostics.run(session_dir)

    timing = json.loads(json_path.read_text())
    assert timing["total_rows"] == 30_000
    assert timing["sensors"]["imu"]["actual_hz"] == pytest.approx(500.0, abs=0.5)
    assert timing["sensors"]["gps"]["actual_hz"] == pytest.approx(1.0, abs=0.01)
    assert timing["clock_drift"]["drift_ppm"] == pytest.approx(0.0, abs=1.0)
    assert (session_dir / "diagnostics" / "jitter_v1.png").exists()
    assert (session_dir / "diagnostics" / "alignment_v1.png").exists()
