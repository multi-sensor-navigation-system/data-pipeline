# Data Pipeline | Multi-Sensor Navigation System

[![Python](https://img.shields.io/badge/Python-3.10+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Pandas](https://img.shields.io/badge/Pandas-2.0+-150458?logo=pandas&logoColor=white)](https://pandas.pydata.org/)
[![NumPy](https://img.shields.io/badge/NumPy-1.24+-013243?logo=numpy&logoColor=white)](https://numpy.org/)
[![SciPy](https://img.shields.io/badge/SciPy-1.10+-8CAAE6?logo=scipy&logoColor=white)](https://scipy.org/)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)
[![Repo](https://img.shields.io/badge/Repo-2%20of%204-blue)](#multi-repo-structure)

> **Repo 2 of 4** · Deterministic serial logging, automated session trimming, and ultra-robust sensor calibration pipeline.
> 
> Transforms raw UART streams from [Repo 1](https://github.com/multi-sensor-navigation-system/firmware) into cleaned, calibrated and analysis-ready datasets.
>
> ⚠️ *This repository was named `navigation-system-pipeline` before moving under the `multi-sensor-navigation-system` organization from [@ShivtejG236](https://github.com/ShivtejG236).*

---

## What this is

The host-side processing engine for the navigation system. It handles high-speed UART data acquisition, performs signal/noise segmentation, generates timing diagnostics, and applies precise sensor calibration with built-in sanity checks.

The pipeline follows a deterministic multi-stage flow:

| Stage | Module | Responsibility | Output |
|---|---|---|---|
| **1. Log** | `logger.py` + `wire.py` | Binary frames (or CSV debug stream) → CSV | `raw/raw.csv`, `raw/firmware.json` |
| **2. Trim** | `trimmer.py` | Signal/Noise segmentation | `clean/signal_v1.csv` |
| **3. Diagnose** | `diagnostics.py` | Timing, jitter & alignment | `diagnostics/timing_v1.json` |
| **4. Calibrate** | `calibrator.py` | Bias, scale & hard-iron | `calibrated/calibrated_v1.csv` |
| **5. Plot** | `plotter.py` | Visual validation | `outputs/plots/*.png` |

---

## System Architecture

```mermaid
graph LR
    %% Input from Repo 1
    FW["<b>Repo 1: Firmware</b><br/>ESP32 CSV Stream<br/>921600 baud"] -->|"UART"| LOG

    subgraph Pipeline["<b>Repo 2: Data Pipeline</b>"]
        LOG["logger.py<br/>Serial Listener"] --> RAW[/"raw.csv"/]
        RAW --> TRIM["trimmer.py<br/>Signal Trimmer"]
        
        TRIM --> CLN[/"signal_v1.csv"/]

        CAL --> CALB[/"calibrated_v1.csv"/]
        CLN --> META[/"metadata.json<br/>Session Manifest"/]
        CAL --> META
        CLN --> DIA["diagnostics.py<br/>Timing Analysis"]
        
        DIA --> DIAG_OUT[/"timing_v1.json"/]
        
        CLN --> CAL["calibrator.py<br/>Sensor Calibration"]
        

    end

    subgraph Data["<b>(data/session_id/)</b>"]
        RAW --> RAWDIR["raw/"]
        ROOT["session root"]
        CALB --> CALDIR["calibrated/"]
        CLN --> CLNDIR["clean/"]
        DIAG_OUT --> DIADIR["diagnostics/"]
    end

    META --> ROOT

    %% Styling
    classDef step fill:#f9f,stroke:#333,stroke-width:2px;
    classDef file fill:#fff,stroke:#333,stroke-dasharray: 5 5;
    classDef dir fill:#ffd580,stroke:#333,stroke-width:1px;
    class LOG,TRIM,DIA,CAL step;
    class RAW,CLN,DIAG_OUT,CALB,META file;
    class RAWDIR,CLNDIR,CALDIR,DIADIR,ROOT dir;
```
```mermaid
graph LR
    subgraph Pipeline["<b>Repo 2</b>"]
        RAW[/"raw.csv"/]
        CALB[/"calibrated_v1.csv"/]
        CLN[/"signal_v1.csv"/]
        TIME[/"timing_v1.json"/]
    end

    subgraph Plot["<b>(outputs/)</b>"]
        REPDIR["reports/"]
        PLTDIR["plots/session_id/"]
    end

    RAW --> PLT["plotter.py\nSignal Plotter"]
    CLN --> PLT
    CALB --> PLT

    PLT --> |"raw.png"| PLTDIR
    PLT --> |"clean_v1.png"| PLTDIR
    PLT --> |"calibrated_v1.png"| PLTDIR

    CALB --> REP[/"summary_id.md"/]
    TIME --> REP
    REP --> REPDIR

    classDef step fill:#f9f,stroke:#333,stroke-width:2px;
    classDef file fill:#fff,stroke:#333,stroke-dasharray: 5 5;
    classDef dir fill:#ffd580,stroke:#333,stroke-width:1px;
    class RAW,CLN,CALB,TIME,REP file;
    class REPDIR,PLTDIR dir;
    class PLT step;
```

---

## Calibration

This pipeline is designed with strict validation checks to prevent invalid data from propagating downstream.

### Checks
If you attempt to run a dynamic session (`free_label`, `mag_rotation`) without a valid `calibration_params.json` in the root, the pipeline will halt and force a decision:
* **Strict Mode:** Abort and generate parameters from a `static_calib` session first.
* **Permissive Mode:** Continue uncalibrated (session flagged as `valid_for_analysis: false`).
* **Override:** Provide a custom path to a specific parameters file.

### Bias Sanity
During parameter computation, the system validates the results against physical limits:
* **Gyro Bias:** If `|bias| > 50 °/s`.
* **Accel Norm:** If `|a|` deviates from `1.0g` by more than 10%.
* **Mag Field:** If field radius is outside the Earth's natural range (`20–70 μT`).

*Calibration parameters are rejected and the session is marked invalid if they fail sanity checks. No calibrated output is generated.*

### Reports
Each calibration run produces a `calibration_v1.json` detailing out the particulars. Few of these labels are:
```json
{
  "version": 1,
  "input_file": "signal_v1.csv",
  "params_file": "calibration_params.json",
  "params_source_session": "session_20260502_224602_static_calib",
  "params_computed_at": "2026-05-02T22:46:20",
  "params_user_note": "flat on desk",
  "calibration_valid": false
}
```
Calibration stage executes for all runs, but may result in (for dynamic runs):
- valid calibrated output
- skipped (no parameters available)
- rejected (failed sanity checks)

Additionally, parameter generation requires valid static calibration data.

---

## Metadata Tracking

Every session produces a `metadata.json` that acts as a deterministic receipt for the run. A few of the labels are listed here:
```json
{
  "session_name": "session_20260502_224602_static_calib",
  "session_type": "static_calib",
  "is_calibrated": true,
  "calibration_source": "session_20260502_224602_static_calib",
  "calibration_valid": false,
  "user_overridden": false,
  "override_type": "none",
  "valid_for_analysis": false,
  "reason": "Internal calibration check failed (insufficient signal or failed sanity limits).",
  "created_at": "2026-05-02T22:46:21",
  "user_note": "flat on desk",
  "duration_s": 10.68,
  "total_rows": 4431,
  "imu_hz": 414.95
}
```

### Data Validity

Each session is categorized as:

- **Calibrated & Valid** → ready for analysis
- **Uncalibrated** → usable for debugging only
- **Invalid** → failed sanity checks or corrupted data

*Only "Calibrated & Valid" data should be used for downstream modeling or analysis.*

---

## Install

`navkit` is the Python package in this repo; it installs the `nav` command.

```bash
# Flash and log only (small: pyserial + esptool)
pip install "navkit @ git+https://github.com/multi-sensor-navigation-system/data-pipeline"

# Everything, including trim / diagnose / calibrate / plot
pip install "navkit[pipeline] @ git+https://github.com/multi-sensor-navigation-system/data-pipeline"
```

From a checkout, for development:

```bash
python -m venv .nav && source .nav/bin/activate
pip install -e ".[dev]"
pytest
```

Requires Python 3.10+.

---

## Quick start

```bash
nav doctor                      # is this machine ready? is a board connected?
nav flash                       # detect the board, download the latest firmware, verify, flash
nav record --type static_calib --note "flat on desk"     # log (Ctrl-C to stop), then process
nav record --type free_label --note "bike ride"
nav sessions
```

`nav wizard` walks through the same steps as an interactive menu.

---

## Commands

| Command | What it does |
|---|---|
| `nav doctor [--online]` | Checks Python, serial access, drivers, the workspace and (with `--online`) firmware releases |
| `nav ports` | Lists serial ports that look like a board |
| `nav flash [--board ID] [--version TAG] [--port P]` | Downloads a firmware release, verifies its SHA-256 and flashes it. `--board auto` (default) detects the chip. `--file image.bin` flashes a local build |
| `nav firmware list` / `nav firmware fetch` | Lists releases and their boards / downloads an image without flashing |
| `nav record --type T [--note N]` | Logs a new session from the board, then runs the whole pipeline on it |
| `nav process SESSION [--from STEP]` | Runs the pipeline on an existing session, from `trim`, `diagnose`, `calibrate` or `plot` |
| `nav calibrate SESSION` | Computes calibration parameters from a static session |
| `nav sessions` | Lists sessions with type, duration and validity |
| `nav log [-o FILE]` | Logs the serial stream to one CSV file and does nothing else |
| `nav wizard` | Interactive menu |
| `nav where` | Shows where sessions, outputs and cached firmware live |

Session types: `static_calib`, `accel_6pos`, `mag_rotation`, `free_label`.

When a dynamic session has no calibration parameters, `record` and `process` ask what to do. Decide up front with `--strict` (abort), `--allow-uncalibrated` (continue; the session is flagged `valid_for_analysis: false`) or `--calib-path FILE`. A custom path wins over the other two. Without a terminal attached the default is strict.

`python run_pipeline.py` still works from a checkout and uses the checkout itself as the workspace.

---

## Workspace

Sessions, plots and calibration parameters live in one directory, the workspace:

```
~/navkit/
├── data/<session>/
│   ├── raw/            raw.csv, firmware.json
│   ├── clean/          signal_v*.csv, noise_v*.csv, cleaning_v*.json
│   ├── diagnostics/    timing_v*.json, jitter_v*.png, alignment_v*.png
│   ├── calibrated/     calibrated_v*.csv, calibration_v*.json
│   └── metadata.json
├── outputs/
│   ├── plots/<session>/
│   └── reports/
└── calibration_params.json
```

It defaults to `~/navkit`. Change it with `nav --workspace DIR …` or the `NAVKIT_HOME` environment variable. Downloaded firmware is cached separately in the per-user cache directory (`nav where` prints both).

---

## Repo-02 Structure
```
data-pipeline/
├── src/navkit/
│   ├── cli.py               # The `nav` command
│   ├── pipeline.py          # Orchestration, calibration gate, metadata, wizard
│   ├── logger.py            # High-speed UART listener
│   ├── wire.py              # Wire protocol v1 decoder (binary + CSV detection)
│   ├── trimmer.py           # Signal/Noise segmenter
│   ├── diagnostics.py       # Timing & jitter analysis
│   ├── calibrator.py        # Bias, scale & hard-iron calibration
│   ├── plotter.py           # Multi-stage signal visualisation
│   ├── firmware.py          # Firmware releases: resolve, download, verify, cache
│   ├── flash.py             # esptool wrapper, chip detection
│   ├── doctor.py            # Environment checks
│   ├── ports.py             # Serial port discovery
│   └── workspace.py         # Where sessions and outputs live
├── tests/                   # pytest suite on synthetic sessions and a golden wire stream
├── run_pipeline.py          # Backwards-compatible entry point (wizard)
├── pyproject.toml           # Package metadata and tooling
└── README.md
```

---

## Highlights

### Calibration Lifecycle
- **Strict/Permissive Modes** – The pipeline prevents uncalibrated data from being used for analysis unless explicitly overridden.
- **Dry-Run Protection** – The system detects sessions with no meaningful sensor activity (e.g. all zeros or constant signals) and flags them as `calibration_valid: false`.
- **Traceability Receipts** – Every calibrated dataset includes a JSON receipt documenting the source session, computation timestamp and user notes for the parameters applied.

### State-aware Metadata
- **Dynamic Sensor Detection** – Automatically lists only the sensors that were actually reporting data in `metadata.json`.
- **Session Context** – Includes user-provided notes ("desk setup", "bike test") and state flags (`is_calibrated`, `calibration_valid`, `user_overridden`).

### Automated Processing
- **Trimming** – Signal/noise segmentation with reboot boundary detection.
- **Timing Integrity** – Jitter analysis and inter-sensor alignment diagnostics are generated for every run.

### Additional Guardrails
- **Session Safety** – Existing sessions are never overwritten. Logging always creates a new session directory to preserve data integrity.
- **Stage Independence** – The pipeline allows partially successful runs. Each step produces outputs independently where possible.

---

## Project Ecosystem

```mermaid
graph LR
    FW["🔧 firmware\nESP32 sensor acquisition\nCSV @ 921600 baud"]
    PL["📡 data-pipeline\nHost logger · serial → CSV\ncleaning & calibration"]
    AN["📊 analysis\nEKF · Allan variance\nvisualisation · reports"]
    EH["🧭 edgehard\nDead reckoning\nGPS dropout modelling"]

    FW -->|"UART CSV stream"| PL
    PL -->|"clean .csv files"| AN
    PL -->|"clean .csv files"| EH
    AN -. "future: model feedback" .-> EH
```

| Repo | Role | Status |
|---|---|---|
| [`firmware`](https://github.com/multi-sensor-navigation-system/firmware) | ESP32 firmware — sensor acquisition & CSV streaming | ✅ Active |
| **`data-pipeline`** ← *(this repo, formerly navigation-system-pipeline)* | Host-side logger & calibration — reads serial, writes timestamped CSV | ✅ Active |
| [`analysis`](#) | Offline processing — EKF, Allan variance, map visualisation, reports | 🚧 In progress |
| [`edgehard`](#) | Dead reckoning — GPS dropout modelling, edge inference | 🔜 Planned |
