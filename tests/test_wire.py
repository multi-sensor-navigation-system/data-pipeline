from pathlib import Path

import pytest

import wire
from conftest import COLUMNS

GOLDEN = (Path(__file__).parent / "fixtures" / "wire_golden_v1.bin").read_bytes()


def decode_all(data: bytes, chunk: int | None = None):
    dec = wire.StreamDecoder()
    events = []
    step = chunk or len(data)
    for i in range(0, len(data), step):
        events += dec.feed(data[i:i + step])
    return dec, events


def test_crc_check_value():
    assert wire.crc16(b"123456789") == 0x29B1


def test_columns_match_the_pipeline():
    assert wire.CSV_COLUMNS == COLUMNS


def test_golden_stream_from_the_firmware_encoder():
    """The fixture is the output of firmware/tools/host/wire_dump.cpp."""
    dec, events = decode_all(GOLDEN)

    assert dec.mode == "binary"
    assert [e.kind for e in events] == ["log", "hello"] + ["row"] * 8 + ["status"]
    assert events[0].payload == "MPU-6050 OK"

    hello = events[1].payload
    assert hello["proto"] == 1
    assert hello["board"] == "esp32dev"
    assert hello["fw"] == "0.2.0"
    assert hello["git"] == "abc1234"
    assert hello["sensors"] == "imu,mag,baro,gps"

    rows = [e.payload for e in events if e.kind == "row"]
    assert list(rows[0]) == COLUMNS
    r3 = rows[3]
    assert r3["t_imu_us"] == "5006000"
    assert r3["t_read_start_us"] == "5006000"
    assert r3["t_proc_end_us"] == "5006603"
    assert r3["t_gps_us"] == "4000000"
    assert float(r3["ax_g"]) == 0.0
    assert float(r3["az_g"]) == pytest.approx(1.003, abs=1e-6)
    assert float(r3["gz_dps"]) == 0.375
    assert float(r3["pressure_hPa"]) == 1013.25
    assert float(r3["lat_deg"]) == 18.5204303
    assert float(r3["lon_deg"]) == 73.8567437
    assert r3["satellites"] == "8"
    assert r3["fault_flag"] == "6"
    assert r3["jitter_us"] == "30"

    assert events[-1].payload == {"rb_drops": 3, "samples_sent": 8}
    assert dec.rb_drops == 3
    assert dec.lost_frames == 0 and dec.crc_errors == 0


@pytest.mark.parametrize("chunk", [1, 7, 64])
def test_decoding_is_independent_of_how_bytes_arrive(chunk):
    _, whole = decode_all(GOLDEN)
    _, pieces = decode_all(GOLDEN, chunk)
    assert [(e.kind, e.payload) for e in pieces] == [(e.kind, e.payload) for e in whole]


def test_corrupted_frame_costs_one_frame_and_is_counted():
    frames = GOLDEN.split(b"\x00")[:-1]
    bad = bytearray(frames[5])            # the 4th sample (seq 3)
    bad[10] ^= 0x55
    frames[5] = bytes(bad)
    dec, events = decode_all(b"\x00".join(frames) + b"\x00")

    assert sum(e.kind == "row" for e in events) == 7
    assert dec.crc_errors == 1
    assert dec.lost_frames == 1            # seen as a gap in seq


def test_resyncs_when_attaching_mid_frame():
    dec, events = decode_all(GOLDEN[40:])  # starts inside the hello frame
    assert dec.mode == "binary"
    assert sum(e.kind == "row" for e in events) == 8
    assert dec.crc_errors == 0             # the partial frame arrived before sync


def test_boot_noise_before_first_frame_is_ignored():
    # The firmware writes one 0x00 before its first frame, which terminates
    # whatever the boot ROM printed at the wrong baud rate.
    noise = bytes([0xFF, 0x00, 0x13, 0x37, 0x00, 0x00, 0xA5] * 9)
    dec, events = decode_all(noise + b"\x00" + GOLDEN)
    assert [e.kind for e in events][:2] == ["log", "hello"]
    assert dec.crc_errors == 0


def test_csv_firmware_is_detected():
    header = ",".join(COLUMNS)
    row = ",".join(["1"] * len(COLUMNS))
    text = f"# MPU-6050 OK\r\n{header}\r\n{row}\r\n# rb_drops=4\r\n1,2\r\n{header}\r\n{row}\r\n"
    dec, events = decode_all(text.encode(), chunk=5)

    assert dec.mode == "csv"
    assert [e.kind for e in events] == ["log", "hello", "row", "log", "bad", "row"]
    assert events[1].payload == {"proto": 0}
    assert dec.header == COLUMNS
    assert dec.rb_drops == 4


def test_unknown_frame_type_is_skipped():
    import struct
    raw = bytes([0x7E]) + b"future"
    raw += struct.pack("<H", wire.crc16(raw))
    # COBS-encode by hand: no zero bytes in this payload, so one block.
    frame = bytes([len(raw) + 1]) + raw + b"\x00"
    dec, events = decode_all(frame + GOLDEN)
    assert dec.mode == "binary"
    assert events[0].kind == "log"
