import json

import numpy as np
import pandas as pd
import pytest

from navkit import calibrator
from navkit import trimmer as session_trimmer
from conftest import GYRO_BIAS, make_session


def test_gyro_bias_is_the_static_mean():
    bias = calibrator.estimate_gyro_bias(make_session(20_000))
    assert bias == pytest.approx(GYRO_BIAS, abs=0.005)


def test_accel_scale_normalises_gravity_to_one_g():
    df = make_session(20_000)
    bias, scale = calibrator.estimate_accel_bias_scale(df)
    measured = float(np.sqrt(df.ax_g**2 + df.ay_g**2 + df.az_g**2).mean())
    assert scale[0] == pytest.approx(1.0 / measured, abs=1e-5)
    assert bias[0] == pytest.approx(0.01, abs=1e-3)


def test_sphere_fit_recovers_hard_iron_offset():
    df = make_session(5_000, rotate_mag=True)
    offsets, radius = calibrator.sphere_fit_mag(df.mx_uT.values, df.my_uT.values, df.mz_uT.values)
    assert offsets == pytest.approx([12.0, -7.0, 3.0], abs=0.01)
    assert radius == pytest.approx(45.0, abs=0.01)


def test_apply_skips_mag_offsets_for_degenerate_fit():
    df = make_session(1_000)
    params = {
        "gyro_bias": [0.5, 0.0, 0.0], "accel_bias": [0, 0, 0], "accel_scale": [1, 1, 1],
        "mag_offsets": [100.0, 100.0, 100.0], "mag_radius_uT": 0.3,
    }
    out = calibrator.apply_calibration(df, params)
    pd.testing.assert_series_equal(out.mx_uT, df.mx_uT)
    assert (out.gx_dps - df.gx_dps).round(9).eq(-0.5).all()


def test_compute_then_apply_round_trip(session_dir, tmp_path):
    session_trimmer.run(session_dir)
    params_path = tmp_path / "calibration_params.json"

    params = calibrator.compute_params(session_dir, params_path, "desk")
    out_csv = calibrator.apply_params(session_dir, params_path)

    assert params["calibration_valid"] is True
    assert params["source_session"] == session_dir.name
    cal = pd.read_csv(out_csv)
    assert abs(cal.gx_dps.mean()) < 1e-3          # bias removed
    receipt = json.loads((session_dir / "calibrated" / "calibration_v1.json").read_text())
    assert receipt["params_user_note"] == "desk"
    assert receipt["params_source_session"] == session_dir.name


def test_dry_run_with_no_sensors_is_marked_invalid(tmp_path):
    d = tmp_path / "session_dry"
    (d / "clean").mkdir(parents=True)
    df = make_session(2_000)
    for col in ["ax_g", "ay_g", "az_g", "mx_uT", "my_uT", "mz_uT"]:
        df[col] = 0.0
    df.to_csv(d / "clean" / "signal_v1.csv", index=False)

    params = calibrator.compute_params(d, tmp_path / "p.json")

    assert params["calibration_valid"] is False
