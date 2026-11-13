#!/usr/bin/env python3
"""
session_trimmer.py  —  Segment a raw session CSV into signal and noise files.

Detects ESP32 reboot boundaries (backward jumps in t_imu_us > 0.5 s),
removes serial framing errors, and writes versioned outputs.

Called by run_pipeline.py or standalone:
    python pipeline/session_trimmer.py <session_dir>

Inputs:
    data/<session>/raw/raw.csv

Outputs:
    data/<session>/clean/signal_v<i>.csv   — largest clean segment
    data/<session>/clean/noise_v<i>.csv    — all other / noisy segments
    data/<session>/clean/cleaning_v<i>.json
"""

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd


# ── helpers ────────────────────────────────────────────────────────────────────

def load_raw(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, low_memory=False)
    df = df[df["t_imu_us"] != "t_imu_us"].copy()          # drop embedded headers
    for col in df.columns:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["t_imu_us"]).reset_index(drop=True)
    return df


def remove_framing_errors(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Drop rows where t_imu_us value leaked into sensor columns (serial misparse)."""
    bad = (df["gz_dps"].abs() > 1e6) | (df["mx_uT"].abs() > 1e6)
    return df[~bad].reset_index(drop=True), int(bad.sum())


def next_version(folder: Path, prefix: str) -> int:
    """Return the next unused version index for files like signal_v1.csv."""
    existing = list(folder.glob(f"{prefix}_v*.csv"))
    if not existing:
        return 1
    nums = []
    for f in existing:
        try:
            nums.append(int(f.stem.split("_v")[-1]))
        except ValueError:
            pass
    return max(nums, default=0) + 1


def segment_quality(seg: pd.DataFrame) -> dict:
    dur = float((seg.t_imu_us.iloc[-1] - seg.t_imu_us.iloc[0]) / 1e6)
    rate = float(len(seg) / dur) if dur > 0 else 0.0
    gz_std = float(seg.gz_dps.std())
    accel = np.sqrt(seg.ax_g**2 + seg.ay_g**2 + seg.az_g**2)
    gps_pct = float((seg.hdop < 2.5).mean() * 100)
    clean = bool(gz_std < 5 and dur > 30)   # explicit bool — numpy bool not JSON serializable
    return {
        "rows":       int(len(seg)),
        "duration_s": round(dur, 2),
        "rate_hz":    round(rate, 1),
        "gz_std":     round(gz_std, 4),
        "accel_mean": round(float(accel.mean()), 4),
        "gps_pct":    round(gps_pct, 1),
        "clean":      clean,
    }


# ── main ───────────────────────────────────────────────────────────────────────

def run(session_dir: Path) -> Path:
    raw_path = session_dir / "raw" / "raw.csv"
    clean_dir = session_dir / "clean"
    clean_dir.mkdir(parents=True, exist_ok=True)

    print("\n── Split sessions ──────────────────────────────────")
    print(f"  Input : {raw_path}")

    df = load_raw(raw_path)
    df, framing_removed = remove_framing_errors(df)

    if len(df) == 0:
        print("  ⚠ No data rows found in raw.csv. Creating empty output files.")
        v = next_version(clean_dir, "signal")
        signal_path = clean_dir / f"signal_v{v}.csv"
        noise_path  = clean_dir / f"noise_v{v}.csv"
        df.to_csv(signal_path, index=False)
        df.to_csv(noise_path, index=False)
        meta = {
            "version": v, "input_rows": 0, "output_signal_rows": 0, "output_noise_rows": 0,
            "rows_removed": framing_removed, "segments_detected": 0, "signal_segment_index": 1,
            "signal_quality": {}, "noise_segments": [], "reasons": ["empty file"],
            "files": {"signal": signal_path.name, "noise": noise_path.name}
        }
        meta_path = clean_dir / f"cleaning_v{v}.json"
        meta_path.write_text(json.dumps(meta, indent=2))
        return signal_path

    # Detect reboot boundaries
    dt = df["t_imu_us"].diff()
    gap_idx = df.index[dt < -500_000].tolist()
    boundaries = [0] + gap_idx + [len(df)]
    segments = [df.iloc[s:e].copy().reset_index(drop=True)
                for s, e in zip(boundaries, boundaries[1:])]

    total_rows  = len(df)
    n_segments  = len(segments)
    reasons     = []
    if framing_removed:
        reasons.append(f"{framing_removed} framing-error rows removed")
    if n_segments > 1:
        reasons.append(f"{n_segments - 1} ESP reboot gap(s) detected")

    print(f"  Rows  : {total_rows:,}  (framing errors removed: {framing_removed})")
    print(f"  Segs  : {n_segments}")

    # Evaluate each segment
    quals = [segment_quality(s) for s in segments]
    for i, q in enumerate(quals):
        tag = "✓ signal" if q["clean"] else "✗ noise"
        print(f"    seg {i+1}: {q['rows']:>6,} rows  {q['duration_s']:.0f}s  "
              f"gz_std={q['gz_std']:.3f}  {tag}")

    # Largest clean segment → signal; everything else → noise
    clean_idx = [i for i, q in enumerate(quals) if q["clean"]]
    if clean_idx:
        signal_idx = max(clean_idx, key=lambda i: quals[i]["rows"])
    else:
        # Fallback: longest segment regardless of cleanliness
        signal_idx = max(range(n_segments), key=lambda i: quals[i]["rows"])
        reasons.append("no clean segment found — longest used as signal")

    noise_segs = [s for i, s in enumerate(segments) if i != signal_idx]

    # Version numbering
    v = next_version(clean_dir, "signal")

    signal_path = clean_dir / f"signal_v{v}.csv"
    noise_path  = clean_dir / f"noise_v{v}.csv"

    segments[signal_idx].to_csv(signal_path, index=False)
    if noise_segs:
        pd.concat(noise_segs, ignore_index=True).to_csv(noise_path, index=False)
    else:
        # Write empty noise file with correct headers so downstream scripts don't break
        pd.DataFrame(columns=df.columns).to_csv(noise_path, index=False)

    # cleaning_vi.json
    meta = {
        "version":          v,
        "is_signal_valid":  bool(clean_idx),   # True only if a clean segment was found
        "input_rows":       total_rows + framing_removed,   # before framing removal
        "output_signal_rows": int(quals[signal_idx]["rows"]),
        "output_noise_rows":  int(sum(q["rows"] for i, q in enumerate(quals) if i != signal_idx)),
        "rows_removed":     framing_removed,
        "segments_detected": n_segments,
        "signal_segment_index": signal_idx + 1,
        "signal_quality":   quals[signal_idx],
        "noise_segments":   [quals[i] for i in range(n_segments) if i != signal_idx],
        "reasons":          reasons if reasons else ["clean single-session file"],
        "files": {
            "signal": signal_path.name,
            "noise":  noise_path.name,
        },
    }
    meta_path = clean_dir / f"cleaning_v{v}.json"
    meta_path.write_text(json.dumps(meta, indent=2))

    print(f"  ✓ signal → {signal_path.name}  ({quals[signal_idx]['rows']:,} rows)")
    print(f"  ✓ noise  → {noise_path.name}")
    print(f"  ✓ meta   → {meta_path.name}")

    return signal_path


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python pipeline/session_trimmer.py data/<session>")
        sys.exit(1)
    run(Path(sys.argv[1]))
