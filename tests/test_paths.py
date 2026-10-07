from pathlib import Path

import pytest

from crucible import paths


def test_home_from_env(crucible_home):
    assert paths.home() == crucible_home
    assert paths.tasks_dir() == crucible_home / "tasks"
    assert paths.configs_dir() == crucible_home / "configs"
    assert paths.results_dir() == crucible_home / "results"
    assert paths.pricing_file() == crucible_home / "pricing.yaml"


def test_home_found_from_subdirectory(tmp_path, monkeypatch):
    monkeypatch.delenv("CRUCIBLE_HOME")
    root = tmp_path / "repo"
    sub = root / "a" / "b"
    sub.mkdir(parents=True)
    (root / "pricing.yaml").write_text("models: {}\n")
    monkeypatch.chdir(sub)
    assert paths.home() == root.resolve()


def test_home_missing_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("CRUCIBLE_HOME")
    empty = tmp_path / "nowhere"
    empty.mkdir()
    monkeypatch.chdir(empty)
    with pytest.raises(paths.CrucibleHomeError, match="CRUCIBLE_HOME"):
        paths.home()


def test_work_dir_env(tmp_path):
    assert paths.work_dir().parent == tmp_path
    assert paths.work_dir().name == "work"


def test_work_dir_default_outside_home(monkeypatch, crucible_home):
    monkeypatch.delenv("CRUCIBLE_WORK_DIR")
    wd = paths.work_dir()
    assert wd.name == "crucible-work"
    assert crucible_home not in wd.parents


def test_placeholder_pricing_exists():
    assert (Path(__file__).resolve().parent.parent / "pricing.yaml").is_file()
