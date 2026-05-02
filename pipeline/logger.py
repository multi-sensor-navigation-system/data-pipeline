#!/usr/bin/env python3
"""
nav_logger.py — Serial → CSV logger for the ESP32 nav-stack firmware.

Usage:
    python nav_logger.py                        # auto-detect port
    python nav_logger.py --port COM3            # Windows
    python nav_logger.py --port /dev/ttyUSB0   # Linux
    python nav_logger.py --port /dev/cu.usbserial-0001  # macOS

Output:
    logs/session_YYYYMMDD_HHMMSS.csv
    
    A new file is created each run. The CSV header is taken from the
    firmware's first non-comment line, so column names stay in sync
    automatically.
"""

import argparse
import csv
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import serial
import serial.tools.list_ports

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


# ── PORT AUTO-DETECT ──────────────────────────────────
def find_esp32_port() -> str | None:
    """Heuristic: look for Silicon Labs CP210x or CH340 (common ESP32 USB chips)."""
    candidates = []
    for p in serial.tools.list_ports.comports():
        desc = (p.description or "").lower()
        mfr  = (p.manufacturer or "").lower()
        if any(x in desc + mfr for x in ["cp210", "ch340", "esp32", "silicon"]):
            candidates.append(p.device)
    if len(candidates) == 1:
        return candidates[0]
    if candidates:
        print("Multiple ESP32-like ports found:")
        for i, c in enumerate(candidates):
            print(f"  [{i}] {c}")
        idx = int(input("Select index: "))
        return candidates[idx]
    return None


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


# ── MAIN ──────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="ESP32 nav-stack CSV logger")
    parser.add_argument("--port",    default=None, help="Serial port")
    parser.add_argument("--output",  default=None, help="Override output CSV path")
    parser.add_argument("--timeout", default=30, type=int,
                        help="Seconds to wait for first data (default 30)")
    args = parser.parse_args()

    port = args.port or find_esp32_port()
    if port is None:
        print("ERROR: Could not auto-detect ESP32 port. Pass --port explicitly.")
        sys.exit(1)

    session_ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
    if args.output:
        out_path = Path(args.output)
    else:
        LOG_DIR.mkdir(exist_ok=True)
        out_path = LOG_DIR / f"session_{session_ts}.csv"
    
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"  Port     : {port} @ {BAUD} baud")
    print(f"  Output   : {out_path}")
    print(f"  Press Ctrl-C to stop logging.\n")

    rows_written = 0
    csv_writer   = None
    csv_file     = None
    t_start      = time.time()

    try:
        with serial.Serial(port, BAUD, timeout=TIMEOUT_S) as ser:
            print(f"  Opened serial port. Waiting for firmware header…")

            # Pulse DTR to reset the ESP32 so it reprints the header.
            # Without this, if the ESP32 booted before we connected we miss
            # the header and time out.
            ser.dtr = False
            time.sleep(0.1)
            ser.dtr = True
            time.sleep(0.5)

            # Wait for header line (first non-comment line from firmware)
            header_cols = None
            deadline = time.time() + args.timeout
            while time.time() < deadline:
                raw = ser.readline()
                if not raw:
                    continue
                line = raw.decode("utf-8", errors="replace").strip()
                if line.startswith("#"):
                    print(f"  [FW] {line[2:].strip()}")
                    continue
                if "t_imu_us" in line:
                    header_cols = [c.strip() for c in line.split(",")]
                    print(f"  Header received ({len(header_cols)} columns).")
                    break

            if header_cols is None:
                print("ERROR: No header received within timeout. Is firmware running?")
                sys.exit(1)

            csv_file   = open(out_path, "w", newline="", buffering=1)
            csv_writer = csv.DictWriter(csv_file, fieldnames=header_cols,
                                        extrasaction="ignore")
            csv_writer.writeheader()

            print(f"  Logging… (preview every {PREVIEW_EVERY} rows)\n")

            while True:
                raw = ser.readline()
                if not raw:
                    continue
                line = raw.decode("utf-8", errors="replace").strip()

                # Pass firmware comments straight to terminal
                if line.startswith("#"):
                    print(f"\n  [FW] {line[2:].strip()}")
                    continue
                if not line:
                    continue

                cols = line.split(",")
                if len(cols) != len(header_cols):
                    # Malformed row (e.g. partial write at startup) — show it instead of silently dropping
                    print(f"\n  [UNEXPECTED] {line}")
                    continue

                row = dict(zip(header_cols, cols))
                csv_writer.writerow(row)
                rows_written += 1

                if rows_written % PREVIEW_EVERY == 0:
                    preview(row, rows_written, t_start)

    except KeyboardInterrupt:
        print(f"\n\n  Stopped by user.")
    except serial.SerialException as e:
        print(f"\n  Serial error: {e}")
    finally:
        if csv_file:
            csv_file.close()

    elapsed = time.time() - t_start
    print(f"  Rows written : {rows_written:,}")
    print(f"  Duration     : {elapsed:.1f} s")
    print(f"  Avg rate     : {rows_written/elapsed:.1f} Hz" if elapsed > 0 else "")
    print(f"  File         : {out_path}")
    print(f"  File size    : {out_path.stat().st_size / 1024:.1f} KB" if rows_written > 0 else "")


def main_with_path(raw_path: Path):
    """Entry point called by run_pipeline.py — logs directly to a given path."""
    sys.argv = ["nav_logger.py", "--output", str(raw_path)]
    main()


if __name__ == "__main__":
    main()
