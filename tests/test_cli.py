import json
from types import SimpleNamespace

import pytest

from conftest import make_session
from navkit import cli, firmware, flash


@pytest.fixture
def ws_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    return tmp_path / "ws"


def nav(ws_dir, *argv):
    return cli.main(["--workspace", str(ws_dir), *argv])


def test_no_command_prints_help(capsys):
    assert cli.main([]) == 0
    assert "flash" in capsys.readouterr().out


def test_where_reports_the_workspace(ws_dir, capsys):
    assert nav(ws_dir, "where") == 0
    assert str(ws_dir.resolve()) in capsys.readouterr().out


def test_workspace_from_environment(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("NAVKIT_HOME", str(tmp_path / "from-env"))
    cli.main(["where"])
    assert "from-env" in capsys.readouterr().out


def test_process_then_sessions(ws_dir, capsys):
    d = ws_dir / "data" / "session_20260101_000000_static_calib"
    (d / "raw").mkdir(parents=True)
    make_session(16_000).to_csv(d / "raw" / "raw.csv", index=False)
    (d / "metadata.json").write_text(json.dumps({"session_type": "static_calib"}))

    assert nav(ws_dir, "process", d.name) == 0
    capsys.readouterr()
    assert nav(ws_dir, "sessions") == 0

    out = capsys.readouterr().out
    assert d.name in out and "static_calib" in out and "0.5 min" in out
    assert (ws_dir / "calibration_params.json").exists()


def test_process_unknown_session_fails_cleanly(ws_dir, capsys):
    with pytest.raises(SystemExit) as exc:
        nav(ws_dir, "process", "nope")
    assert exc.value.code == 1
    assert "No session 'nope'" in capsys.readouterr().out


def test_process_without_params_is_strict_by_default(ws_dir):
    d = ws_dir / "data" / "session_x"
    (d / "raw").mkdir(parents=True)
    make_session(1_000).to_csv(d / "raw" / "raw.csv", index=False)
    with pytest.raises(SystemExit):
        nav(ws_dir, "process", "session_x")
    assert not (d / "clean").exists()


def test_ports_with_no_board(ws_dir, monkeypatch, capsys):
    monkeypatch.setattr("navkit.ports.candidates", lambda: [])
    assert nav(ws_dir, "ports") == 1


# ── flash ──────────────────────────────────────────────────────────────────────
@pytest.fixture
def flashed(monkeypatch, tmp_path):
    """Stub the board and GitHub; capture what would be flashed."""
    calls = []
    image_file = tmp_path / "msns-firmware-esp32dev-v0.3.0.bin"
    image_file.write_bytes(b"img")
    image = firmware.Image(version="v0.3.0", board="esp32dev", chip="esp32", filename=image_file.name,
                           url="x", sha256="ab" * 32, proto=1, name="ESP32 DevKit")
    monkeypatch.setattr("navkit.ports.find_esp32_port", lambda: "/dev/fake")
    monkeypatch.setattr(flash, "detect_chip", lambda port: "esp32")
    monkeypatch.setattr(flash, "flash", lambda path, port, **kw: calls.append((path, port, kw)))
    monkeypatch.setattr(firmware, "boards", lambda version="latest": {
        "esp32dev": {"chip": "esp32"}, "esp32-s3-devkitc-1": {"chip": "esp32s3"}})
    monkeypatch.setattr(firmware, "resolve", lambda board, version="latest": image)
    monkeypatch.setattr(firmware, "fetch", lambda img: image_file)
    return SimpleNamespace(calls=calls, image_file=image_file)


def test_flash_auto_detects_board_and_flashes_release(ws_dir, flashed, capsys):
    assert nav(ws_dir, "flash") == 0

    (path, port, kw), = flashed.calls
    assert path == flashed.image_file and port == "/dev/fake"
    assert kw == {"chip": "esp32", "offset": "0x0", "baud": 921600}
    assert "Detected esp32 → board 'esp32dev'" in capsys.readouterr().out


def test_flash_refuses_to_guess_between_boards(ws_dir, flashed, monkeypatch, capsys):
    monkeypatch.setattr(flash, "detect_chip", lambda port: "esp32c3")
    assert nav(ws_dir, "flash") == 1
    assert flashed.calls == []
    assert "Pass --board" in capsys.readouterr().out


def test_flash_local_file(ws_dir, flashed, tmp_path):
    local = tmp_path / "local.bin"
    local.write_bytes(b"x")
    assert nav(ws_dir, "flash", "--file", str(local), "--chip", "esp32s3", "--port", "/dev/x") == 0
    (path, port, kw), = flashed.calls
    assert (path, port, kw["chip"]) == (local, "/dev/x", "esp32s3")


def test_flash_reports_firmware_errors(ws_dir, flashed, monkeypatch, capsys):
    def boom(board, version="latest"):
        raise firmware.FirmwareError("Release v0.3.0 has no image for board 'esp32dev'.")
    monkeypatch.setattr(firmware, "resolve", boom)
    assert nav(ws_dir, "flash", "--board", "esp32dev") == 1
    assert flashed.calls == []
    assert "no image" in capsys.readouterr().out


def test_flash_without_a_board_connected(ws_dir, monkeypatch):
    monkeypatch.setattr("navkit.ports.find_esp32_port", lambda: None)
    with pytest.raises(SystemExit):
        nav(ws_dir, "flash")


def test_esptool_arguments():
    args = flash.esptool_args("fw.bin", "/dev/x", chip="esp32s3", offset="0x0", baud=460800)
    assert args == ["--chip", "esp32s3", "--port", "/dev/x", "--baud", "460800", "write-flash", "0x0", "fw.bin"]


@pytest.mark.parametrize("name,expected", [("ESP32", "esp32"), ("ESP32-S3", "esp32s3"), ("ESP32-C3", "esp32c3")])
def test_chip_id(name, expected):
    assert flash.chip_id(name) == expected


# ── doctor ─────────────────────────────────────────────────────────────────────
def test_doctor_passes_on_a_working_machine(ws_dir, monkeypatch, capsys):
    port = SimpleNamespace(device="/dev/null", description="CP2102 USB to UART")
    monkeypatch.setattr("navkit.ports.candidates", lambda: [port])
    assert nav(ws_dir, "doctor") == 0
    out = capsys.readouterr().out
    assert "board port found" in out and "Ready." in out


def test_doctor_warns_but_passes_without_a_board(ws_dir, monkeypatch, capsys):
    monkeypatch.setattr("navkit.ports.candidates", lambda: [])
    assert nav(ws_dir, "doctor") == 0
    assert "no ESP32-like serial port found" in capsys.readouterr().out
