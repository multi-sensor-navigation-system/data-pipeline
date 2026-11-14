#!/usr/bin/env python3
"""
run_pipeline.py — Backwards-compatible entry point.

Runs the interactive wizard with this checkout as the workspace, exactly as
before. The supported interface is now the `nav` command (see README):

    pip install -e ".[pipeline]"
    nav wizard
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(ROOT / "src"))

from navkit import pipeline, workspace  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="Nav-Stack Pipeline")
    parser.add_argument("--action", choices=["1", "2", "3", "4", "5"], help="Action to perform")
    parser.add_argument("--session-type", choices=list(pipeline.SESSION_TYPES.values()), help="For action 1")
    parser.add_argument("--session-dir", type=str, help="For actions 2, 3, 4")
    parser.add_argument("--start-from", choices=list(pipeline.PIPELINE_STEPS.values()), help="For action 3")
    parser.add_argument("--user-note", type=str, default="", help="For calibration")
    parser.add_argument("--calib-mode", choices=["0", "1"], help="0=abort if no calib, 1=continue")
    parser.add_argument("--calib-path", type=str, help="Custom path for calibration params")
    args, _ = parser.parse_known_args()

    opts = pipeline.Options(
        session_type=args.session_type, session_dir=args.session_dir, start_from=args.start_from,
        user_note=args.user_note, calib_mode=args.calib_mode, calib_path=args.calib_path,
    )
    pipeline.wizard(workspace.resolve(ROOT), opts, action=args.action)


if __name__ == "__main__":
    main()
