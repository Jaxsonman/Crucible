"""Crucible home and directory resolution. See docs/design.md."""

import os
import tempfile
from pathlib import Path


class CrucibleHomeError(RuntimeError):
    pass


DATA_DIR = Path(__file__).resolve().parent / "data"
HOME_DIRNAME = ".crucible"


def home() -> Path:
    env = os.environ.get("CRUCIBLE_HOME")
    if env:
        return Path(env)
    cwd = Path.cwd().resolve()
    for candidate in (cwd, *cwd.parents):
        if (candidate / HOME_DIRNAME).is_dir():
            return candidate / HOME_DIRNAME
        if (candidate / "pricing.yaml").is_file():
            return candidate
    raise CrucibleHomeError(
        f"No Crucible home found: set CRUCIBLE_HOME, run `crucible init` inside a git repo, "
        f"or run from a directory containing .crucible/ or pricing.yaml (searched from {cwd})."
    )


def home_kind() -> str:
    """'repo' for a .crucible/ home inside a host repo, else 'standalone'."""
    return "repo" if home().name == HOME_DIRNAME else "standalone"


def tasks_dir() -> Path:
    return home() / "tasks"


def configs_dir() -> Path:
    return home() / "configs"


def results_dir() -> Path:
    return home() / "results"


def pricing_file() -> Path:
    """<home>/pricing.yaml if present, else the bundled one."""
    local = home() / "pricing.yaml"
    return local if local.is_file() else DATA_DIR / "pricing.yaml"


def bundled_configs_dir() -> Path:
    return DATA_DIR / "configs"


def config_file(name: str) -> Path:
    """<home>/configs/<name>.yaml if present, else the bundled one (may not exist)."""
    local = configs_dir() / f"{name}.yaml"
    return local if local.is_file() else bundled_configs_dir() / f"{name}.yaml"


def work_dir() -> Path:
    env = os.environ.get("CRUCIBLE_WORK_DIR")
    if env:
        return Path(env)
    return Path(tempfile.gettempdir()) / "crucible-work"
