import json
from argparse import Namespace

import pytest

import run_pipeline


def args(**kw):
    base = dict(calib_mode=None, calib_path=None, session_type=None, session_dir=None,
                start_from=None, user_note="", action=None)
    base.update(kw)
    return Namespace(**base)


@pytest.fixture
def no_params(tmp_path, monkeypatch):
    """Point the pipeline at an empty project root (no calibration_params.json)."""
    monkeypatch.setattr(run_pipeline, "PARAMS_FILE", tmp_path / "calibration_params.json")
    return tmp_path


def test_static_calib_never_needs_params(no_params):
    state = run_pipeline.enforce_calibration_check("static_calib", args())
    assert state["valid_for_analysis"] is True


def test_strict_mode_aborts(no_params):
    with pytest.raises(SystemExit):
        run_pipeline.enforce_calibration_check("free_label", args(calib_mode="0"))


def test_permissive_mode_flags_session_invalid(no_params):
    state = run_pipeline.enforce_calibration_check("free_label", args(calib_mode="1"))
    assert state["is_calibrated"] is False
    assert state["valid_for_analysis"] is False
    assert state["override_type"] == "allowed uncalibrated"


def test_calib_path_overrides_calib_mode(no_params):
    custom = no_params / "custom.json"
    custom.write_text("{}")
    state = run_pipeline.enforce_calibration_check(
        "free_label", args(calib_mode="0", calib_path=str(custom)))
    assert state["override_type"] == "custom path provided"
    assert run_pipeline.PARAMS_FILE == custom


def test_missing_calib_path_aborts(no_params):
    with pytest.raises(SystemExit):
        run_pipeline.enforce_calibration_check(
            "free_label", args(calib_path=str(no_params / "nope.json")))


def test_interactive_custom_path_returns_state_usable_by_metadata(no_params, monkeypatch):
    custom = no_params / "custom.json"
    custom.write_text("{}")
    answers = iter(["p", str(custom)])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    state = run_pipeline.enforce_calibration_check("free_label", args())

    assert isinstance(state, dict)
    assert state["user_overridden"] is True
    session = no_params / "session_x"
    session.mkdir()
    meta = run_pipeline.write_metadata(session, "free_label", ["trim"], None, None, calib_state=state)
    assert meta["override_type"] == "custom path provided"
    assert json.loads((session / "metadata.json").read_text())["valid_for_analysis"] is True


def test_interactive_default_aborts(no_params, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _prompt="": "")
    with pytest.raises(SystemExit):
        run_pipeline.enforce_calibration_check("free_label", args())
