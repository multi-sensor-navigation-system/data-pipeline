import json

import pandas as pd

import session_trimmer
from conftest import COLUMNS, make_session


def test_single_clean_session_is_all_signal(session_dir):
    signal = session_trimmer.run(session_dir)

    assert signal.name == "signal_v1.csv"
    assert len(pd.read_csv(signal)) == 30_000
    meta = json.loads((session_dir / "clean" / "cleaning_v1.json").read_text())
    assert meta["is_signal_valid"] is True
    assert meta["segments_detected"] == 1
    assert meta["output_noise_rows"] == 0


def test_reboot_splits_segments_and_longest_clean_one_wins(tmp_path):
    d = tmp_path / "session_reboot"
    (d / "raw").mkdir(parents=True)
    first = make_session(5_000, t0_us=50_000_000)           # 10 s, too short to be "clean"
    second = make_session(20_000, t0_us=1_000_000, seed=1)  # 40 s after a reboot
    raw = d / "raw" / "raw.csv"
    with open(raw, "w", newline="") as fh:
        first.to_csv(fh, index=False)
        fh.write(",".join(COLUMNS) + "\n")                  # header reprinted on reboot
        second.to_csv(fh, index=False, header=False)

    signal = session_trimmer.run(d)

    df = pd.read_csv(signal)
    assert len(df) == 20_000
    assert df.t_imu_us.iloc[0] == 1_000_000
    meta = json.loads((d / "clean" / "cleaning_v1.json").read_text())
    assert meta["segments_detected"] == 2
    assert meta["signal_segment_index"] == 2
    assert meta["output_noise_rows"] == 5_000


def test_framing_errors_are_removed(tmp_path):
    d = tmp_path / "session_framing"
    (d / "raw").mkdir(parents=True)
    df = make_session(20_000)
    df.loc[100, "gz_dps"] = 3.2e9          # timestamp leaked into a sensor column
    df.loc[200, "mx_uT"] = -4.1e8
    df.to_csv(d / "raw" / "raw.csv", index=False)

    session_trimmer.run(d)

    meta = json.loads((d / "clean" / "cleaning_v1.json").read_text())
    assert meta["rows_removed"] == 2
    assert meta["output_signal_rows"] == 19_998


def test_rerun_creates_new_version(session_dir):
    session_trimmer.run(session_dir)
    assert session_trimmer.run(session_dir).name == "signal_v2.csv"
