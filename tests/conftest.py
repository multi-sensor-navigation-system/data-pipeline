import os

os.environ.setdefault("MPLBACKEND", "Agg")

import numpy as np
import pandas as pd
import pytest

COLUMNS = [
    "t_imu_us", "t_read_start_us", "t_proc_end_us", "t_gps_us",
    "ax_g", "ay_g", "az_g", "gx_dps", "gy_dps", "gz_dps",
    "mx_uT", "my_uT", "mz_uT",
    "pressure_hPa", "bmp_temp_C",
    "lat_deg", "lon_deg", "alt_gps_m", "hdop", "satellites",
    "fault_flag", "jitter_us",
]

GYRO_BIAS = (0.8, -0.4, 0.25)


def make_session(n: int, hz: float = 500.0, t0_us: int = 1_000_000, seed: int = 0,
                 rotate_mag: bool = False) -> pd.DataFrame:
    """A synthetic static session shaped like the firmware's CSV stream."""
    rng = np.random.default_rng(seed)
    period = 1e6 / hz
    t = (t0_us + np.arange(n) * period).astype(np.int64)

    if rotate_mag:
        # Points on a 45 uT sphere centred on a hard-iron offset.
        theta = rng.uniform(0, np.pi, n)
        phi = rng.uniform(0, 2 * np.pi, n)
        mx = 12.0 + 45.0 * np.sin(theta) * np.cos(phi)
        my = -7.0 + 45.0 * np.sin(theta) * np.sin(phi)
        mz = 3.0 + 45.0 * np.cos(theta)
    else:
        mx = 20.0 + rng.normal(0, 0.2, n)
        my = -5.0 + rng.normal(0, 0.2, n)
        mz = 38.0 + rng.normal(0, 0.2, n)

    temp = 27.0 + np.linspace(0, 0.5, n)
    # GPS position steps once per second, as a 1 Hz receiver would report.
    sec = (np.arange(n) // int(hz)).astype(float)
    df = pd.DataFrame({
        "t_imu_us": t,
        "t_read_start_us": t,
        "t_proc_end_us": t + 600,
        "t_gps_us": (t0_us + sec * 1e6).astype(np.int64),
        "ax_g": rng.normal(0.01, 0.002, n),
        "ay_g": rng.normal(-0.02, 0.002, n),
        "az_g": rng.normal(1.02, 0.002, n),
        "gx_dps": rng.normal(GYRO_BIAS[0], 0.05, n),
        "gy_dps": rng.normal(GYRO_BIAS[1], 0.05, n),
        "gz_dps": rng.normal(GYRO_BIAS[2], 0.05, n),
        "mx_uT": mx, "my_uT": my, "mz_uT": mz,
        "pressure_hPa": 1013.0 + 0.4 * (temp - 27.0) + rng.normal(0, 0.01, n),
        "bmp_temp_C": temp,
        "lat_deg": 18.5204 + sec * 1e-6,
        "lon_deg": 73.8567 + sec * 1e-6,
        "alt_gps_m": 560.0,
        "hdop": 1.2,
        "satellites": 8,
        "fault_flag": 0,
        "jitter_us": rng.integers(0, 40, n),
    })
    return df[COLUMNS]


@pytest.fixture
def session_dir(tmp_path):
    """A session directory holding a 60 s static raw.csv."""
    d = tmp_path / "session_20260101_000000_static_calib"
    (d / "raw").mkdir(parents=True)
    make_session(30_000).to_csv(d / "raw" / "raw.csv", index=False)
    return d
