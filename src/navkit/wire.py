#!/usr/bin/env python3
"""
wire.py — Decoder for the firmware's host stream.

The firmware speaks one of two formats on UART0:

  binary (default)   COBS( type | body | crc16 ) 0x00      — wire protocol v1
  csv    (debug)     "# comment" lines, a header line, then one CSV row per sample

StreamDecoder accepts raw bytes from the serial port, works out which format
it is looking at, and turns either one into the same stream of events, so the
logger does not care which firmware build is on the board.

The protocol is specified in the firmware repo: docs/wire-protocol.md.
"""

import binascii
import struct
from dataclasses import dataclass, field

PROTO_VERSION = 1

HELLO, SAMPLE, STATUS, LOG = 0x01, 0x02, 0x03, 0x04

# Column order of the CSV the pipeline works on (identical to the CSV firmware build).
CSV_COLUMNS = [
    "t_imu_us", "t_read_start_us", "t_proc_end_us", "t_gps_us",
    "ax_g", "ay_g", "az_g", "gx_dps", "gy_dps", "gz_dps",
    "mx_uT", "my_uT", "mz_uT",
    "pressure_hPa", "bmp_temp_C",
    "lat_deg", "lon_deg", "alt_gps_m", "hdop", "satellites",
    "fault_flag", "jitter_us",
]

# seq, t_imu_us, proc_us, t_gps_us, 6×imu, 3×mag, pressure, temp, lat, lon, alt, hdop, sats, fault, jitter
_SAMPLE = struct.Struct("<IQIQ6f3f2f2d2fBBI")
_STATUS = struct.Struct("<II")
assert _SAMPLE.size == 98

MAX_FRAME = 260   # longest legal frame is ~206 bytes; anything longer is noise


class FrameError(ValueError):
    """A frame that failed COBS decoding or its CRC."""


def crc16(data: bytes) -> int:
    """CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF)."""
    return binascii.crc_hqx(data, 0xFFFF)


def cobs_decode(data: bytes) -> bytes:
    out = bytearray()
    i, n = 0, len(data)
    while i < n:
        code = data[i]
        if code == 0:
            raise FrameError("zero byte inside frame")
        i += 1
        end = i + code - 1
        if end > n:
            raise FrameError("truncated COBS block")
        out += data[i:end]
        i = end
        if code != 0xFF and i < n:
            out.append(0)
    return bytes(out)


def decode_frame(frame: bytes) -> tuple[int, bytes]:
    """Decode one frame (without its 0x00 delimiter) into (type, body)."""
    raw = cobs_decode(frame)
    if len(raw) < 3:
        raise FrameError("frame too short")
    (crc,) = struct.unpack("<H", raw[-2:])
    if crc != crc16(raw[:-2]):
        raise FrameError("CRC mismatch")
    return raw[0], raw[1:-2]


def parse_hello(body: bytes) -> dict:
    """HELLO body → {"proto": int, "board": ..., "fw": ..., ...}. Unknown keys are kept."""
    if not body:
        raise FrameError("empty HELLO")
    info: dict = {"proto": body[0]}
    for pair in body[1:].decode("ascii", errors="replace").split(";"):
        key, sep, value = pair.partition("=")
        if sep:
            info[key] = value
    return info


def _f32(x: float) -> str:
    return format(x, ".9g")     # enough digits to round-trip a float32


def parse_sample(body: bytes) -> tuple[int, dict]:
    """SAMPLE body → (seq, row keyed by CSV_COLUMNS)."""
    if len(body) != _SAMPLE.size:
        raise FrameError(f"SAMPLE body is {len(body)} bytes, expected {_SAMPLE.size}")
    (seq, t_imu, proc, t_gps, ax, ay, az, gx, gy, gz, mx, my, mz,
     pressure, temp, lat, lon, alt, hdop, sats, fault, jitter) = _SAMPLE.unpack(body)
    values = [
        t_imu, t_imu, t_imu + proc, t_gps,
        _f32(ax), _f32(ay), _f32(az), _f32(gx), _f32(gy), _f32(gz),
        _f32(mx), _f32(my), _f32(mz),
        _f32(pressure), _f32(temp),
        repr(lat), repr(lon), _f32(alt), _f32(hdop), sats,
        fault, jitter,
    ]
    return seq, dict(zip(CSV_COLUMNS, (str(v) for v in values)))


def parse_status(body: bytes) -> dict:
    if len(body) != _STATUS.size:
        raise FrameError("bad STATUS body")
    rb_drops, samples_sent = _STATUS.unpack(body)
    return {"rb_drops": rb_drops, "samples_sent": samples_sent}


@dataclass
class Event:
    """One decoded item from the stream.

    kind      payload
    ───────   ─────────────────────────────────────────────
    hello     dict from parse_hello (binary) / {"proto": 0} (csv header seen);
              StreamDecoder.header holds the column names from here on
    row       dict keyed by column name
    status    {"rb_drops": int, "samples_sent": int}
    log       str — a firmware message
    bad       str — a malformed CSV line
    """
    kind: str
    payload: object = None


@dataclass
class StreamDecoder:
    mode: str | None = None          # None until detected, then "binary" or "csv"
    crc_errors: int = 0              # frames dropped after sync (binary)
    lost_frames: int = 0             # gaps in the sample sequence (binary)
    rb_drops: int = 0                # last value reported by the firmware
    header: list[str] | None = None
    _frames: bytearray = field(default_factory=bytearray)
    _lines: bytearray = field(default_factory=bytearray)
    _next_seq: int | None = None

    def feed(self, data: bytes) -> list[Event]:
        events: list[Event] = []
        if self.mode != "csv":
            self._frames += data
            self._drain_frames(events)
        if self.mode != "binary":
            self._lines += data
            self._drain_lines(events)
        return events

    # ── binary ────────────────────────────────────────────────────────────────
    def _drain_frames(self, events: list[Event]) -> None:
        while True:
            end = self._frames.find(0)
            if end < 0:
                if len(self._frames) > MAX_FRAME:      # noise with no delimiter
                    del self._frames[:-MAX_FRAME]
                return
            frame = bytes(self._frames[:end])
            del self._frames[:end + 1]
            if not frame:
                continue
            try:
                ftype, body = decode_frame(frame)
                event = self._frame_event(ftype, body)
            except FrameError:
                # Before sync this is boot noise or a partial first frame.
                if self.mode == "binary":
                    self.crc_errors += 1
                continue
            if self.mode is None:
                self.mode = "binary"
                self.header = list(CSV_COLUMNS)
                self._lines.clear()
            if event is not None:
                events.append(event)

    def _frame_event(self, ftype: int, body: bytes) -> Event | None:
        if ftype == SAMPLE:
            seq, row = parse_sample(body)
            if self._next_seq is not None and seq > self._next_seq:
                self.lost_frames += seq - self._next_seq
            self._next_seq = (seq + 1) & 0xFFFFFFFF   # a lower seq means the board rebooted
            return Event("row", row)
        if ftype == HELLO:
            return Event("hello", parse_hello(body))
        if ftype == STATUS:
            status = parse_status(body)
            self.rb_drops = status["rb_drops"]
            return Event("status", status)
        if ftype == LOG:
            return Event("log", body.decode("utf-8", errors="replace"))
        return None                                   # unknown type: ignore, stay in sync

    # ── csv ───────────────────────────────────────────────────────────────────
    def _drain_lines(self, events: list[Event]) -> None:
        while True:
            end = self._lines.find(b"\n")
            if end < 0:
                return
            line = self._lines[:end].decode("utf-8", errors="replace").strip()
            del self._lines[:end + 1]
            if not line:
                continue
            if line.startswith("#"):
                text = line[1:].strip()
                if text.startswith("rb_drops="):
                    try:
                        self.rb_drops = int(text.split("=", 1)[1])
                    except ValueError:
                        pass
                if self.mode == "csv" or text.isprintable():
                    events.append(Event("log", text))
                continue
            if self.header is None:
                if "t_imu_us" in line:
                    self.mode = "csv"
                    self.header = [c.strip() for c in line.split(",")]
                    self._frames.clear()
                    events.append(Event("hello", {"proto": 0}))
                continue
            cols = line.split(",")
            if cols[0].strip() == "t_imu_us":         # header reprinted after a reboot
                continue
            if len(cols) != len(self.header):
                events.append(Event("bad", line))
                continue
            events.append(Event("row", dict(zip(self.header, cols))))
