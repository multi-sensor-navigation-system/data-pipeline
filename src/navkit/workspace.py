"""Where navkit keeps sessions, outputs and calibration parameters.

The workspace is a plain directory:

    <workspace>/
    ├── data/<session>/...          raw, clean, diagnostics, calibrated, metadata.json
    ├── outputs/plots|reports/
    └── calibration_params.json

It is resolved in this order: an explicit path (``--workspace``), the
``NAVKIT_HOME`` environment variable, then ``~/navkit``. Downloaded firmware
images live in the per-user cache directory, not in the workspace.
"""

import os
from dataclasses import dataclass
from pathlib import Path

import platformdirs

ENV_VAR = "NAVKIT_HOME"


@dataclass(frozen=True)
class Workspace:
    root: Path

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def outputs_dir(self) -> Path:
        return self.root / "outputs"

    @property
    def params_file(self) -> Path:
        return self.root / "calibration_params.json"

    def sessions(self) -> list[Path]:
        """Session directories, most recent first."""
        if not self.data_dir.is_dir():
            return []
        return sorted((d for d in self.data_dir.iterdir() if d.is_dir()), reverse=True)

    def session(self, name_or_path: str) -> Path:
        """Resolve a session given its name or a path to its directory."""
        p = Path(name_or_path).expanduser()
        if p.is_dir():
            return p.resolve()
        candidate = self.data_dir / name_or_path
        if candidate.is_dir():
            return candidate
        raise FileNotFoundError(f"No session '{name_or_path}' in {self.data_dir}")


def resolve(explicit: str | os.PathLike | None = None) -> Workspace:
    if explicit:
        root = Path(explicit)
    elif os.environ.get(ENV_VAR):
        root = Path(os.environ[ENV_VAR])
    else:
        root = Path.home() / "navkit"
    return Workspace(root.expanduser().resolve())


def cache_dir() -> Path:
    return Path(platformdirs.user_cache_dir("navkit"))
