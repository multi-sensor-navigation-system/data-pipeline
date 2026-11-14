import csv
import json
from pathlib import Path

import pytest

import logger
from conftest import COLUMNS, make_session


GOLDEN = (Path(__file__).parent / "fixtures" / "wire_golden_v1.bin").read_bytes()


class FakeSerial:
    """Replays canned firmware bytes, then simulates the user's Ctrl-C."""

    def __init__(self, data: bytes, chunk: int = 64):
        self._data = data
        self._chunk = chunk
        self.dtr = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @property
    def in_waiting(self):
        return min(self._chunk, len(self._data))

    def read(self, n=1):
        if not self._data:
            raise KeyboardInterrupt
        out, self._data = self._data[:n], self._data[n:]
        return out


def csv_stream(n_rows=20) -> bytes:
    df = make_session(n_rows)
    rows = [",".join(str(v) for v in r) for r in df.itertuples(index=False)]
    lines = ["# MPU-6050 OK", "# BMP280 OK", ",".join(COLUMNS)]
    lines += rows[:10]
    lines += ["# rb_drops=3", "123,456", ""]          # comment, short row, blank
    lines += rows[10:]
    return "".join(line + "\r\n" for line in lines).encode()


@pytest.fixture
def fake_port(monkeypatch):
    def install(data: bytes):
        monkeypatch.setattr(logger.serial, "Serial", lambda *a, **k: FakeSerial(data))
        monkeypatch.setattr(logger.time, "sleep", lambda s: None)
    return install


@pytest.mark.parametrize("flag,expected", [
    (0, "OK"), (1, "IMU"), (0b11000, "GPS_HDOP|GPS_NOFIX"), (0b00110, "MAG|BARO"), (0x80, "0x80"),
])
def test_decode_faults(flag, expected):
    assert logger.decode_faults(flag) == expected


def test_csv_firmware_writes_only_well_formed_rows(tmp_path, fake_port):
    fake_port(csv_stream())
    out = tmp_path / "raw" / "raw.csv"

    rows = logger.log_session(out, port="/dev/fake")

    assert rows == 20
    with open(out, newline="") as fh:
        got = list(csv.DictReader(fh))
    assert len(got) == 20
    assert list(got[0].keys()) == COLUMNS
    assert not (out.parent / "firmware.json").exists()


def test_binary_firmware_is_logged_to_the_same_csv_layout(tmp_path, fake_port, capsys):
    fake_port(GOLDEN)
    out = tmp_path / "raw" / "raw.csv"

    rows = logger.log_session(out, port="/dev/fake")

    assert rows == 8
    with open(out, newline="") as fh:
        got = list(csv.DictReader(fh))
    assert list(got[0].keys()) == COLUMNS
    assert got[3]["t_imu_us"] == "5006000"
    assert got[3]["fault_flag"] == "6"
    firmware = json.loads((out.parent / "firmware.json").read_text())
    assert firmware["board"] == "esp32dev" and firmware["fw"] == "0.2.0"
    printed = capsys.readouterr().out
    assert "[FW] MPU-6050 OK" in printed
    assert "rb_drops=3" in printed


def test_binary_rows_before_the_hello_are_not_written(tmp_path, fake_port):
    frames = GOLDEN.split(b"\x00")[:-1]
    hello, samples = frames[1], frames[2:10]
    stream = b"\x00".join(samples[:3] + [hello] + samples[3:]) + b"\x00"
    fake_port(stream)

    rows = logger.log_session(tmp_path / "raw.csv", port="/dev/fake")

    assert rows == 5


def test_unsupported_protocol_version_is_refused(tmp_path, fake_port):
    import struct

    import wire
    raw = bytes([wire.HELLO, 99]) + b"board=x"
    raw += struct.pack("<H", wire.crc16(raw))
    assert 0 not in raw
    fake_port(bytes([len(raw) + 1]) + raw + b"\x00")

    with pytest.raises(SystemExit):
        logger.log_session(tmp_path / "raw.csv", port="/dev/fake")
    assert not (tmp_path / "raw.csv").exists()


def test_no_firmware_output_writes_nothing(tmp_path, fake_port):
    fake_port(b"# booting\r\n" * 3)
    # FakeSerial raises KeyboardInterrupt once drained, which ends the wait.
    rows = logger.log_session(tmp_path / "raw.csv", port="/dev/fake", timeout=1)
    assert rows == 0
    assert not (tmp_path / "raw.csv").exists()
