#!/usr/bin/env python3
"""
diagnostics.py  —  Timing integrity validation for a clean session.

Analyses:
  - Jitter histogram per sensor  (dt distribution, mean vs variance)
  - Sampling consistency         (p50/p95/p99 of dt, expected vs actual Hz)
  - Inter-sensor alignment       (IMU vs GPS timestamps, baro vs IMU lag)
  - Missing / duplicate samples  (gaps > 3×expected, duplicate t_imu_us)
  - Clock drift                  (MCU timer drift relative to GPS time)

Called by navkit.pipeline or standalone:
    python -m navkit.diagnostics <session_dir> [--version N]

Inputs:
    data/<session>/clean/signal_v<i>.csv

Outputs:
    data/<session>/diagnostics/jitter_v<i>.png
    data/<session>/diagnostics/alignment_v<i>.png
    data/<session>/diagnostics/timing_v<i>.json
"""

import argparse
import json
from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import numpy as np
import pandas as pd


# ── expected sensor rates ──────────────────────────────────────────────────────
EXPECTED_HZ = {
    "imu":  500,    # IMU target (actual ~350 Hz with vTaskDelay)
    "baro":  25,    # BMP280 polling rate
    "mag":   75,    # QMC5883L polling rate
    "gps":    1,    # NEO-6M NMEA sentence rate
}


# ── loading ────────────────────────────────────────────────────────────────────
def load_signal(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, low_memory=False)
    df = df[df["t_imu_us"] != "t_imu_us"].copy()
    for col in df.columns:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["t_imu_us"]).reset_index(drop=True)


def build_wallclock(df: pd.DataFrame) -> pd.Series:
    t = df["t_imu_us"].values.astype(np.float64)
    offset, result = 0.0, np.empty_like(t)
    for i in range(len(t)):
        if i > 0 and t[i] < t[i - 1] - 500_000:
            offset += t[i - 1]
        result[i] = t[i] + offset
    return pd.Series(result / 1e6, name="wall_s")


# ── per-sensor dt extraction ───────────────────────────────────────────────────
def extract_sensor_dt(df: pd.DataFrame) -> dict[str, pd.Series]:
    """
    Return dt series (in ms) for each sensor.
    Only rows where that sensor's value actually changed are used —
    this gives the true update rate rather than the IMU polling rate.
    """
    def intra_dt(mask: pd.Series) -> pd.Series:
        t = df.loc[mask, "t_imu_us"]
        dt = t.diff().dropna()
        return (dt[(dt > 0) & (dt < 10_000_000)] / 1000.0)   # μs → ms

    return {
        "imu":  (df["t_imu_us"].diff().dropna()
                 .pipe(lambda s: s[(s > 0) & (s < 100_000)]) / 1000.0),
        "baro": intra_dt(df["pressure_hPa"].diff().abs() > 1e-4),
        "mag":  intra_dt(df["mx_uT"].diff().abs() > 1e-4),
        "gps":  intra_dt((df["lat_deg"].diff().abs() > 1e-9) & (df["hdop"] < 90)),
    }


# ── statistics ─────────────────────────────────────────────────────────────────
def dt_stats(dt_ms: pd.Series, expected_hz: float) -> dict:
    if len(dt_ms) < 2:
        return {}
    expected_ms = 1000.0 / expected_hz
    return {
        "expected_hz":       expected_hz,
        "expected_dt_ms":    round(expected_ms, 3),
        "actual_hz":         round(1000.0 / float(dt_ms.mean()), 2),
        "p50_dt_ms":         round(float(dt_ms.quantile(0.50)), 3),
        "p95_dt_ms":         round(float(dt_ms.quantile(0.95)), 3),
        "p99_dt_ms":         round(float(dt_ms.quantile(0.99)), 3),
        "mean_dt_ms":        round(float(dt_ms.mean()), 3),
        "std_dt_ms":         round(float(dt_ms.std()), 3),
        "max_dt_ms":         round(float(dt_ms.max()), 3),
    }


def missing_samples(dt_ms: pd.Series, expected_hz: float) -> dict:
    if len(dt_ms) < 2:
        return {"gaps": 0, "duplicates": 0, "max_gap_ms": 0}
    expected_ms = 1000.0 / expected_hz
    gaps = int((dt_ms > 3 * expected_ms).sum())
    dups = int((dt_ms < 0.1).sum())
    return {
        "gaps":       gaps,
        "duplicates": dups,
        "max_gap_ms": round(float(dt_ms.max()), 2),
    }


def clock_drift(df: pd.DataFrame) -> dict:
    """
    Compare MCU timer rate against GPS 1 Hz ticks.
    If the MCU clock is perfect, consecutive GPS updates should be
    exactly 1 000 000 μs apart on the t_imu_us axis.
    """
    gps_rows = df[(df["lat_deg"].diff().abs() > 1e-9) & (df["hdop"] < 90)].copy()
    if len(gps_rows) < 10:
        return {"drift_ppm": None, "note": "insufficient GPS rows"}
    dt_mcu = gps_rows["t_imu_us"].diff().dropna()
    dt_mcu = dt_mcu[(dt_mcu > 800_000) & (dt_mcu < 1_200_000)]   # ~1 s ticks
    if len(dt_mcu) < 5:
        return {"drift_ppm": None, "note": "insufficient 1 Hz GPS ticks"}
    expected_us = 1_000_000.0
    drift_ppm = float((dt_mcu.mean() - expected_us) / expected_us * 1e6)
    return {
        "drift_ppm":     round(drift_ppm, 2),
        "mcu_tick_mean_us": round(float(dt_mcu.mean()), 1),
        "note": "positive = MCU runs faster than GPS 1 Hz",
    }


# ── plots ──────────────────────────────────────────────────────────────────────
SENSOR_COLORS = {"imu": "#378ADD", "baro": "#1D9E75", "mag": "#D85A30", "gps": "#BA7517"}

def plot_jitter(dt_dict: dict, stats: dict, out_path: Path):
    sensors = [s for s in ["imu", "baro", "mag", "gps"] if len(dt_dict.get(s, [])) > 1]
    fig, axes = plt.subplots(1, len(sensors), figsize=(5 * len(sensors), 4))
    if len(sensors) == 1:
        axes = [axes]
    fig.suptitle("Sensor dt distributions (jitter)", fontsize=11)

    for ax, sensor in zip(axes, sensors):
        dt = dt_dict[sensor]
        color = SENSOR_COLORS.get(sensor, "#888")
        s = stats.get(sensor, {})
        expected_ms = 1000.0 / EXPECTED_HZ[sensor]

        # Clip at 99.5th percentile so tail outliers don't squash the histogram
        clip = float(dt.quantile(0.995))
        ax.hist(dt[dt <= clip], bins=80, color=color, alpha=0.75, edgecolor="none")
        ax.axvline(expected_ms, color="black",  lw=1.2, ls="--", label=f"expected {expected_ms:.1f} ms")
        ax.axvline(s.get("p50_dt_ms", 0), color="white", lw=1,   ls=":",  label=f"p50 {s.get('p50_dt_ms', 0):.1f} ms")
        ax.axvline(s.get("p95_dt_ms", 0), color="red",   lw=0.8, ls=":",  label=f"p95 {s.get('p95_dt_ms', 0):.1f} ms")

        ax.set_title(f"{sensor.upper()}  ·  actual {s.get('actual_hz', 0):.0f} Hz", fontsize=9)
        ax.set_xlabel("dt (ms)", fontsize=8)
        ax.set_ylabel("count", fontsize=8)
        ax.legend(fontsize=7, framealpha=0.5)
        ax.tick_params(labelsize=7)

    plt.tight_layout()
    plt.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close()


def plot_alignment(df: pd.DataFrame, wall_s: pd.Series, out_path: Path):
    fig = plt.figure(figsize=(14, 8))
    fig.suptitle("Inter-sensor alignment", fontsize=11)
    gs = gridspec.GridSpec(3, 1, hspace=0.5)

    # Panel 1: IMU dt over time — scheduling issues show as vertical spikes
    ax0 = fig.add_subplot(gs[0])
    dt_imu_ms = (df["t_imu_us"].diff() / 1000.0).clip(0, 50)
    ax0.plot(wall_s, dt_imu_ms, lw=0.3, color=SENSOR_COLORS["imu"], alpha=0.7)
    ax0.axhline(1000.0 / EXPECTED_HZ["imu"], color="red", lw=0.8, ls="--", alpha=0.6, label="expected")
    ax0.set_ylabel("IMU dt (ms)", fontsize=8)
    ax0.set_title("IMU inter-sample interval — spikes = scheduling issues", fontsize=8)
    ax0.legend(fontsize=7)
    ax0.tick_params(labelsize=7)

    # Panel 2: Baro update cadence — highlight async buffering lag
    ax1 = fig.add_subplot(gs[1], sharex=ax0)
    baro_change = df["pressure_hPa"].diff().abs() > 1e-4
    baro_t = wall_s[baro_change]
    ax1.scatter(baro_t.values, (df.loc[baro_change, "t_imu_us"].diff() / 1000.0).values,
                s=1, color=SENSOR_COLORS["baro"], alpha=0.5)
    ax1.axhline(1000.0 / EXPECTED_HZ["baro"], color="red", lw=0.8, ls="--", alpha=0.6, label="expected")
    ax1.set_ylabel("Baro dt (ms)", fontsize=8)
    ax1.set_title("Baro update interval — gaps = async read buffering", fontsize=8)
    ax1.legend(fontsize=7)
    ax1.tick_params(labelsize=7)

    # Panel 3: GPS timestamp lag vs IMU — shows sync/desync
    ax2 = fig.add_subplot(gs[2], sharex=ax0)
    gps_valid = (df["hdop"] < 2.5) & (df["t_gps_us"] > 0)
    lag_ms = ((df.loc[gps_valid, "t_imu_us"] - df.loc[gps_valid, "t_gps_us"]) / 1000.0).clip(-2000, 2000)
    ax2.plot(wall_s[gps_valid].values, lag_ms.values, lw=0.4, color=SENSOR_COLORS["gps"], alpha=0.8)
    ax2.axhline(0, color="black", lw=0.5, ls="--", alpha=0.4)
    ax2.set_ylabel("IMU − GPS lag (ms)", fontsize=8)
    ax2.set_xlabel("Wall-clock time (s)", fontsize=8)
    ax2.set_title("GPS timestamp lag — drift = MCU vs GPS clock skew", fontsize=8)
    ax2.tick_params(labelsize=7)

    plt.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close()


# ── main ───────────────────────────────────────────────────────────────────────
def run(session_dir: Path, version: int | None = None) -> Path:
    clean_dir = session_dir / "clean"
    diag_dir  = session_dir / "diagnostics"
    diag_dir.mkdir(parents=True, exist_ok=True)

    # Find signal file
    if version is None:
        signals = sorted(clean_dir.glob("signal_v*.csv"))
        if not signals:
            raise FileNotFoundError(f"No signal_v*.csv in {clean_dir}")
        sig_path = signals[-1]
        v = int(sig_path.stem.split("_v")[-1])
    else:
        sig_path = clean_dir / f"signal_v{version}.csv"
        v = version

    print("\n── Timing analysis ─────────────────────────────────")
    print(f"  Input : {sig_path.name}")

    df      = load_signal(sig_path)
    wall_s  = build_wallclock(df)
    dt_dict = extract_sensor_dt(df)

    # Stats
    stats   = {s: dt_stats(dt_dict[s], EXPECTED_HZ[s]) for s in EXPECTED_HZ if len(dt_dict.get(s, [])) > 1}
    missing = {s: missing_samples(dt_dict[s], EXPECTED_HZ[s]) for s in stats}
    drift   = clock_drift(df)

    # Print summary
    for sensor, s in stats.items():
        m = missing.get(sensor, {})
        print(f"  {sensor:<4}  expected {s['expected_hz']:>4} Hz  "
              f"actual {s['actual_hz']:>6.1f} Hz  "
              f"p50={s['p50_dt_ms']:.1f}ms  p95={s['p95_dt_ms']:.1f}ms  p99={s['p99_dt_ms']:.1f}ms  "
              f"gaps={m.get('gaps',0)}  dups={m.get('duplicates',0)}")
    if drift.get("drift_ppm") is not None:
        print(f"  clock drift: {drift['drift_ppm']:+.1f} ppm vs GPS")

    # Plots
    jitter_path    = diag_dir / f"jitter_v{v}.png"
    alignment_path = diag_dir / f"alignment_v{v}.png"
    plot_jitter(dt_dict, stats, jitter_path)
    plot_alignment(df, wall_s, alignment_path)
    print(f"  ✓ jitter    → {jitter_path.name}")
    print(f"  ✓ alignment → {alignment_path.name}")

    # timing_vi.json
    timing_json = {
        "version": v,
        "input_file": sig_path.name,
        "total_rows": len(df),
        "total_duration_s": round(float(wall_s.iloc[-1] - wall_s.iloc[0]), 2),
        "sensors": {
            sensor: {
                **stats[sensor],
                **missing.get(sensor, {}),
            }
            for sensor in stats
        },
        "clock_drift": drift,
        "plots": {
            "jitter":    jitter_path.name,
            "alignment": alignment_path.name,
        },
    }
    json_path = diag_dir / f"timing_v{v}.json"
    json_path.write_text(json.dumps(timing_json, indent=2), encoding="utf-8")
    print(f"  ✓ timing    → {json_path.name}")

    return json_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("session_dir", type=Path)
    parser.add_argument("--version", type=int, default=None)
    args = parser.parse_args()
    run(args.session_dir, args.version)
