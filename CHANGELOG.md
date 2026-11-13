# Changelog

All notable changes to this repository are recorded here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- MIT `LICENSE` file (the README badge already pointed at one).
- Test suite (`pytest`) covering the logger, trimmer, diagnostics, calibrator
  and the calibration gate, driven by synthetic sessions.
- CI: ruff lint plus tests on Linux, macOS and Windows.

### Fixed
- Choosing "custom path" in the interactive calibration prompt crashed the
  run when metadata was written (the gate returned `True` instead of the
  state dict).
- `--calib-path` now overrides `--calib-mode`, as the README documents.
- Action 4 (compute calibration params) crashed on a missing argument.

### Changed
- `logger.log_session()` replaces `main_with_path()`, which worked by
  rewriting `sys.argv`.
- Module docstrings now use the current file names.
