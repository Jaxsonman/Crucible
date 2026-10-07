import shutil
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def crucible_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    for name in ("tasks", "configs", "results"):
        (home / name).mkdir(parents=True)
    shutil.copy(REPO_ROOT / "pricing.yaml", home / "pricing.yaml")
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv("CRUCIBLE_HOME", str(home))
    monkeypatch.setenv("CRUCIBLE_WORK_DIR", str(work))
    return home
