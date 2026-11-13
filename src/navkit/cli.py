"""The `nav` command."""

import argparse
import sys
from datetime import datetime
from pathlib import Path

from . import __version__, workspace

SESSION_TYPES = ["static_calib", "accel_6pos", "mag_rotation", "free_label"]
STEPS = ["trim", "diagnose", "calibrate", "plot"]


def _add_calib_options(p: argparse.ArgumentParser) -> None:
    g = p.add_mutually_exclusive_group()
    g.add_argument("--strict", dest="calib_mode", action="store_const", const="0",
                   help="abort if no calibration parameters exist")
    g.add_argument("--allow-uncalibrated", dest="calib_mode", action="store_const", const="1",
                   help="continue without calibration; the session is flagged not valid for analysis")
    p.add_argument("--calib-path", metavar="FILE", help="use this calibration_params.json")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nav",
        description="Host tools for the Multi-Sensor Navigation System: flash, log, process.",
    )
    parser.add_argument("--version", action="version", version=f"navkit {__version__}")
    parser.add_argument("-w", "--workspace", metavar="DIR",
                        help=f"workspace directory (default: ${workspace.ENV_VAR} or ~/navkit)")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    # ── board ────────────────────────────────────────────────────────────────
    p = sub.add_parser("flash", help="download a firmware release and flash it")
    p.add_argument("--board", default="auto",
                   help="board id (default: auto — detect the chip and pick the matching image)")
    p.add_argument("--version", dest="fw_version", default="latest", metavar="TAG",
                   help="firmware release to flash (default: latest)")
    p.add_argument("--file", metavar="IMAGE", help="flash this local merged image instead of a release")
    p.add_argument("--chip", help="esptool chip id for --file (default: detect)")
    p.add_argument("--port", help="serial port (default: auto-detect)")
    p.add_argument("--baud", type=int, default=921600)
    p.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")

    p = sub.add_parser("firmware", help="list or download firmware releases")
    fsub = p.add_subparsers(dest="firmware_command", metavar="<action>", required=True)
    fsub.add_parser("list", help="list published firmware releases and their boards")
    f = fsub.add_parser("fetch", help="download and verify an image without flashing")
    f.add_argument("--board", default="esp32dev")
    f.add_argument("--version", dest="fw_version", default="latest", metavar="TAG")

    sub.add_parser("ports", help="list serial ports that look like a board")

    p = sub.add_parser("doctor", help="check that this machine can flash and log")
    p.add_argument("--online", action="store_true", help="also check that firmware releases are reachable")

    # ── sessions ─────────────────────────────────────────────────────────────
    p = sub.add_parser("record", help="log a new session from the board, then process it")
    p.add_argument("--type", dest="session_type", choices=SESSION_TYPES, default="free_label")
    p.add_argument("--note", default="", help="free-text note stored with the session")
    p.add_argument("--port", help="serial port (default: auto-detect)")
    _add_calib_options(p)

    p = sub.add_parser("process", help="run the pipeline on an existing session")
    p.add_argument("session", help="session name, or path to its directory")
    p.add_argument("--from", dest="start_from", choices=STEPS, default="trim",
                   help="first step to run (default: trim)")
    _add_calib_options(p)

    p = sub.add_parser("calibrate", help="compute calibration parameters from a static session")
    p.add_argument("session", help="session name, or path to its directory")
    p.add_argument("--note", default="")

    sub.add_parser("sessions", help="list sessions in the workspace")

    p = sub.add_parser("log", help="log the serial stream to a CSV file, nothing else")
    p.add_argument("-o", "--output", metavar="FILE", help="output CSV (default: ./session_<time>.csv)")
    p.add_argument("--port", help="serial port (default: auto-detect)")
    p.add_argument("--timeout", type=int, default=30, help="seconds to wait for the firmware (default 30)")

    sub.add_parser("wizard", help="interactive menu that walks through everything")
    sub.add_parser("where", help="show where sessions, outputs and cached firmware live")
    return parser


# ── command implementations ────────────────────────────────────────────────────
def _port_or_exit(port: str | None) -> str:
    from . import ports
    port = port or ports.find_esp32_port()
    if port is None:
        print("ERROR: no board found. Plug it in, or pass --port. `nav doctor` can help.")
        sys.exit(1)
    return port


def cmd_flash(args, ws) -> int:
    from . import firmware, flash

    port = _port_or_exit(args.port)

    if args.file:
        image_path = Path(args.file).expanduser()
        if not image_path.is_file():
            print(f"ERROR: {image_path} does not exist.")
            return 1
        chip = args.chip or flash.detect_chip(port)
        label, offset = image_path.name, "0x0"
    else:
        try:
            board = args.board
            if board == "auto":
                chip = flash.detect_chip(port)
                available = firmware.boards(args.fw_version)
                matches = sorted(b for b, e in available.items() if e.get("chip", "esp32") == chip)
                if len(matches) != 1:
                    options = ", ".join(matches or sorted(available)) or "none"
                    print(f"ERROR: detected a {chip} chip; cannot choose a board automatically "
                          f"(candidates: {options}). Pass --board.")
                    return 1
                board = matches[0]
                print(f"  Detected {chip} → board '{board}'")
            image = firmware.resolve(board, args.fw_version)
            print(f"  Firmware {image.version} for {image.name or image.board}")
            image_path = firmware.fetch(image)
        except firmware.FirmwareError as e:
            print(f"ERROR: {e}")
            return 1
        if image.sha256:
            print(f"  Verified sha256 {image.sha256[:16]}…")
        else:
            print("  ! This release has no checksum; the download was not verified.")
        warning = firmware.check_protocol(image)
        if warning:
            print(f"  ! {warning}")
        chip, offset, label = image.chip, image.offset, f"{image.filename}"

    print(f"  Port {port}, chip {chip}")
    if not args.yes and sys.stdin.isatty():
        if input(f"  Flash {label}? This replaces the firmware on the board. [y/N] ").strip().lower() != "y":
            print("  Cancelled.")
            return 1
    flash.flash(image_path, port, chip=chip, offset=offset, baud=args.baud)
    print("\n  Done. Try `nav record` to log a session.")
    return 0


def cmd_firmware(args, ws) -> int:
    from . import firmware

    try:
        if args.firmware_command == "list":
            rels = firmware.releases()
            if not rels:
                print("  No firmware releases published yet.")
                return 0
            for rel in rels:
                m = firmware.manifest(rel)
                names = sorted(m["boards"]) if m else sorted(
                    a["name"] for a in rel.get("assets", []) if a["name"].endswith(".bin"))
                date = (rel.get("published_at") or "")[:10]
                print(f"  {rel['tag_name']:<10} {date:<11} {', '.join(names)}")
            return 0
        image = firmware.resolve(args.board, args.fw_version)
        path = firmware.fetch(image)
        print(f"  {image.version} for {image.board}: {path}")
        return 0
    except firmware.FirmwareError as e:
        print(f"ERROR: {e}")
        return 1


def cmd_ports(args, ws) -> int:
    from . import ports

    found = ports.candidates()
    if not found:
        print("  No ESP32-like serial ports found.")
        return 1
    for p in found:
        print(f"  {p.device:<28} {p.description}")
    return 0


def cmd_doctor(args, ws) -> int:
    from . import doctor
    return doctor.run(ws, online=args.online)


def _options(args):
    from .pipeline import Options
    return Options(
        session_type=getattr(args, "session_type", None),
        start_from=getattr(args, "start_from", None),
        user_note=getattr(args, "note", ""),
        calib_mode=getattr(args, "calib_mode", None),
        calib_path=getattr(args, "calib_path", None),
        port=getattr(args, "port", None),
    )


def _session_or_exit(ws, name: str) -> Path:
    try:
        return ws.session(name)
    except FileNotFoundError as e:
        print(f"ERROR: {e}")
        sys.exit(1)


def cmd_record(args, ws) -> int:
    from . import pipeline
    opts = _options(args)
    opts.port = _port_or_exit(args.port)
    pipeline.record(ws, opts)
    return 0


def cmd_process(args, ws) -> int:
    from . import pipeline
    pipeline.process(ws, _session_or_exit(ws, args.session), _options(args))
    return 0


def cmd_calibrate(args, ws) -> int:
    from . import pipeline
    pipeline.compute_calibration(ws, _session_or_exit(ws, args.session), args.note)
    return 0


def cmd_sessions(args, ws) -> int:
    from . import pipeline
    pipeline.print_sessions(ws)
    return 0


def cmd_log(args, ws) -> int:
    from . import logger
    out = Path(args.output) if args.output else Path(f"session_{datetime.now():%Y%m%d_%H%M%S}.csv")
    logger.log_session(out, port=_port_or_exit(args.port), timeout=args.timeout)
    return 0


def cmd_wizard(args, ws) -> int:
    from . import pipeline
    pipeline.wizard(ws)
    return 0


def cmd_where(args, ws) -> int:
    print(f"  workspace : {ws.root}")
    print(f"  sessions  : {ws.data_dir}")
    print(f"  outputs   : {ws.outputs_dir}")
    print(f"  calibration parameters : {ws.params_file}")
    print(f"  firmware cache : {workspace.cache_dir() / 'firmware'}")
    return 0


COMMANDS = {
    "flash": cmd_flash, "firmware": cmd_firmware, "ports": cmd_ports, "doctor": cmd_doctor,
    "record": cmd_record, "process": cmd_process, "calibrate": cmd_calibrate,
    "sessions": cmd_sessions, "log": cmd_log, "wizard": cmd_wizard, "where": cmd_where,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    ws = workspace.resolve(args.workspace)
    try:
        return COMMANDS[args.command](args, ws) or 0
    except KeyboardInterrupt:
        print("\n  Interrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
