"""Per-device settings: which GPIO carries each sensor signal on one board.

Every board reports a unique id (its chip's factory MAC address) in its
HELLO. The firmware stores that board's pins on the chip and checks every
change itself; this module sends the commands, explains refusals, and keeps
a copy of each board's pins in the workspace so they can be put back after
a full flash erase:

    <workspace>/devices/<id>.json

The commands are specified in the firmware repo: docs/wire-protocol.md.
"""

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from . import wire

BAUD = 921600
HELLO_TIMEOUT_S = 10.0      # the binary firmware repeats its hello every 2 s
ACK_TIMEOUT_S = 1.0
ATTEMPTS = 3

_ID = re.compile(r"[0-9a-f]{12}")


class DeviceError(RuntimeError):
    """The device refused a command, did not answer, or cannot do this."""


# ── checking a choice before it is sent ────────────────────────────────────────
def pin_errors(pins: dict, rules: dict) -> list[str]:
    """Every reason `pins` cannot be used, given a board's `pin_rules` from the
    firmware release manifest. Empty means the device will accept it."""
    errors = []
    for signal in wire.PIN_SIGNALS:
        gpio = pins[signal]
        if gpio not in rules["gpio"]:
            errors.append(f"{signal}: GPIO {gpio} does not exist on this chip")
        elif str(gpio) in rules["blocked"]:
            errors.append(f"{signal}: GPIO {gpio} is {rules['blocked'][str(gpio)]}")
        elif signal != "gps_rx" and gpio in rules["input_only"]:
            errors.append(f"{signal}: GPIO {gpio} is input-only")
    used = [pins[s] for s in wire.PIN_SIGNALS]
    for gpio in sorted({g for g in used if used.count(g) > 1}):
        shared = ", ".join(s for s in wire.PIN_SIGNALS if pins[s] == gpio)
        errors.append(f"GPIO {gpio} is assigned to more than one signal ({shared})")
    return errors


# ── talking to the board ───────────────────────────────────────────────────────
class Link:
    """Commands and answers over an open serial port (anything with
    ``read(n)`` and ``write(bytes)``; ``read`` must time out, not block)."""

    def __init__(self, port, clock=time.monotonic):
        self.port = port
        self.clock = clock
        self.decoder = wire.StreamDecoder()

    def _events(self, timeout: float):
        deadline = self.clock() + timeout
        while self.clock() < deadline:
            chunk = self.port.read(max(1, getattr(self.port, "in_waiting", 0) or 1))
            if chunk:
                yield from self.decoder.feed(chunk)

    def hello(self, timeout: float = HELLO_TIMEOUT_S, accept=lambda info: True) -> dict:
        """The next HELLO that `accept` approves of."""
        for ev in self._events(timeout):
            if ev.kind == "hello" and ev.payload.get("proto") and accept(ev.payload):
                return ev.payload
        if self.decoder.mode == "csv":
            raise DeviceError("this board runs the CSV debug firmware, which takes no commands")
        raise DeviceError("the board did not identify itself; is the firmware running?")

    def command(self, frame: bytes, cmd: int) -> dict:
        """Send a command and return its ACK. Retries, since a frame can be lost."""
        for _ in range(ATTEMPTS):
            self.port.write(frame)
            for ev in self._events(ACK_TIMEOUT_S):
                if ev.kind == "ack" and ev.payload["cmd"] == cmd:
                    return ev.payload
        raise DeviceError("the board did not answer; its firmware may predate pin settings "
                          "(update it with `nav flash`)")


def identify(link: Link) -> tuple[dict, dict]:
    """(hello, pins) of the connected board. Fails for firmware without pin settings."""
    info = link.hello()
    pins = wire.hello_pins(info)
    if pins is None or not _ID.fullmatch(info.get("id", "")):
        raise DeviceError(f"firmware {info.get('fw', '?')} on this board does not support pin settings; "
                          "update it with `nav flash`")
    return info, pins


def _refusal(ack: dict, pins: dict | None) -> str:
    reason = wire.ACK_STATUS.get(ack["status"], f"status {ack['status']}")
    signal = ack["signal"]
    if signal and pins:
        return f"{signal}: GPIO {pins[signal]} {reason}"
    return reason


def set_pins(link: Link, pins: dict) -> dict:
    """Store `pins` on the board. Returns the HELLO it sends after restarting
    with them. The device checks the pins itself and stores nothing it rejects."""
    ack = link.command(wire.set_pins_frame(pins), wire.SET_PINS)
    if ack["status"] != 0:
        raise DeviceError(_refusal(ack, pins))
    return link.hello(accept=lambda info: wire.hello_pins(info) == pins and info.get("pin_src") == "stored")


def reset_pins(link: Link) -> dict:
    """Forget the board's stored pins. Returns the HELLO after it restarts."""
    ack = link.command(wire.reset_pins_frame(), wire.RESET_PINS)
    if ack["status"] != 0:
        raise DeviceError(_refusal(ack, None))
    return link.hello(accept=lambda info: info.get("pin_src") == "default")


# ── the workspace's copy ───────────────────────────────────────────────────────
def profile_path(root: Path, device_id: str) -> Path:
    # The id comes from the device; never let it choose a path.
    if not _ID.fullmatch(device_id):
        raise DeviceError(f"not a device id: {device_id!r}")
    return Path(root) / "devices" / f"{device_id}.json"


def load_profile(root: Path, device_id: str) -> dict | None:
    path = profile_path(root, device_id)
    if not path.is_file():
        return None
    try:
        profile = json.loads(path.read_text(encoding="utf-8"))
        pins = profile["pins"]
        if not all(isinstance(pins[s], int) and not isinstance(pins[s], bool) for s in wire.PIN_SIGNALS):
            raise ValueError("pins must be GPIO numbers")
    except (ValueError, KeyError, TypeError) as e:
        raise DeviceError(f"{path} is not a valid device profile ({e})") from e
    return profile


def save_profile(root: Path, info: dict, pins: dict) -> Path:
    path = profile_path(root, info["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    profile = {
        "id": info["id"],
        "board": info.get("board"),
        "pins": {s: pins[s] for s in wire.PIN_SIGNALS},
        "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    path.write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")
    return path


def delete_profile(root: Path, device_id: str) -> None:
    profile_path(root, device_id).unlink(missing_ok=True)
