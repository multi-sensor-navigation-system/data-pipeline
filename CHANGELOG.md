# Changelog

All notable changes to this repository are recorded here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- **`navkit` package and the `nav` command.** The pipeline is now an
  installable Python package (`pip install navkit`, `navkit[pipeline]` for
  the analysis stages) with subcommands: `flash`, `firmware`, `ports`,
  `doctor`, `record`, `process`, `calibrate`, `sessions`, `log`, `wizard`,
  `where`.
- `nav flash`: detects the chip, downloads the matching firmware release,
  verifies its SHA-256 and flashes it with esptool. Images are cached.
- `nav doctor`: checks Python, serial access, drivers and the workspace.
- A workspace directory (`~/navkit`, `--workspace`, or `NAVKIT_HOME`) holds
  sessions, outputs and calibration parameters, so the tool no longer has
  to run from a source checkout.

### Changed
- Modules moved from `pipeline/` to `src/navkit/`; `session_trimmer.py` is
  now `trimmer.py`.
- Flashing and logging no longer import pandas, scipy or matplotlib.
- Without a terminal attached, a missing calibration file aborts the run
  (strict) instead of waiting for input.
- `run_pipeline.py` is a thin wrapper around the wizard and keeps using the
  checkout as its workspace.

### Added
- `pipeline/wire.py`: decoder for the firmware's binary wire protocol v1
  (COBS framing, CRC-16), tested against a golden stream produced by the
  firmware's own encoder.
- The logger detects which firmware build is on the board (binary or CSV
  debug) and writes the same `raw.csv` for both.
- `raw/firmware.json` records the board, firmware version and git revision
  the firmware reported; `metadata.json` includes it.
- The logger reports frames lost on the wire and CRC errors.

### Changed
- The logger refuses firmware that speaks a newer wire protocol than it knows.
- A CSV header reprinted after a reboot is no longer written as a data row.

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
