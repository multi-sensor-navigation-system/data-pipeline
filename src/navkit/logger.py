#!/usr/bin/env python3
"""
logger.py — Serial → CSV logger for the ESP32 nav-stack firmware.

Usage:
    python -m navkit.logger                        # auto-detect port
    python -m navkit.logger --port COM3            # Windows
    python -m navkit.logger --port /dev/ttyUSB0    # Linux
    python -m navkit.logger --port /dev/cu.usbserial-0001  # macOS

Output:
    logs/session_YYYYMMDD_HHMMSS.csv
    logs/firmware.json   — what the firmware said about itself (binary builds)

    A new file is created each run. Both firmware builds are understood:
    the default binary wire protocol and the CSV debug build (see wire.py).
"""

import argparse
import csv
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import serial

from . import wire
from .ports import find_esp32_port

# ── CONFIG ────────────────────────────────────────────
BAUD          = 921600
TIMEOUT_S     = 2.0
PREVIEW_EVERY = 500      # print a summary line every N rows
LOG_DIR       = Path("logs")

# Fault flag bitmask labels (matches config.h)
FAULT_LABELS = {
    0: "IMU",
    1: "MAG",
    2: "BARO",
    3: "GPS_HDOP",
    4: "GPS_NOFIX",
}


# ── FAULT FLAG DECODER ────────────────────────────────
def decode_faults(flag: int) -> str:
    if flag == 0:
        return "OK"
    active = [label for bit, label in FAULT_LABELS.items() if flag & (1 << bit)]
    return "|".join(active) if active else f"0x{flag:02X}"


# ── LIVE PREVIEW ──────────────────────────────────────
def preview(row: dict, row_count: int, t_start: float):
    elapsed   = time.time() - t_start
    rate      = row_count / elapsed if elapsed > 0 else 0
    fault_str = decode_faults(int(row.get("fault_flag", 0)))
    jitter_us = int(row.get("jitter_us", 0))

    print(
        f"\r  rows={row_count:>7,}  "
        f"rate={rate:>6.1f} Hz  "
        f"baro={float(row.get('pressure_hPa', 0)):.2f} hPa  "
        f"bmp_t={float(row.get('bmp_temp_C', 0)):.1f}°C  "
        f"hdop={float(row.get('hdop', 99)):.1f}  "
        f"sats={row.get('satellites', 0)}  "
        f"fault=[{fault_str}]  "
        f"jitter={jitter_us} μs  ",
        end="",
        flush=True,
    )


# ── LOGGING ───────────────────────────────────────────
def log_session(out_path: Path, port: str | None = None, timeout: int = 30) -> int:
    """Log the firmware CSV stream to ``out_path`` until Ctrl-C.

    Returns the number of rows written. Called by navkit.pipeline and by
    the standalone CLI below.
    """
    port = port or find_esp32_port()
    if port is None:
        print("ERROR: Could not auto-detect ESP32 port. Pass --port explicitly.")
        sys.exit(1)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"  Port     : {port} @ {BAUD} baud")
    print(f"  Output   : {out_path}")
    print("  Press Ctrl-C to stop logging.\n")

    rows_written = 0
    csv_writer   = None
    csv_file     = None
    decoder      = wire.StreamDecoder()
    last_drops   = 0
    t_start      = time.time()

    try:
        with serial.Serial(port, BAUD, timeout=TIMEOUT_S) as ser:
            print("  Opened serial port. Waiting for firmware…")

            # Pulse DTR to reset the ESP32 so it announces itself straight away.
            # (Binary firmware also repeats its hello every 2 s, CSV firmware
            # only prints its header at boot.)
            ser.dtr = False
            time.sleep(0.1)
            ser.dtr = True
            time.sleep(0.5)

            deadline = time.time() + timeout
            while True:
                chunk = ser.read(max(1, getattr(ser, "in_waiting", 0) or 1))
                if csv_writer is None and time.time() > deadline:
                    print("ERROR: Firmware did not identify itself within the timeout. Is it running?")
                    sys.exit(1)
                if not chunk:
                    continue

                for ev in decoder.feed(chunk):
                    if ev.kind == "log":
                        print(f"\n  [FW] {ev.payload}")

                    elif ev.kind == "hello":
                        if csv_writer is not None:
                            continue                      # periodic repeat
                        info = ev.payload
                        if info["proto"] not in (0, wire.PROTO_VERSION):
                            print(f"ERROR: Firmware speaks wire protocol v{info['proto']}; "
                                  f"this logger understands v{wire.PROTO_VERSION}. Update the pipeline.")
                            sys.exit(1)
                        if decoder.mode == "binary":
                            print(f"  Firmware : {info.get('fw', '?')} ({info.get('git', '?')}) "
                                  f"on {info.get('board', '?')}, sensors: {info.get('sensors', '?')}")
                            out_path.with_name("firmware.json").write_text(json.dumps(info, indent=2))
                        else:
                            print("  Firmware : CSV debug build")
                        csv_file   = open(out_path, "w", newline="", buffering=1)   # noqa: SIM115
                        csv_writer = csv.DictWriter(csv_file, fieldnames=decoder.header,
                                                    extrasaction="ignore")
                        csv_writer.writeheader()
                        t_start = time.time()
                        print(f"  Logging {decoder.mode} stream… (preview every {PREVIEW_EVERY} rows)\n")

                    elif ev.kind == "row":
                        if csv_writer is None:
                            continue                      # attached mid-stream: wait for the hello
                        csv_writer.writerow(ev.payload)
                        rows_written += 1
                        if rows_written % PREVIEW_EVERY == 0:
                            preview(ev.payload, rows_written, t_start)

                    elif ev.kind == "status":
                        if decoder.rb_drops > last_drops:
                            print(f"\n  [FW] rb_drops={decoder.rb_drops}")
                            last_drops = decoder.rb_drops

                    elif ev.kind == "bad":
                        # Malformed CSV row (e.g. partial write at startup) — show it instead of silently dropping
                        print(f"\n  [UNEXPECTED] {ev.payload}")

    except KeyboardInterrupt:
        print("\n\n  Stopped by user.")
    except serial.SerialException as e:
        print(f"\n  Serial error: {e}")
    finally:
        if csv_file:
            csv_file.close()

    elapsed = time.time() - t_start
    print(f"  Rows written : {rows_written:,}")
    print(f"  Duration     : {elapsed:.1f} s")
    if elapsed > 0:
        print(f"  Avg rate     : {rows_written/elapsed:.1f} Hz")
    if decoder.mode == "binary":
        print(f"  Lost frames  : {decoder.lost_frames:,}  (CRC errors: {decoder.crc_errors:,})")
    print(f"  FW rb_drops  : {decoder.rb_drops:,}")
    print(f"  File         : {out_path}")
    if rows_written > 0:
        print(f"  File size    : {out_path.stat().st_size / 1024:.1f} KB")
    return rows_written


# ── CLI ───────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="ESP32 nav-stack CSV logger")
    parser.add_argument("--port",    default=None, help="Serial port")
    parser.add_argument("--output",  default=None, help="Override output CSV path")
    parser.add_argument("--timeout", default=30, type=int,
                        help="Seconds to wait for first data (default 30)")
    args = parser.parse_args()

    if args.output:
        out_path = Path(args.output)
    else:
        session_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = LOG_DIR / f"session_{session_ts}.csv"

    log_session(out_path, port=args.port, timeout=args.timeout)


if __name__ == "__main__":
    main()
