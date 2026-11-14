#!/usr/bin/env python3
"""
run_pipeline.py  —  Single entry point for the nav-stack data pipeline.

Run from the project root:
    python run_pipeline.py

No arguments needed — the script walks you through everything interactively.

Pipeline order:
    1. logger.py          →  raw/raw.csv
    2. session_trimmer.py →  clean/signal_vi.csv + noise_vi.csv + cleaning_vi.json
    3. diagnostics.py     →  diagnostics/ jitter + alignment plots + timing_vi.json
    4. calibrator.py      →  calibrated/calibrated_vi.csv + calibration_vi.json
    5. plotter.py         →  outputs/plots/<session>/ raw + clean + calibrated pngs
    6. (this script)      →  data/<session>/metadata.json + outputs/reports/session_level/<session>_summary.md
"""

import argparse
import importlib.util
import json
import sys
import time
from datetime import datetime
from pathlib import Path

# ── project paths ───────────────────────────────────────────────────────────────
ROOT         = Path(__file__).parent.resolve()
PIPELINE_DIR = ROOT / "pipeline"
DATA_DIR     = ROOT / "data"
OUTPUTS_DIR  = ROOT / "outputs"
PARAMS_FILE  = ROOT / "calibration_params.json"

SESSION_TYPES = {
    "1": "static_calib",
    "2": "accel_6pos",
    "3": "mag_rotation",
    "4": "free_label",
}

PIPELINE_STEPS = {
    "1": "log",
    "2": "trim",
    "3": "diagnose",
    "4": "calibrate",
    "5": "plot",
}


# ── helpers ─────────────────────────────────────────────────────────────────────
def hr(char="─", width=52):
    print(char * width)


def ask(prompt: str, options: dict, default: str | None = None) -> str:
    for k, v in options.items():
        print(f"\n  [{k}] {v}")
    suffix = f" (default {default})" if default else ""
    while True:
        choice = input(f"\n{prompt}{suffix}: ").strip() or default
        if choice in options:
            return choice
        print(f"\n  Please enter one of: {', '.join(options.keys())}")


def run_step(label: str, fn, *args, **kwargs):
    print(f"\n\n{'─'*52}")
    print(f"\n  Running: {label}")
    print(f"\n{'─'*52}")
    t0 = time.time()
    result = fn(*args, **kwargs)
    elapsed = time.time() - t0
    print(f"\n  ✓ done in {elapsed:.1f}s")
    return result


def import_pipeline(name: str):
    spec = importlib.util.spec_from_file_location(name, PIPELINE_DIR / f"{name}.py")
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── session name generation ─────────────────────────────────────────────────────
def make_session_name(session_type: str) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"session_{ts}_{session_type}"


# ── metadata + summary ──────────────────────────────────────────────────────────
def write_metadata(session_dir: Path, session_type: str, steps_run: list[str],
                   timing_json: dict | None, calib_json: dict | None,
                   calib_state: dict | None = None, user_note: str = ""):
    meta = {
        "session_name":  session_dir.name,
        "session_type":  session_type,
    }

    # Add calibration metadata at the top
    if calib_state:
        meta.update({
            "is_calibrated":      calib_state.get("is_calibrated", True),
            "calibration_source": calib_json.get("params_source_session", "none") if calib_json else "none",
            "calibration_valid":  calib_json.get("calibration_valid", True) if calib_json else True,
            "user_overridden":    calib_state.get("user_overridden", False),
            "override_type":      calib_state.get("override_type", "none"),
            "valid_for_analysis": calib_state.get("valid_for_analysis", True),
        })
        if "reason" in calib_state:
            meta["reason"] = calib_state["reason"]
    else:
        # Default for legacy sessions or static_calib where no check was needed
        meta.update({
            "is_calibrated":      True,
            "calibration_source": calib_json.get("params_source_session", "none") if calib_json else "none",
            "calibration_valid":  calib_json.get("calibration_valid", True) if calib_json else True,
            "user_overridden":    False,
            "override_type":      "none",
            "valid_for_analysis": True,
        })

    # Enforce consistency: if internal calibration checks failed, it's NOT valid for analysis
    if not meta["calibration_valid"]:
        meta["valid_for_analysis"] = False
        if "reason" not in meta:
            meta["reason"] = "Internal calibration check failed (insufficient signal or failed sanity limits)."

    # Dynamic sensor detection from timing analysis
    sensor_map = {"imu": "MPU-6050", "mag": "QMC5883L", "baro": "BMP280", "gps": "NEO-6M GPS"}
    if timing_json and "sensors" in timing_json:
        detected = [sensor_map[s] for s in sensor_map if s in timing_json["sensors"]]
    else:
        detected = list(sensor_map.values())

    # Identity reported by the firmware itself (binary builds only).
    firmware = _load_json_safe(session_dir / "raw" / "firmware.json")

    meta.update({
        "created_at":    datetime.now().isoformat(timespec="seconds"),
        "user_note":     user_note,
        "platform":      "ESP32 nav-stack v1",
        "firmware":      firmware,
        "sensors":       detected,
        "steps_run":     steps_run,
        "files": {
            "raw":         "raw/raw.csv",
            "signal":      f"clean/signal_v{_latest_v(session_dir/'clean','signal')}.csv",
            "calibrated":  f"calibrated/calibrated_v{_latest_v(session_dir/'calibrated','calibrated')}.csv",
            "timing":      f"diagnostics/timing_v{_latest_v(session_dir/'diagnostics','timing')}.json",
        },
    })

    if timing_json:
        meta["duration_s"]  = timing_json.get("total_duration_s")
        meta["total_rows"]  = timing_json.get("total_rows")
        meta["imu_hz"]      = timing_json.get("sensors", {}).get("imu", {}).get("actual_hz")
        meta["gps_hdop_ok"] = True   # already filtered in timing analysis
    if calib_json:
        meta["gyro_bias"]   = calib_json.get("gyro_bias")
        meta["accel_scale"] = calib_json.get("accel_scale")

    (session_dir / "metadata.json").write_text(json.dumps(meta, indent=2))
    print("\n  ✓ metadata.json written")
    return meta


def write_summary(session_dir: Path, meta: dict,
                  timing_json: dict | None, calib_json: dict | None):
    rep_dir = OUTPUTS_DIR / "reports"
    rep_dir.mkdir(parents=True, exist_ok=True)

    imu  = (timing_json or {}).get("sensors", {}).get("imu", {})
    gps  = (timing_json or {}).get("sensors", {}).get("gps", {})
    dur  = meta.get("duration_s", "?")
    dur_min = f"{float(dur)/60:.1f}" if dur != "?" else "?"

    lines = [
        f"# Session {session_dir.name} summary",
        "",
        "## Overview",
        f"- Duration     : {dur_min} min ({dur} s)",
        f"- Session type : {meta.get('session_type', '?')}",
        f"- Platform     : {meta.get('platform', '?')}",
        f"- Sensors      : {', '.join(meta.get('sensors', []))}",
        "",
        "## Timing",
        f"- IMU  : p50={imu.get('p50_dt_ms','?')} ms  "
        f"p95={imu.get('p95_dt_ms','?')} ms  "
        f"p99={imu.get('p99_dt_ms','?')} ms  "
        f"actual={imu.get('actual_hz','?')} Hz",
        f"- GPS  : p50={gps.get('p50_dt_ms','?')} ms",
        f"- Gaps : {imu.get('gaps','?')}  Dups : {imu.get('duplicates','?')}",
        "",
    ]

    if calib_json:
        gb = calib_json.get("gyro_bias", [])
        lines += [
            "## Calibration",
            f"- Gyro bias  : {gb}  °/s",
            f"- Post-cal |a| : {calib_json.get('post_cal_accel_mean_g','?')} g  (target 1.0)",
            f"- Post-cal |B| : {calib_json.get('post_cal_mag_mean_uT','?')} μT",
            f"- Baro temp R² : {calib_json.get('baro_model_r2','?')}",
            "",
        ]

    lines += [
        "## Links",
        f"- [Jitter plot](../../data/{session_dir.name}/diagnostics/jitter_v1.png)",
        f"- [Alignment plot](../../data/{session_dir.name}/diagnostics/alignment_v1.png)",
        f"- [Calibrated data](../../data/{session_dir.name}/calibrated/)",
        f"- [Raw plots](../plots/{session_dir.name}/)",
    ]

    out = rep_dir / f"{session_dir.name}_summary.md"
    out.write_text("\n".join(lines))
    print(f"\n  ✓ session summary → {out.relative_to(ROOT)}")


def _latest_v(folder: Path, prefix: str) -> int:
    if not folder.exists():
        return 1
    files = list(folder.glob(f"{prefix}_v*.csv")) + list(folder.glob(f"{prefix}_v*.json"))
    if not files:
        return 1
    nums = []
    for f in files:
        try:
            nums.append(int(f.stem.split("_v")[-1]))
        except ValueError:
            pass
    return max(nums, default=1)


def _load_json_safe(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


UNCALIBRATED_STATE = {
    "is_calibrated": False,
    "user_overridden": True,
    "override_type": "allowed uncalibrated",
    "valid_for_analysis": False,
    "reason": "Missing calibration parameters; run was forced in permissive mode.",
}


def enforce_calibration_check(session_type: str, args) -> dict:
    """Decide how to proceed when calibration params may be missing.

    Always returns the calibration-state dict consumed by write_metadata(),
    or exits the process when the run must not continue.
    """
    global PARAMS_FILE

    state = {
        "is_calibrated": True,
        "user_overridden": False,
        "override_type": "none",
        "valid_for_analysis": True
    }

    if session_type == "static_calib" or PARAMS_FILE.exists():
        return state

    print(f"\n  \033[93m⚠ WARNING: No calibration_params.json found at {PARAMS_FILE}\033[0m")
    print("  Dynamic sessions require calibration parameters to produce valid output.")

    # An explicit --calib-path wins over --calib-mode (documented behaviour).
    if args.calib_path:
        p = Path(args.calib_path)
        if p.is_file():
            PARAMS_FILE = p
            print(f"  [Auto] Using custom params file: {PARAMS_FILE}")
            state.update({
                "user_overridden": True,
                "override_type": "custom path provided",
            })
            return state
        print("  [Auto] Custom params file not found. Aborting.")
        sys.exit(1)

    if args.calib_mode == "0":
        print("  [Auto] Aborting (strict mode).")
        sys.exit(1)
    if args.calib_mode == "1":
        print("  [Auto] Continuing without calibration (permissive mode).")
        state.update(UNCALIBRATED_STATE)
        return state

    opts = {
        "0": "Abort pipeline (strict mode: run a static_calib session first)",
        "1": "Continue without calibration (permissive mode: NOT suitable for analysis)",
        "p": "Enter custom path to a calibration_params.json file",
    }
    choice = ask("How to proceed?", opts, default="0")
    if choice == "1":
        print("  Continuing in permissive mode. 'calibrate' step will be skipped.\nOutputs NOT valid for analysis.\n")
        state.update(UNCALIBRATED_STATE)
        return state
    if choice == "p":
        custom_path = input("  Enter full path to parameters file: ").strip()
        p = Path(custom_path)
        if p.is_file():
            PARAMS_FILE = p
            print(f"\n  Using custom params file: {PARAMS_FILE}")
            state.update({
                "user_overridden": True,
                "override_type": "custom path provided",
            })
            return state
        print("  File not found. Aborting.")
        sys.exit(1)

    print("  Aborting. Please run a static_calib session to generate parameters.")
    sys.exit(1)


# ── main menu ──────────────────────────────────────────────────────────────────
def menu_new_session(args) -> tuple[str, Path, str]:
    print("\nSession type:")
    session_type = args.session_type if args.session_type else SESSION_TYPES[ask("Select type", SESSION_TYPES, default="1")]
    name = make_session_name(session_type)
    session_dir = DATA_DIR / name
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "raw").mkdir(exist_ok=True)
    print(f"\n\n  Session folder: data/{name}")

    # Prompt for session note for all new sessions
    user_note = args.user_note if args.user_note else input("\n  Enter a note for this session (e.g., 'desk', 'bike ride') [none]: ").strip()

    return session_type, session_dir, user_note


def menu_existing_session(args) -> Path:
    sessions = sorted([d for d in DATA_DIR.iterdir() if d.is_dir()], reverse=True)
    if not sessions:
        print("  No sessions found in data/. Run a new session first.")
        sys.exit(0)
    opts = {str(i+1): s.name for i, s in enumerate(sessions[:10])}
    if args.session_dir:
        p = Path(args.session_dir)
        if p.exists(): return p
        print(f"\n  Session dir {p} not found.")
        sys.exit(1)
    print("\nExisting sessions (most recent first):")
    idx = ask("Select session", opts, default="1")
    return sessions[int(idx)-1]


def menu_from_step(args) -> str:
    print("\nStart from step:")
    # Filter out 'log' since re-running from log on an existing session is redundant
    steps = {k: v for k, v in PIPELINE_STEPS.items() if v != "log"}
    return args.start_from if args.start_from else steps[ask("Select step", steps, default="2")]


# ── pipeline runner ─────────────────────────────────────────────────────────────
def run_pipeline(session_dir: Path, session_type: str, start_from: str = "log", args=None, calib_state=None, user_note: str = ""):
    steps_run    = []
    timing_json  = None
    calib_json   = None
    order        = ["log", "trim", "diagnose", "calibrate", "plot"]
    start_idx    = order.index(start_from)

    # ── 1. Log ────────────────────────────────────────────────────────────────
    if order.index("log") >= start_idx:
        logger = import_pipeline("logger")
        raw_path = session_dir / "raw" / "raw.csv"
        print(f"\n\n  Logger will write to: data/{session_dir.name}/raw/raw.csv")
        print("  Press Ctrl-C in the logger to stop and continue the pipeline.\n")
        logger.log_session(raw_path)
        steps_run.append("log")

    # ── 2. Trim ───────────────────────────────────────────────────────────────
    if order.index("trim") >= start_idx:
        trimmer = import_pipeline("session_trimmer")
        run_step("session_trimmer", trimmer.run, session_dir)
        steps_run.append("trim")

    # ── 3. Diagnose ───────────────────────────────────────────────────────────
    if order.index("diagnose") >= start_idx:
        diagnostics = import_pipeline("diagnostics")
        json_path = run_step("diagnostics", diagnostics.run, session_dir)
        timing_json = _load_json_safe(json_path)
        steps_run.append("diagnose")

    # ── 4. Calibrate ──────────────────────────────────────────────────────────
    if order.index("calibrate") >= start_idx:
        calibrator = import_pipeline("calibrator")

        # Always compute new parameters if this is a calibration session
        if session_type == "static_calib":
            print("\n  Computing NEW calibration params from this static session...")
            # Use the session note if we're in Action 1, otherwise prompt
            note = user_note if user_note else input("  Enter a note for this calibration (e.g., 'desk', 'bike') [none]: ").strip()
            run_step("compute_params", calibrator.compute_params, session_dir, PARAMS_FILE, note)

        if not PARAMS_FILE.exists():
            print(f"\n  ⚠  calibration_params.json not found at {PARAMS_FILE}")
            print("  Run a static_calib session first to generate it.")
            print("  Skipping calibration application step.")
        else:
            run_step("calibrator", calibrator.apply_params, session_dir, PARAMS_FILE)
            v = _latest_v(session_dir / "calibrated", "calibration")
            calib_json = _load_json_safe(session_dir / "calibrated" / f"calibration_v{v}.json")
            steps_run.append("calibrate")

    # ── 5. Plot ───────────────────────────────────────────────────────────────
    if order.index("plot") >= start_idx:
        plotter = import_pipeline("plotter")
        plots_dir = OUTPUTS_DIR / "plots" / session_dir.name
        plots_dir.mkdir(parents=True, exist_ok=True)
        # Plot raw, clean, calibrated
        raw_csv = session_dir / "raw" / "raw.csv"
        sig_csv = sorted((session_dir / "clean").glob("signal_v*.csv"))
        cal_csv = sorted((session_dir / "calibrated").glob("calibrated_v*.csv"))
        for csv, tag in ([(raw_csv, "raw")] +
                         [(p, f"clean_v{i+1}") for i, p in enumerate(sig_csv)] +
                         [(p, f"calibrated_v{i+1}") for i, p in enumerate(cal_csv)]):
            if csv.exists():
                run_step(f"plotter ({tag})", plotter.run,
                         csv, plots_dir / f"{tag}.png")
        steps_run.append("plot")

    # ── 6. Metadata + summary ─────────────────────────────────────────────────
    print(f"\n\n{'─'*52}")
    print("  Finalising metadata & summary")
    print(f"\n{'─'*52}")
    v_t = _latest_v(session_dir / "diagnostics", "timing")
    timing_json = timing_json or _load_json_safe(
        session_dir / "diagnostics" / f"timing_v{v_t}.json")
    v_c = _latest_v(session_dir / "calibrated", "calibration")
    calib_json = calib_json or _load_json_safe(
        session_dir / "calibrated" / f"calibration_v{v_c}.json")
    meta = write_metadata(session_dir, session_type, steps_run, timing_json, calib_json, calib_state=calib_state, user_note=user_note)
    write_summary(session_dir, meta, timing_json, calib_json)


# ── entry point ────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Nav-Stack Pipeline")
    parser.add_argument("--action", choices=["1", "2", "3", "4", "5"], help="Action to perform")
    parser.add_argument("--session-type", choices=list(SESSION_TYPES.values()), help="For action 1")
    parser.add_argument("--session-dir", type=str, help="For actions 2, 3, 4")
    parser.add_argument("--start-from", choices=list(PIPELINE_STEPS.values()), help="For action 3")
    parser.add_argument("--user-note", type=str, default="", help="For calibration")
    parser.add_argument("--calib-mode", choices=["0", "1"], help="0=abort if no calib, 1=continue")
    parser.add_argument("--calib-path", type=str, help="Custom path for calibration params")
    args, _ = parser.parse_known_args()

    hr("═")
    print("  Nav-Stack Pipeline")
    hr("═")

    actions = {
        "1": "Start new session  (log → trim → diagnose → calibrate → plot)",
        "2": "Run pipeline on existing raw data",
        "3": "Re-run from a specific step",
        "4": "Compute calibration params from existing static session",
        "5": "List existing sessions",
    }
    action = args.action if args.action else ask("\nWhat would you like to do?", actions, default="1")

    if action == "1":
        session_type, session_dir, user_note = menu_new_session(args)
        cs = enforce_calibration_check(session_type, args)
        run_pipeline(session_dir, session_type, start_from="log", args=args, calib_state=cs, user_note=user_note)

    elif action == "2":
        session_dir  = menu_existing_session(args)
        meta_path    = session_dir / "metadata.json"
        meta         = _load_json_safe(meta_path) or {}
        session_type = meta.get("session_type", "free_label")
        user_note    = meta.get("user_note", "")
        cs = enforce_calibration_check(session_type, args)
        run_pipeline(session_dir, session_type, start_from="trim", args=args, calib_state=cs, user_note=user_note)

    elif action == "3":
        session_dir  = menu_existing_session(args)
        meta_path    = session_dir / "metadata.json"
        meta         = _load_json_safe(meta_path) or {}
        session_type = meta.get("session_type", "free_label")
        user_note    = meta.get("user_note", "")
        start_from   = menu_from_step(args)
        cs = enforce_calibration_check(session_type, args)
        run_pipeline(session_dir, session_type, start_from=start_from, args=args, calib_state=cs, user_note=user_note)

    elif action == "4":
        session_dir = menu_existing_session(args)
        calibrator = import_pipeline("calibrator")
        user_note = input("\n  Enter a note for this calibration (e.g., 'static (desk)', 'dynamic (bike)') [none]: ").strip()
        calibrator.compute_params(session_dir, PARAMS_FILE, user_note)

    elif action == "5":
        sessions = sorted([d for d in DATA_DIR.iterdir() if d.is_dir()], reverse=True)
        print()
        for s in sessions:
            meta = _load_json_safe(s / "metadata.json") or {}
            dur  = f"{float(meta['duration_s'])/60:.1f} min" \
                   if "duration_s" in meta else "?"
            print(f"\n  {s.name:<45}  {meta.get('session_type','?'):<15}  {dur}")
        return

    hr("═")
    print("  Pipeline complete.")
    hr("═")


if __name__ == "__main__":
    main()
