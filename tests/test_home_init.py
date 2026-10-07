import filecmp
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest
import yaml

from crucible import configs, paths
from crucible.cli import main

REPO_ROOT = Path(__file__).resolve().parent.parent
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
}


def make_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, **GIT_ENV}
    subprocess.run(["git", "init", "-q"], cwd=path, check=True, env=env)
    (path / "f.txt").write_text("x\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True, env=env)
    return path


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.delenv("CRUCIBLE_HOME")
    r = make_repo(tmp_path / "proj").resolve()
    monkeypatch.chdir(r)
    return r


def snapshot(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


# --- bundled data ---

def test_bundled_pricing_identical_to_root():
    assert filecmp.cmp(
        REPO_ROOT / "pricing.yaml", REPO_ROOT / "src/crucible/data/pricing.yaml", shallow=False
    )


def test_bundled_default_matrix_exists():
    assert (REPO_ROOT / "src/crucible/data/configs/default.yaml").is_file()


# --- init ---

def test_init_creates_layout(repo, capsys):
    assert main(["init"]) == 0
    out = capsys.readouterr().out
    home = repo / ".crucible"
    assert str(home) in out
    files = sorted(str(p.relative_to(home)) for p in home.rglob("*") if p.is_file())
    assert files == [
        ".gitignore", "README.md", "configs/.gitkeep", "results/.gitkeep", "tasks/.gitkeep",
    ]
    assert (home / ".gitignore").read_text().split() == ["results/", "mine/"]
    readme = (home / "README.md").read_text()
    for needle in ("crucible", "results/", ".gitignore"):
        assert needle in readme


def test_init_twice_changes_nothing(repo, capsys):
    assert main(["init"]) == 0
    before = snapshot(repo / ".crucible")
    mtimes = {p: p.stat().st_mtime_ns for p in (repo / ".crucible").rglob("*") if p.is_file()}
    capsys.readouterr()
    assert main(["init"]) == 0
    assert "already initialised" in capsys.readouterr().out
    assert snapshot(repo / ".crucible") == before
    assert {p: p.stat().st_mtime_ns for p in mtimes} == mtimes


def test_init_from_subdir_uses_toplevel(repo):
    sub = repo / "a" / "b"
    sub.mkdir(parents=True)
    os.chdir(sub)
    assert main(["init"]) == 0
    assert (repo / ".crucible" / "tasks" / ".gitkeep").is_file()
    assert not (sub / ".crucible").exists()


def test_init_outside_git_repo_fails(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("CRUCIBLE_HOME")
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.chdir(plain)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    assert main(["init"]) == 1
    assert "git repo" in capsys.readouterr().err
    assert not (plain / ".crucible").exists()


# --- home discovery ---

def test_home_from_repo_subdir(repo):
    main(["init"])
    sub = repo / "x" / "y"
    sub.mkdir(parents=True)
    os.chdir(sub)
    assert paths.home() == repo / ".crucible"
    assert paths.home_kind() == "repo"


def test_crucible_dir_beats_pricing_in_same_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("CRUCIBLE_HOME")
    (tmp_path / ".crucible").mkdir()
    (tmp_path / "pricing.yaml").write_text("models: {}\n")
    monkeypatch.chdir(tmp_path)
    assert paths.home() == tmp_path.resolve() / ".crucible"


def test_nearest_ancestor_wins(tmp_path, monkeypatch):
    monkeypatch.delenv("CRUCIBLE_HOME")
    (tmp_path / ".crucible").mkdir()
    inner = tmp_path / "inner"
    inner.mkdir()
    (inner / "pricing.yaml").write_text("models: {}\n")
    monkeypatch.chdir(inner)
    assert paths.home() == inner.resolve()
    assert paths.home_kind() == "standalone"


def test_crucible_repo_is_standalone(monkeypatch):
    monkeypatch.delenv("CRUCIBLE_HOME")
    for cwd in (REPO_ROOT, REPO_ROOT / "src" / "crucible"):
        monkeypatch.chdir(cwd)
        assert paths.home() == REPO_ROOT
        assert paths.home_kind() == "standalone"


def test_env_home_kind_standalone(crucible_home):
    assert paths.home_kind() == "standalone"


def test_env_home_kind_repo_when_named_dot_crucible(tmp_path, monkeypatch):
    h = tmp_path / ".crucible"
    h.mkdir()
    monkeypatch.setenv("CRUCIBLE_HOME", str(h))
    assert paths.home_kind() == "repo"


def test_no_home_error_suggests_init(tmp_path, monkeypatch):
    monkeypatch.delenv("CRUCIBLE_HOME")
    empty = tmp_path / "nowhere"
    empty.mkdir()
    monkeypatch.chdir(empty)
    with pytest.raises(paths.CrucibleHomeError, match="crucible init"):
        paths.home()


# --- resolution of pricing and configs ---

def test_bundled_fallback_in_initialised_repo(repo, capsys):
    main(["init"])
    capsys.readouterr()
    assert paths.pricing_file() == REPO_ROOT / "src/crucible/data/pricing.yaml"
    assert main(["configs", "default"]) == 0
    out = capsys.readouterr().out
    assert out.count("claude-opus-5-5") == 2 and out.count("claude-sonnet-5-5") == 2
    assert "$4.00" in out and "$20.00" in out
    assert len(out.strip().splitlines()) == 4


def test_local_pricing_overrides(repo, capsys):
    main(["init"])
    data = yaml.safe_load((REPO_ROOT / "pricing.yaml").read_text())
    local = repo / ".crucible" / "pricing.yaml"
    local.write_text(yaml.safe_dump(data))
    assert paths.pricing_file() == local
    data["models"]["claude-opus-5-5"]["input"] = 12.34
    local.write_text(yaml.safe_dump(data))
    capsys.readouterr()
    assert main(["configs", "default"]) == 0
    assert "$12.34 in" in capsys.readouterr().out


def test_local_configs_listed_and_marked(repo, capsys):
    main(["init"])
    (repo / ".crucible" / "configs" / "mine.yaml").write_text("configs: [opus:low]\n")
    assert main(["configs"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert any(line.startswith("mine") and "local" in line for line in lines)
    assert any(line.startswith("default") and "local" not in line for line in lines)
    assert [c.slug for c in configs.load_named("mine")] == ["opus-low"]


def test_local_config_overrides_bundled_name(repo):
    main(["init"])
    (repo / ".crucible" / "configs" / "default.yaml").write_text("configs: [opus:low]\n")
    assert [c.slug for c in configs.load_named("default")] == ["opus-low"]
    assert configs.list_matrices() == ["default"]


def test_unknown_named_matrix_still_errors(repo):
    main(["init"])
    with pytest.raises(configs.ConfigError, match="not found"):
        configs.load_named("zzz")


# --- packaging ---

@pytest.mark.skipif(shutil.which("uv") is None, reason="no uv")
def test_wheel_contains_data(tmp_path):
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path)], cwd=REPO_ROOT, check=True,
        capture_output=True,
    )
    (whl,) = tmp_path.glob("crucible-*.whl")
    names = zipfile.ZipFile(whl).namelist()
    assert "crucible/data/pricing.yaml" in names
    assert "crucible/data/configs/default.yaml" in names
