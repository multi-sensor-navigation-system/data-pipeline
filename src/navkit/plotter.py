#!/usr/bin/env python3
"""
plotter.py  —  Validation and plotting for any session CSV.

Handles multi-boot sessions, mixed-type columns, and outlier-compressed axes.
Saves plots to outputs/plots/<session>/ when called from navkit.pipeline.

Usage (standalone):
    python -m navkit.plotter data/<session>/raw/raw.csv --plot
    python -m navkit.plotter data/<session>/clean/signal_v1.csv --plot
    python -m navkit.plotter data/<session>/calibrated/calibrated_v1.csv --plot

Called by navkit.pipeline via:
    plotter.run(csv_path, out_png)
"""

import argparse
import sys
from pathlib import Path

import matplotlib.gridspec as gridspec
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

FAULT_LABELS = {0: "IMU", 1: "MAG", 2: "BARO", 3: "GPS_HDOP", 4: "GPS_NOFIX"}


# ── loading ────────────────────────────────────────────────────────────────────
def load_and_clean(path: Path) -> pd.DataFrame:
    """Load CSV, strip embedded header rows, coerce all columns to numeric."""
    df = pd.read_csv(path, low_memory=False)
    df = df[df["t_imu_us"] != "t_imu_us"].copy()
    for col in df.columns:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["t_imu_us"]).reset_index(drop=True)
    return df


def build_wallclock(df: pd.DataFrame) -> pd.Series:
    """Reconstruct monotonic wall-clock time across ESP32 reboots."""
    t = df["t_imu_us"].values.astype(np.float64)
    offset, result = 0.0, np.empty_like(t)
    for i in range(len(t)):
        if i > 0 and t[i] < t[i - 1] - 500_000:
            offset += t[i - 1]
        result[i] = t[i] + offset
    return pd.Series(result / 1e6, name="wall_s")


def decode_faults(series: pd.Series) -> pd.DataFrame:
    return pd.DataFrame({
        f"fault_{label}": (series.astype(int) & (1 << bit)).astype(bool)
        for bit, label in FAULT_LABELS.items()
    })


# ── stats ──────────────────────────────────────────────────────────────────────
def print_stats(df: pd.DataFrame, t: pd.Series, path: Path):
    total_s = t.iloc[-1] - t.iloc[0]
    reboots = (df["t_imu_us"].diff() < -500_000).sum()

    print(f"\n{'='*60}")
    print(f"  {path.name}")
    print(f"{'='*60}")
    print(f"  Rows          : {len(df):,}")
    print(f"  Wall duration : {total_s:.1f} s  ({total_s/60:.1f} min)")
    print(f"  Reboots       : {reboots}  {'✓' if reboots == 0 else '⚠ multi-session file'}")

    dt_us = df["t_imu_us"].diff()
    dt_clean = dt_us[(dt_us > 0) & (dt_us < 100_000)]
    jitter = df["jitter_us"][df["jitter_us"] < 1e9]
    print("\n── IMU Timing ──────────────────────────────────────")
    print(f"  Mean period : {dt_clean.mean():.1f} μs")
    print(f"  Std dev     : {dt_clean.std():.1f} μs")
    print(f"  p5  / p95   : {dt_clean.quantile(0.05):.0f} / {dt_clean.quantile(0.95):.0f} μs")
    print(f"  jitter p95  : {jitter.quantile(0.95):.0f} μs")
    print(f"  jitter p99  : {jitter.quantile(0.99):.0f} μs")

    faults = decode_faults(df["fault_flag"])
    print("\n── Fault Flags ─────────────────────────────────────")
    for col in faults.columns:
        pct = faults[col].mean() * 100
        print(f"  {'✓' if pct < 1 else '⚠'} {col:<20}: {faults[col].sum():>6,}  ({pct:.2f}%)")

    accel = np.sqrt(df.ax_g**2 + df.ay_g**2 + df.az_g**2)
    print("\n── IMU ─────────────────────────────────────────────")
    print(f"  |accel| mean/std : {accel.mean():.4f} / {accel.std():.4f} g")
    for col in ["ax_g", "ay_g", "az_g"]:
        print(f"  {col:<8} mean/std : {df[col].mean():.5f} / {df[col].std():.5f} g")
    for col in ["gx_dps", "gy_dps", "gz_dps"]:
        print(f"  {col:<8} mean/std : {df[col].mean():.4f} / {df[col].std():.4f} °/s")

    baro = df[df.pressure_hPa > 0]
    print("\n── Barometer ───────────────────────────────────────")
    print(f"  Pressure : {baro.pressure_hPa.mean():.4f} ± {baro.pressure_hPa.std():.5f} hPa")
    print(f"  Temp     : {baro.bmp_temp_C.mean():.3f} ± {baro.bmp_temp_C.std():.4f} °C")

    mag = np.sqrt(df.mx_uT**2 + df.my_uT**2 + df.mz_uT**2)
    print("\n── Magnetometer ────────────────────────────────────")
    print(f"  |B| mean/std : {mag.mean():.2f} / {mag.std():.3f} μT")

    gps_good = df[df.hdop < 2.5]
    print("\n── GPS ─────────────────────────────────────────────")
    print(f"  HDOP <2.5 : {len(gps_good):,} / {len(df):,} ({len(gps_good)/len(df)*100:.1f}%)")
    if len(gps_good):
        print(f"  HDOP mean : {gps_good.hdop.mean():.2f}   sats : {gps_good.satellites.mean():.1f}")
        print(f"  Lat / Lon : {gps_good.lat_deg.mean():.6f}, {gps_good.lon_deg.mean():.6f}")
        print(f"  Altitude  : {gps_good.alt_gps_m.mean():.1f} m")
    print()


# ── plots ──────────────────────────────────────────────────────────────────────
def clip_col(s: pd.Series, k: float = 5.0) -> pd.Series:
    """IQR-based outlier clip — prevents a handful of bad rows squashing the axis."""
    q1, q3 = s.quantile(0.01), s.quantile(0.99)
    iqr = q3 - q1
    return s.clip(q1 - k * iqr, q3 + k * iqr)


def make_plots(df: pd.DataFrame, t: pd.Series, path: Path, out_png: Path):
    total_s = t.iloc[-1] - t.iloc[0]
    mag = np.sqrt(df.mx_uT**2 + df.my_uT**2 + df.mz_uT**2)

    fig = plt.figure(figsize=(16, 14))
    fig.suptitle(f"{path.name}  ({total_s/60:.1f} min)", fontsize=11)
    gs = gridspec.GridSpec(5, 1, hspace=0.45)

    ax0 = fig.add_subplot(gs[0])
    ax0.plot(t, clip_col(df.ax_g), lw=0.3, label="ax", alpha=0.8)
    ax0.plot(t, clip_col(df.ay_g), lw=0.3, label="ay", alpha=0.8)
    ax0.plot(t, clip_col(df.az_g), lw=0.3, label="az", alpha=0.8)
    ax0.axhline(1, color="k", lw=0.5, ls="--", alpha=0.3)
    ax0.set_ylabel("Accel (g)")
    ax0.legend(loc="upper right", fontsize=7, ncol=3)
    ax0.set_title("Accelerometer", fontsize=9)

    ax1 = fig.add_subplot(gs[1], sharex=ax0)
    ax1.plot(t, clip_col(df.gx_dps), lw=0.3, label="gx", alpha=0.8)
    ax1.plot(t, clip_col(df.gy_dps), lw=0.3, label="gy", alpha=0.8)
    ax1.plot(t, clip_col(df.gz_dps), lw=0.3, label="gz", alpha=0.8)
    ax1.axhline(0, color="k", lw=0.5, ls="--", alpha=0.3)
    ax1.set_ylabel("Gyro (°/s)")
    ax1.legend(loc="upper right", fontsize=7, ncol=3)
    ax1.set_title("Gyroscope", fontsize=9)

    ax2 = fig.add_subplot(gs[2], sharex=ax0)
    ax2.plot(t, clip_col(df.pressure_hPa), color="steelblue", lw=0.3)
    ax2.set_ylabel("Pressure (hPa)", color="steelblue")
    ax2b = ax2.twinx()
    ax2b.plot(t, clip_col(df.bmp_temp_C), color="tomato", lw=0.3, alpha=0.7)
    ax2b.set_ylabel("Temp (°C)", color="tomato")
    ax2.set_title("BMP280", fontsize=9)

    ax3 = fig.add_subplot(gs[3], sharex=ax0)
    ax3.plot(t, clip_col(mag), color="darkgreen", lw=0.3)
    ax3.set_ylabel("|B| (μT)")
    ax3.set_title("QMC5883L", fontsize=9)

    ax4 = fig.add_subplot(gs[4], sharex=ax0)
    ax4.plot(t, df.hdop.where(df.hdop < 90), color="navy", lw=0.3, label="HDOP")
    ax4.axhline(2.5, color="red", lw=0.8, ls="--", alpha=0.5)
    ax4b = ax4.twinx()
    ax4b.plot(t, df.satellites, color="orange", lw=0.3, alpha=0.7)
    ax4b.set_ylabel("Satellites", color="orange")
    ax4.set_ylabel("HDOP", color="navy")
    ax4.set_xlabel("Wall-clock time (s)")
    ax4.set_title("GPS quality", fontsize=9)

    plt.savefig(out_png, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  ✓ plot → {out_png}")


# ── entry points ───────────────────────────────────────────────────────────────
def run(csv_path: Path, out_png: Path):
    """Programmatic entry point for run_pipeline.py."""
    import matplotlib
    matplotlib.use("Agg")   # non-interactive backend — no window
    df = load_and_clean(csv_path)
    t  = build_wallclock(df)
    print_stats(df, t, csv_path)
    make_plots(df, t, csv_path, out_png)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("csv",   help="Path to session CSV")
    parser.add_argument("--plot", action="store_true", help="Save and show plot")
    parser.add_argument("--out",  default=None,         help="Output PNG path (default: <csv>.png)")
    args = parser.parse_args()

    path    = Path(args.csv)
    out_png = Path(args.out) if args.out else path.with_suffix(".png")

    if not path.exists():
        print(f"File not found: {path}")
        sys.exit(1)

    df = load_and_clean(path)
    t  = build_wallclock(df)
    print_stats(df, t, path)

    if args.plot:
        try:
            make_plots(df, t, path, out_png)
            plt.show()
        except ImportError:
            print("  matplotlib not installed — skipping plots")


if __name__ == "__main__":
    main()
