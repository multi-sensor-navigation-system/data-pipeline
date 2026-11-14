import json

import pytest

from conftest import make_session
from navkit import pipeline, workspace
from navkit.pipeline import Options


@pytest.fixture
def ws(tmp_path):
    """An empty workspace (no calibration_params.json)."""
    return workspace.resolve(tmp_path / "ws")


@pytest.fixture
def no_tty(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)


@pytest.fixture
def tty(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)


def answers(monkeypatch, *replies):
    it = iter(replies)
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(it))


# ── calibration gate ───────────────────────────────────────────────────────────
def test_static_calib_never_needs_params(ws):
    state, params = pipeline.enforce_calibration_check("static_calib", ws, Options())
    assert state["valid_for_analysis"] is True
    assert params == ws.params_file


def test_strict_mode_aborts(ws):
    with pytest.raises(SystemExit):
        pipeline.enforce_calibration_check("free_label", ws, Options(calib_mode="0"))


def test_permissive_mode_flags_session_invalid(ws):
    state, _ = pipeline.enforce_calibration_check("free_label", ws, Options(calib_mode="1"))
    assert state["is_calibrated"] is False
    assert state["valid_for_analysis"] is False
    assert state["override_type"] == "allowed uncalibrated"


def test_calib_path_overrides_calib_mode(ws, tmp_path):
    custom = tmp_path / "custom.json"
    custom.write_text("{}")
    state, params = pipeline.enforce_calibration_check(
        "free_label", ws, Options(calib_mode="0", calib_path=str(custom)))
    assert state["override_type"] == "custom path provided"
    assert params == custom


def test_missing_calib_path_aborts(ws, tmp_path):
    with pytest.raises(SystemExit):
        pipeline.enforce_calibration_check(
            "free_label", ws, Options(calib_path=str(tmp_path / "nope.json")))


def test_without_a_terminal_the_gate_is_strict(ws, no_tty):
    with pytest.raises(SystemExit):
        pipeline.enforce_calibration_check("free_label", ws, Options())


def test_interactive_custom_path_returns_state_usable_by_metadata(ws, tmp_path, tty, monkeypatch):
    custom = tmp_path / "custom.json"
    custom.write_text("{}")
    answers(monkeypatch, "p", str(custom))

    state, params = pipeline.enforce_calibration_check("free_label", ws, Options())

    assert params == custom
    assert state["user_overridden"] is True
    session = tmp_path / "session_x"
    session.mkdir()
    meta = pipeline.write_metadata(session, "free_label", ["trim"], None, None, calib_state=state)
    assert meta["override_type"] == "custom path provided"
    assert json.loads((session / "metadata.json").read_text())["valid_for_analysis"] is True


def test_interactive_default_aborts(ws, tty, monkeypatch):
    answers(monkeypatch, "")
    with pytest.raises(SystemExit):
        pipeline.enforce_calibration_check("free_label", ws, Options())


# ── end to end ─────────────────────────────────────────────────────────────────
def make_raw_session(ws, name, n=16_000, seed=0):
    d = ws.data_dir / name
    (d / "raw").mkdir(parents=True)
    make_session(n, seed=seed).to_csv(d / "raw" / "raw.csv", index=False)
    return d


def test_static_session_runs_end_to_end_and_produces_params(ws, no_tty):
    d = make_raw_session(ws, "session_20260101_000000_static_calib")
    (d / "raw" / "firmware.json").write_text(json.dumps({"proto": 1, "board": "esp32dev", "fw": "0.2.0"}))

    meta = pipeline.process(ws, d, Options(session_type="static_calib", user_note="desk"))

    assert meta["steps_run"] == ["trim", "diagnose", "calibrate", "plot"]
    assert meta["valid_for_analysis"] is True
    assert meta["calibration_source"] == d.name
    assert meta["firmware"]["board"] == "esp32dev"
    assert meta["imu_hz"] == pytest.approx(500.0, abs=0.5)
    assert ws.params_file.exists()
    assert (d / "calibrated" / "calibrated_v1.csv").exists()
    assert (ws.outputs_dir / "plots" / d.name / "calibrated_v1.png").exists()
    assert (ws.outputs_dir / "reports" / f"{d.name}_summary.md").exists()


def test_dynamic_session_uses_existing_params_and_restarts_midway(ws, no_tty):
    static = make_raw_session(ws, "session_20260101_000000_static_calib")
    pipeline.process(ws, static, Options(session_type="static_calib"))
    dynamic = make_raw_session(ws, "session_20260102_000000_free_label", seed=5)

    pipeline.process(ws, dynamic, Options(session_type="free_label"))
    meta = pipeline.process(ws, dynamic, Options(start_from="calibrate"))

    assert meta["session_type"] == "free_label"          # remembered from metadata.json
    assert meta["steps_run"] == ["calibrate", "plot"]
    assert meta["calibration_source"] == static.name
    assert (dynamic / "calibrated" / "calibrated_v2.csv").exists()


def test_uncalibrated_run_is_flagged(ws, no_tty):
    d = make_raw_session(ws, "session_20260103_000000_free_label")
    meta = pipeline.process(ws, d, Options(session_type="free_label", calib_mode="1"))
    assert meta["is_calibrated"] is False
    assert meta["valid_for_analysis"] is False
    assert "calibrate" not in meta["steps_run"]


def test_new_session_never_overwrites(ws, monkeypatch):
    monkeypatch.setattr(pipeline, "make_session_name", lambda t: "session_fixed")
    pipeline.new_session_dir(ws, "free_label")
    with pytest.raises(FileExistsError):
        pipeline.new_session_dir(ws, "free_label")


def test_list_sessions(ws, no_tty):
    d = make_raw_session(ws, "session_20260101_000000_static_calib")
    make_raw_session(ws, "session_20260102_000000_free_label")
    pipeline.process(ws, d, Options(session_type="static_calib"))

    rows = pipeline.list_sessions(ws)

    assert [r["name"] for r in rows] == ["session_20260102_000000_free_label", d.name]
    assert rows[0]["session_type"] == "?"
    assert rows[1]["duration_s"] == pytest.approx(32.0, abs=0.1)
