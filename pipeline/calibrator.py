#!/usr/bin/env python3
"""
calibrator.py  —  Sensor calibration and derived signal computation.

TWO MODES:

  --compute-params  Estimate calibration parameters FROM the static session.
                    Saves calibration_params.json at the project root.

  --apply           Apply saved params to any session, adding corrected and
                    derived columns. Saves calibrated_vi.csv + calibration_vi.json.

Called by run_pipeline.py or standalone:
    python pipeline/calibrator.py --compute-params data/static_calib_session
    python pipeline/calibrator.py --apply data/mag_rot_session --params calibration_params.json

Calibration steps applied:
  1. Gyroscope bias subtraction    (from static session mean per axis)
  2. Accelerometer scale + offset  (per axis, from static session or 6-pos)
  3. Magnetometer hard iron offset (from sphere fit on rotation session)
  4. Barometer temperature drift   (linear pressure ~ f(temp) correction)

Derived columns:
  (Removed: Fusion and analysis such as roll/pitch and EKF are moved to the analysis repo)
"""

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import least_squares


# ── loading ────────────────────────────────────────────────────────────────────
def load_signal(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, low_memory=False)
    df = df[df["t_imu_us"] != "t_imu_us"].copy()
    for col in df.columns:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["t_imu_us"]).reset_index(drop=True)


def latest_signal(session_dir: Path) -> Path:
    signals = sorted((session_dir / "clean").glob("signal_v*.csv"))
    if not signals:
        raise FileNotFoundError(f"No signal_v*.csv in {session_dir}/clean/")
    return signals[-1]


def next_version(folder: Path) -> int:
    existing = list(folder.glob("calibrated_v*.csv"))
    nums = []
    for f in existing:
        try:
            nums.append(int(f.stem.split("_v")[-1]))
        except ValueError:
            pass
    return max(nums, default=0) + 1


# ── calibration estimation ─────────────────────────────────────────────────────
def estimate_gyro_bias(df: pd.DataFrame) -> list[float]:
    """Mean gyro reading at rest = bias."""
    return [
        round(float(df.gx_dps.mean()), 6),
        round(float(df.gy_dps.mean()), 6),
        round(float(df.gz_dps.mean()), 6),
    ]


def estimate_accel_bias_scale(df: pd.DataFrame) -> tuple[list, list]:
    """
    Simple single-axis estimate from a static session where az ≈ +1g.
    For a full 6-position calibration, run this on the accel_6pos session
    with known face-down orientations — the pipeline will use that version
    when available. This gives a reasonable starting point from static data.
    """
    # Each axis at rest: bias = mean, scale from |accel| normalization
    ax_mean = float(df.ax_g.mean())
    ay_mean = float(df.ay_g.mean())
    az_mean = float(df.az_g.mean())

    # True gravity vector magnitude should be 1g
    measured_g = float(np.sqrt(df.ax_g**2 + df.ay_g**2 + df.az_g**2).mean())
    scale = 1.0 / measured_g if measured_g > 0 else 1.0

    # For a truly static session, ax and ay biases are their means.
    # az bias is less reliable (always loaded), so we compute it as
    # the residual after scale: az_true = az * scale, bias = az_mean*scale - 1
    bias  = [round(ax_mean, 6), round(ay_mean, 6), round(az_mean * scale - 1.0, 6)]
    scales = [round(scale, 6), round(scale, 6), round(scale, 6)]
    return bias, scales


def sphere_fit_mag(mx: np.ndarray, my: np.ndarray, mz: np.ndarray) -> tuple[list, float]:
    """
    Fit a sphere to magnetometer readings to find hard iron offsets.
    Returns (offsets [ox, oy, oz], radius).

    NOTE: Requires rotation data covering all orientations. On a static session
    the fit degenerates — radius will be near zero and offsets will equal the
    mean field, which is wrong. The apply_calibration() guard skips offsets
    when radius < 10 μT to protect against this.
    """
    def residuals(p, x, y, z):
        ox, oy, oz, r = p
        return np.sqrt((x - ox)**2 + (y - oy)**2 + (z - oz)**2) - r

    x0 = [float(mx.mean()), float(my.mean()), float(mz.mean()),
          float(np.sqrt(mx**2 + my**2 + mz**2).mean())]
    result = least_squares(residuals, x0, args=(mx, my, mz), max_nfev=5000)
    ox, oy, oz, r = result.x
    r_fit = round(float(r), 4)
    if r_fit < 10.0:
        print(f"  ⚠ mag sphere fit radius={r_fit:.2f} μT — degenerate fit from static data")
        print("    Offsets will be stored but NOT applied until a mag_rotation session is run.")
    return [round(float(ox), 4), round(float(oy), 4), round(float(oz), 4)], r_fit


def estimate_baro_temp_model(df: pd.DataFrame) -> dict:
    """
    Fit linear model: pressure = a * temp + b
    Residuals from this fit represent true pressure variation (weather + altitude)
    minus the temperature-induced drift.
    """
    valid = df[(df.pressure_hPa > 800) & (df.bmp_temp_C > 0)].copy()
    if len(valid) < 100:
        return {"a": 0.0, "b": float(df.pressure_hPa.mean()), "r2": 0.0}
    x = valid.bmp_temp_C.values
    y = valid.pressure_hPa.values
    coeffs = np.polyfit(x, y, 1)
    y_pred = np.polyval(coeffs, x)
    ss_res = float(np.sum((y - y_pred)**2))
    ss_tot = float(np.sum((y - y.mean())**2))
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return {
        "a":  round(float(coeffs[0]), 6),
        "b":  round(float(coeffs[1]), 4),
        "r2": round(r2, 4),
    }


# ── calibration application ────────────────────────────────────────────────────
def apply_calibration(df: pd.DataFrame, params: dict) -> pd.DataFrame:
    df = df.copy()

    # 1. Gyro bias subtraction
    gb = params["gyro_bias"]
    df["gx_dps"] -= gb[0]
    df["gy_dps"] -= gb[1]
    df["gz_dps"] -= gb[2]

    # 2. Accel scale + bias
    ab = params["accel_bias"]
    as_ = params["accel_scale"]
    df["ax_g"] = (df["ax_g"] - ab[0]) * as_[0]
    df["ay_g"] = (df["ay_g"] - ab[1]) * as_[1]
    df["az_g"] = (df["az_g"] - ab[2]) * as_[2]

    # 3. Magnetometer hard iron offsets
    # Only apply if radius > 10 μT — sphere fit from a static session
    # degenerates to near-zero radius and would wipe out the entire signal.
    # Valid offsets only come from a mag_rotation session with full 3D coverage.
    mag_radius = params.get("mag_radius_uT", 0)
    if mag_radius > 10.0:
        mo = params["mag_offsets"]
        df["mx_uT"] -= mo[0]
        df["my_uT"] -= mo[1]
        df["mz_uT"] -= mo[2]
    else:
        print(f"  ⚠ mag offsets skipped — radius {mag_radius:.2f} μT < 10 μT "
              f"(run a mag_rotation session for valid hard iron calibration)")

    return df



# ── compute params mode ────────────────────────────────────────────────────────
def compute_params(session_dir: Path, params_path: Path, user_note: str = ""):
    sig_path = latest_signal(session_dir)
    print("\n── Compute calibration params ──────────────────────")
    print(f"  Source : {sig_path}")

    df = load_signal(sig_path)

    gyro_bias          = estimate_gyro_bias(df)
    accel_bias, accel_scale = estimate_accel_bias_scale(df)
    baro_model         = estimate_baro_temp_model(df)

    # Mag offsets from sphere fit (static session gives a rough estimate;
    # will be overwritten when mag rotation session is processed)
    mx = df["mx_uT"].dropna().values
    my = df["my_uT"].dropna().values
    mz = df["mz_uT"].dropna().values
    mag_offsets, mag_radius = sphere_fit_mag(mx, my, mz)

    # Reference pressure + GPS altitude anchor for absolute baro altitude
    p0 = float(df.pressure_hPa[df.pressure_hPa > 800].median()) if len(df.pressure_hPa[df.pressure_hPa > 800]) > 0 else float("nan")
    gps_ok = df[(df.hdop < 2.5) & (df.alt_gps_m > 0)]
    gps_ref_alt = float(gps_ok.alt_gps_m.mean()) if len(gps_ok) >= 10 else None

    from datetime import datetime
    # Quality check for "dry runs" (no sensors connected)
    measured_g = float(np.sqrt(df.ax_g**2 + df.ay_g**2 + df.az_g**2).mean())
    calib_valid = bool(measured_g > 0 and mag_radius > 0)

    if not calib_valid:
        print("\n\033[91m  ⚠ Calibration invalid — insufficient sensor signal (all zeros detected)\033[0m")

    params = {
        "computed_at":           datetime.now().isoformat(timespec="seconds"),
        "user_note":             user_note,
        "source_session":        session_dir.name,
        "calibration_valid":     calib_valid,
        "gyro_bias":             gyro_bias,
        "accel_bias":            accel_bias,
        "accel_scale":           accel_scale,
        "mag_offsets":           mag_offsets,
        "mag_radius_uT":         mag_radius,
        "baro_temp_model":       baro_model,
        "baro_ref_pressure_hPa": round(p0, 4),
        "baro_ref_temp_K":       288.15,
        "gps_ref_alt_m":         round(gps_ref_alt, 1) if gps_ref_alt else None,
        "notes": {
            "gyro_bias":    "subtract from raw °/s — valid until sensor is re-powered in new environment",
            "accel_bias":   "static session estimate — replace with 6-position calibration for better accuracy",
            "accel_scale":  "uniform scale from |a| normalization — replace with per-axis 6-pos calibration",
            "mag_offsets":  "hard iron offsets from sphere fit — replace with rotation session for best accuracy",
            "baro_model":   f"pressure = {baro_model['a']:.4f}*T + {baro_model['b']:.2f}  R²={baro_model['r2']:.3f}",
        },
    }

    params_path.write_text(json.dumps(params, indent=2))
    print(f"\n  Gyro bias     : gx={gyro_bias[0]:+.4f}  gy={gyro_bias[1]:+.4f}  gz={gyro_bias[2]:+.4f}  °/s")
    print(f"  Accel bias    : {[f'{x:+.4f}' for x in accel_bias]}")
    print(f"  Accel scale   : {[f'{x:.4f}' for x in accel_scale]}")
    print(f"  Mag offsets   : {[f'{x:+.2f}' for x in mag_offsets]}  μT  (r={mag_radius:.1f} μT)")
    print(f"  Baro model    : a={baro_model['a']:.4f}  b={baro_model['b']:.2f}  R²={baro_model['r2']:.3f}")
    print(f"  Baro ref P    : {p0:.2f} hPa")
    if gps_ref_alt:
        print(f"  GPS ref alt   : {gps_ref_alt:.1f} m  (absolute anchor for baro altitude)")
    else:
        print("  GPS ref alt   : not available — alt_baro_m will be relative")

    # SUSPICIOUS BIAS WARNINGS
    warnings = []
    if any(abs(gb) > 50.0 for gb in gyro_bias):
        warnings.append("⚠ Gyro bias is > 50 °/s. This is suspiciously high! Was the sensor moving?")

    # Calculate |a| for raw accel
    measured_g = float(np.sqrt(df.ax_g**2 + df.ay_g**2 + df.az_g**2).mean())
    if not (0.90 <= measured_g <= 1.10):
        warnings.append(f"⚠ Raw |accel| mean is {measured_g:.3f} g. This is far from 1.0 g!")

    if mag_radius > 10.0 and not (20.0 <= mag_radius <= 70.0):
        warnings.append(f"⚠ Mag radius is {mag_radius:.1f} μT. Expected Earth's field (20-70 μT)!")

    if warnings:
        print("\n\033[91m  SUSPICIOUS CALIBRATION WARNINGS:\033[0m")
        for w in warnings:
            print(f"\033[91m    {w}\033[0m")

    print(f"\n  ✓ params → {params_path}")

    return params


# ── apply mode ─────────────────────────────────────────────────────────────────
def apply_params(session_dir: Path, params_path: Path):
    sig_path = latest_signal(session_dir)
    calib_dir = session_dir / "calibrated"
    calib_dir.mkdir(parents=True, exist_ok=True)

    print("\n── Apply calibration ───────────────────────────────")
    print(f"  Input  : {sig_path.name}")
    print(f"  Params : {params_path.name}")

    params = json.loads(params_path.read_text())
    df     = load_signal(sig_path)

    df_cal = apply_calibration(df, params)

    v = next_version(calib_dir)
    out_csv  = calib_dir / f"calibrated_v{v}.csv"
    out_json = calib_dir / f"calibration_v{v}.json"

    df_cal.to_csv(out_csv, index=False)

    # calibration_vi.json
    accel_after = float(np.sqrt(df_cal.ax_g**2 + df_cal.ay_g**2 + df_cal.az_g**2).mean())
    mag_after   = float(np.sqrt(df_cal.mx_uT**2 + df_cal.my_uT**2 + df_cal.mz_uT**2).mean())

    calib_meta = {
        "version":          v,
        "input_file":       sig_path.name,
        "params_file":      params_path.name,
        "params_source_session": params.get("source_session", "none"),
        "params_computed_at":    params.get("computed_at", "unknown"),
        "params_user_note":      params.get("user_note", ""),
        "calibration_valid":     params.get("calibration_valid", True),
        "gyro_bias":        params["gyro_bias"],
        "accel_bias":       params["accel_bias"],
        "accel_scale":      params["accel_scale"],
        "mag_offsets":      params["mag_offsets"],
        "mag_radius_uT":    params.get("mag_radius_uT"),
        "baro_temp_coeff":  params["baro_temp_model"]["a"],
        "baro_model_r2":    params["baro_temp_model"]["r2"],
        "post_cal_accel_mean_g":  round(accel_after, 4),
        "post_cal_mag_mean_uT":   round(mag_after, 2),
    }
    out_json.write_text(json.dumps(calib_meta, indent=2))

    print(f"  Post-cal |a| = {accel_after:.4f} g  (target 1.0000)")
    print(f"  Post-cal |B| = {mag_after:.2f} μT")
    print(f"  ✓ calibrated → {out_csv.name}")
    print(f"  ✓ meta       → {out_json.name}")

    return out_csv


# ── CLI ─────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--compute-params", metavar="SESSION_DIR", type=Path,
                       help="Estimate params from a static session")
    group.add_argument("--apply", metavar="SESSION_DIR", type=Path,
                       help="Apply params to a session")
    parser.add_argument("--params", type=Path, default=Path("calibration_params.json"),
                        help="Path to calibration_params.json (default: ./calibration_params.json)")
    args = parser.parse_args()

    if args.compute_params:
        compute_params(args.compute_params, args.params)
    else:
        apply_params(args.apply, args.params)


if __name__ == "__main__":
    main()
