"""
pipeline.py — Orchestrates a session through the pipeline.

    1. logger       →  raw/raw.csv (+ raw/firmware.json)
    2. trimmer      →  clean/signal_vi.csv + noise_vi.csv + cleaning_vi.json
    3. diagnostics  →  diagnostics/ jitter + alignment plots + timing_vi.json
    4. calibrator   →  calibrated/calibrated_vi.csv + calibration_vi.json
    5. plotter      →  outputs/plots/<session>/ raw + clean + calibrated pngs
    6. (this file)  →  data/<session>/metadata.json + outputs/reports/<session>_summary.md

Used by the `nav` CLI (record / process / calibrate / sessions / wizard).
"""

import importlib
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .workspace import Workspace

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

ORDER = ["log", "trim", "diagnose", "calibrate", "plot"]


@dataclass
class Options:
    """Choices that would otherwise be asked interactively."""
    session_type: str | None = None
    session_dir: str | None = None
    start_from: str | None = None
    user_note: str = ""
    calib_mode: str | None = None      # "0" strict, "1" permissive
    calib_path: str | None = None
    port: str | None = None


def _stage(name: str):
    """Import a pipeline stage on demand, so `nav flash` never loads pandas."""
    os.environ.setdefault("MPLBACKEND", "Agg")      # stages only ever save figures
    try:
        return importlib.import_module(f"navkit.{name}")
    except ModuleNotFoundError as e:
        if e.name in {"pandas", "numpy", "scipy", "matplotlib"}:
            print(f"ERROR: this command needs the pipeline extras ({e.name} is missing).")
            print('       Install them with:  pip install "navkit[pipeline]"')
            sys.exit(1)
        raise


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


def write_summary(ws: Workspace, session_dir: Path, meta: dict,
                  timing_json: dict | None, calib_json: dict | None):
    rep_dir = ws.outputs_dir / "reports"
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
    print(f"\n  ✓ session summary → {out}")


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


def enforce_calibration_check(session_type: str, ws: Workspace, opts: Options) -> tuple[dict, Path]:
    """Decide how to proceed when calibration params may be missing.

    Returns (state, params_file): the calibration-state dict consumed by
    write_metadata() and the parameters file to use. Exits the process when
    the run must not continue.
    """
    state = {
        "is_calibrated": True,
        "user_overridden": False,
        "override_type": "none",
        "valid_for_analysis": True,
    }
    custom_state = {"user_overridden": True, "override_type": "custom path provided"}

    # An explicit calibration file wins over everything else.
    if opts.calib_path:
        p = Path(opts.calib_path).expanduser()
        if p.is_file():
            print(f"  Using custom params file: {p}")
            state.update(custom_state)
            return state, p
        print(f"  Custom params file not found: {p}. Aborting.")
        sys.exit(1)

    params_file = ws.params_file
    if session_type == "static_calib" or params_file.exists():
        return state, params_file

    print(f"\n  \033[93m⚠ WARNING: No calibration_params.json found at {params_file}\033[0m")
    print("  Dynamic sessions require calibration parameters to produce valid output.")

    mode = opts.calib_mode
    if mode is None and not sys.stdin.isatty():
        mode = "0"                                   # nobody to ask: be strict
    if mode == "0":
        print("  Aborting (strict mode). Run a static_calib session first, "
              "or allow an uncalibrated run explicitly.")
        sys.exit(1)
    if mode == "1":
        print("  Continuing without calibration (permissive mode).")
        state.update(UNCALIBRATED_STATE)
        return state, params_file

    opts_menu = {
        "0": "Abort pipeline (strict mode: run a static_calib session first)",
        "1": "Continue without calibration (permissive mode: NOT suitable for analysis)",
        "p": "Enter custom path to a calibration_params.json file",
    }
    choice = ask("How to proceed?", opts_menu, default="0")
    if choice == "1":
        print("  Continuing in permissive mode. 'calibrate' step will be skipped.\nOutputs NOT valid for analysis.\n")
        state.update(UNCALIBRATED_STATE)
        return state, params_file
    if choice == "p":
        p = Path(input("  Enter full path to parameters file: ").strip()).expanduser()
        if p.is_file():
            print(f"\n  Using custom params file: {p}")
            state.update(custom_state)
            return state, p
        print("  File not found. Aborting.")
        sys.exit(1)

    print("  Aborting. Please run a static_calib session to generate parameters.")
    sys.exit(1)


# ── pipeline runner ─────────────────────────────────────────────────────────────
def run_pipeline(ws: Workspace, session_dir: Path, session_type: str, start_from: str = "log",
                 params_file: Path | None = None, calib_state: dict | None = None,
                 user_note: str = "", port: str | None = None) -> dict:
    params_file  = params_file or ws.params_file
    steps_run    = []
    timing_json  = None
    calib_json   = None
    start_idx    = ORDER.index(start_from)

    # ── 1. Log ────────────────────────────────────────────────────────────────
    if ORDER.index("log") >= start_idx:
        logger = _stage("logger")
        raw_path = session_dir / "raw" / "raw.csv"
        print(f"\n\n  Logger will write to: {raw_path}")
        print("  Press Ctrl-C in the logger to stop and continue the pipeline.\n")
        logger.log_session(raw_path, port=port)
        steps_run.append("log")

    # ── 2. Trim ───────────────────────────────────────────────────────────────
    if ORDER.index("trim") >= start_idx:
        trimmer = _stage("trimmer")
        run_step("trimmer", trimmer.run, session_dir)
        steps_run.append("trim")

    # ── 3. Diagnose ───────────────────────────────────────────────────────────
    if ORDER.index("diagnose") >= start_idx:
        diagnostics = _stage("diagnostics")
        json_path = run_step("diagnostics", diagnostics.run, session_dir)
        timing_json = _load_json_safe(json_path)
        steps_run.append("diagnose")

    # ── 4. Calibrate ──────────────────────────────────────────────────────────
    if ORDER.index("calibrate") >= start_idx:
        calibrator = _stage("calibrator")

        # Always compute new parameters if this is a calibration session
        if session_type == "static_calib":
            print("\n  Computing NEW calibration params from this static session...")
            note = user_note
            if not note and sys.stdin.isatty():
                note = input("  Enter a note for this calibration (e.g., 'desk', 'bike') [none]: ").strip()
            params_file.parent.mkdir(parents=True, exist_ok=True)
            run_step("compute_params", calibrator.compute_params, session_dir, params_file, note)

        if not params_file.exists():
            print(f"\n  ⚠  calibration_params.json not found at {params_file}")
            print("  Run a static_calib session first to generate it.")
            print("  Skipping calibration application step.")
        else:
            run_step("calibrator", calibrator.apply_params, session_dir, params_file)
            v = _latest_v(session_dir / "calibrated", "calibration")
            calib_json = _load_json_safe(session_dir / "calibrated" / f"calibration_v{v}.json")
            steps_run.append("calibrate")

    # ── 5. Plot ───────────────────────────────────────────────────────────────
    if ORDER.index("plot") >= start_idx:
        plotter = _stage("plotter")
        plots_dir = ws.outputs_dir / "plots" / session_dir.name
        plots_dir.mkdir(parents=True, exist_ok=True)
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
    meta = write_metadata(session_dir, session_type, steps_run, timing_json, calib_json,
                          calib_state=calib_state, user_note=user_note)
    write_summary(ws, session_dir, meta, timing_json, calib_json)
    return meta


# ── operations (used by the CLI and the wizard) ────────────────────────────────
def new_session_dir(ws: Workspace, session_type: str) -> Path:
    session_dir = ws.data_dir / make_session_name(session_type)
    (session_dir / "raw").mkdir(parents=True, exist_ok=False)   # never overwrite a session
    return session_dir


def record(ws: Workspace, opts: Options) -> dict:
    """Log a new session from the board, then run the whole pipeline on it."""
    session_type = opts.session_type or "free_label"
    state, params = enforce_calibration_check(session_type, ws, opts)
    session_dir = new_session_dir(ws, session_type)
    print(f"\n  Session folder: {session_dir}")
    return run_pipeline(ws, session_dir, session_type, "log", params, state,
                        user_note=opts.user_note, port=opts.port)


def process(ws: Workspace, session_dir: Path, opts: Options) -> dict:
    """Run the pipeline on a session that already has raw data."""
    meta         = _load_json_safe(session_dir / "metadata.json") or {}
    session_type = opts.session_type or meta.get("session_type", "free_label")
    user_note    = opts.user_note or meta.get("user_note", "")
    start_from   = opts.start_from or "trim"
    if start_from == "log":
        raise ValueError("an existing session cannot be re-logged; start from 'trim' or later")
    state, params = enforce_calibration_check(session_type, ws, opts)
    return run_pipeline(ws, session_dir, session_type, start_from, params, state, user_note=user_note)


def compute_calibration(ws: Workspace, session_dir: Path, note: str = "") -> dict:
    """Compute calibration parameters from an existing static session."""
    calibrator = _stage("calibrator")
    ws.root.mkdir(parents=True, exist_ok=True)
    return calibrator.compute_params(session_dir, ws.params_file, note)


def list_sessions(ws: Workspace) -> list[dict]:
    rows = []
    for s in ws.sessions():
        meta = _load_json_safe(s / "metadata.json") or {}
        rows.append({
            "name": s.name,
            "session_type": meta.get("session_type", "?"),
            "duration_s": meta.get("duration_s"),
            "valid_for_analysis": meta.get("valid_for_analysis"),
        })
    return rows


def print_sessions(ws: Workspace) -> None:
    rows = list_sessions(ws)
    if not rows:
        print(f"  No sessions in {ws.data_dir}")
        return
    for r in rows:
        dur = f"{float(r['duration_s'])/60:.1f} min" if r["duration_s"] is not None else "?"
        valid = {True: "valid", False: "not valid", None: "?"}[r["valid_for_analysis"]]
        print(f"  {r['name']:<45}  {r['session_type']:<15}  {dur:<10}  {valid}")


# ── interactive wizard ─────────────────────────────────────────────────────────
def _pick_session(ws: Workspace, opts: Options) -> Path:
    if opts.session_dir:
        try:
            return ws.session(opts.session_dir)
        except FileNotFoundError as e:
            print(f"\n  {e}")
            sys.exit(1)
    sessions = ws.sessions()
    if not sessions:
        print(f"  No sessions found in {ws.data_dir}. Run a new session first.")
        sys.exit(0)
    menu = {str(i+1): s.name for i, s in enumerate(sessions[:10])}
    print("\nExisting sessions (most recent first):")
    return sessions[int(ask("Select session", menu, default="1")) - 1]


def wizard(ws: Workspace, opts: Options | None = None, action: str | None = None) -> None:
    """The menu-driven flow: walks through every choice interactively."""
    opts = opts or Options()
    hr("═")
    print("  Nav-Stack Pipeline")
    print(f"  Workspace: {ws.root}")
    hr("═")

    actions = {
        "1": "Start new session  (log → trim → diagnose → calibrate → plot)",
        "2": "Run pipeline on existing raw data",
        "3": "Re-run from a specific step",
        "4": "Compute calibration params from existing static session",
        "5": "List existing sessions",
    }
    action = action or ask("\nWhat would you like to do?", actions, default="1")

    if action == "1":
        if not opts.session_type:
            print("\nSession type:")
            opts.session_type = SESSION_TYPES[ask("Select type", SESSION_TYPES, default="1")]
        if not opts.user_note:
            opts.user_note = input("\n  Enter a note for this session (e.g., 'desk', 'bike ride') [none]: ").strip()
        record(ws, opts)

    elif action == "2":
        opts.start_from = "trim"
        process(ws, _pick_session(ws, opts), opts)

    elif action == "3":
        session_dir = _pick_session(ws, opts)
        if not opts.start_from:
            print("\nStart from step:")
            steps = {k: v for k, v in PIPELINE_STEPS.items() if v != "log"}
            opts.start_from = steps[ask("Select step", steps, default="2")]
        process(ws, session_dir, opts)

    elif action == "4":
        session_dir = _pick_session(ws, opts)
        note = opts.user_note or input(
            "\n  Enter a note for this calibration (e.g., 'static (desk)', 'dynamic (bike)') [none]: ").strip()
        compute_calibration(ws, session_dir, note)

    elif action == "5":
        print()
        print_sessions(ws)
        return

    hr("═")
    print("  Pipeline complete.")
    hr("═")
