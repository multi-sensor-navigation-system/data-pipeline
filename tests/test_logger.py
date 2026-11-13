import csv

import pytest

import logger
from conftest import COLUMNS, make_session


class FakeSerial:
    """Replays canned firmware output, then simulates the user's Ctrl-C."""

    def __init__(self, lines):
        self._lines = iter(lines)
        self.dtr = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def readline(self):
        try:
            return next(self._lines)
        except StopIteration:
            raise KeyboardInterrupt from None


def firmware_stream(n_rows=20):
    df = make_session(n_rows)
    rows = [",".join(str(v) for v in r) for r in df.itertuples(index=False)]
    lines = ["# MPU-6050 OK", "# BMP280 OK", ",".join(COLUMNS)]
    lines += rows[:10]
    lines += ["# rb_drops=3", "123,456", ""]          # comment, short row, blank
    lines += rows[10:]
    return [(line + "\r\n").encode() for line in lines]


@pytest.mark.parametrize("flag,expected", [
    (0, "OK"), (1, "IMU"), (0b11000, "GPS_HDOP|GPS_NOFIX"), (0b00110, "MAG|BARO"), (0x80, "0x80"),
])
def test_decode_faults(flag, expected):
    assert logger.decode_faults(flag) == expected


def test_log_session_writes_only_well_formed_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(logger.serial, "Serial", lambda *a, **k: FakeSerial(firmware_stream()))
    monkeypatch.setattr(logger.time, "sleep", lambda s: None)
    out = tmp_path / "raw" / "raw.csv"

    rows = logger.log_session(out, port="/dev/fake")

    assert rows == 20
    with open(out, newline="") as fh:
        got = list(csv.DictReader(fh))
    assert len(got) == 20
    assert list(got[0].keys()) == COLUMNS


def test_log_session_exits_without_header(tmp_path, monkeypatch):
    monkeypatch.setattr(logger.serial, "Serial", lambda *a, **k: FakeSerial([b"# booting\r\n"] * 3))
    monkeypatch.setattr(logger.time, "sleep", lambda s: None)
    # FakeSerial raises KeyboardInterrupt once drained, which ends the wait.
    rows = logger.log_session(tmp_path / "raw.csv", port="/dev/fake", timeout=1)
    assert rows == 0
    assert not (tmp_path / "raw.csv").exists()
