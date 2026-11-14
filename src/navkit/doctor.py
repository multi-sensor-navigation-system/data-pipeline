"""`nav doctor` — check that this machine can flash and log."""

import importlib
import importlib.util
import os
import platform
import sys

from . import __version__, ports, workspace

OK, WARN, FAIL = "ok", "warn", "fail"
_MARK = {OK: "✓", WARN: "!", FAIL: "✗"}


def _check_python():
    v = sys.version_info
    status = OK if v >= (3, 10) else FAIL
    return status, f"Python {v.major}.{v.minor}.{v.micro}", "navkit needs Python 3.10 or newer"


def _check_module(name: str, purpose: str, required: bool):
    if importlib.util.find_spec(name) is not None:
        return OK, f"{name} available ({purpose})", ""
    return (FAIL if required else WARN), f"{name} missing ({purpose})", ""


def _check_pipeline_extras():
    missing = [m for m in ("pandas", "numpy", "scipy", "matplotlib")
               if importlib.util.find_spec(m) is None]
    if not missing:
        return OK, "pipeline extras installed (trim, diagnose, calibrate, plot)", ""
    return WARN, f"pipeline extras missing: {', '.join(missing)}", 'pip install "navkit[pipeline]"'


def _check_ports():
    found = ports.candidates()
    if found:
        listing = ", ".join(f"{p.device} ({p.description})" for p in found)
        return OK, f"board port found: {listing}", ""
    system = platform.system()
    hint = "Plug the board in with a data-capable USB cable."
    if system == "Darwin":
        hint += " If it still does not appear, install the CP210x or CH340 driver for your board's USB chip."
    elif system == "Windows":
        hint += " Check Device Manager for a COM port; install the CP210x or CH340 driver if it is missing."
    return WARN, "no ESP32-like serial port found", hint


def _check_port_access():
    if platform.system() != "Linux":
        return None
    found = ports.candidates()
    if not found:
        return None
    blocked = [p.device for p in found if not os.access(p.device, os.R_OK | os.W_OK)]
    if not blocked:
        return OK, "serial port is readable and writable", ""
    return (FAIL, f"no permission to open {', '.join(blocked)}",
            "sudo usermod -aG dialout $USER   (then log out and back in)")


def _check_workspace(ws):
    probe = ws.root
    while not probe.exists():
        probe = probe.parent
    if os.access(probe, os.W_OK):
        return OK, f"workspace: {ws.root}", ""
    return FAIL, f"workspace not writable: {ws.root}", f"set {workspace.ENV_VAR} or pass --workspace"


def _check_github():
    from . import firmware
    try:
        rel = firmware.releases()
    except firmware.FirmwareError as e:
        return WARN, "cannot list firmware releases", str(e)
    if not rel:
        return WARN, "no firmware releases published yet", "flash a local build with: nav flash --file <image.bin>"
    return OK, f"latest firmware release: {rel[0]['tag_name']}", ""


def run(ws, online: bool = False) -> int:
    """Print the report. Returns 0 when nothing failed, 1 otherwise."""
    print(f"navkit {__version__} on {platform.system()} {platform.machine()}\n")
    checks = [
        _check_python(),
        _check_module("serial", "pyserial, serial logging", required=True),
        _check_module("esptool", "flashing", required=True),
        _check_pipeline_extras(),
        _check_ports(),
        _check_port_access(),
        _check_workspace(ws),
    ]
    if online:
        checks.append(_check_github())

    failed = False
    for check in checks:
        if check is None:
            continue
        status, message, hint = check
        failed |= status == FAIL
        print(f"  {_MARK[status]} {message}")
        if hint and status != OK:
            print(f"      → {hint}")
    print()
    print("Problems found." if failed else "Ready.")
    return 1 if failed else 0
