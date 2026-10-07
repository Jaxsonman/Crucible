import json
import os
import re
import shutil
import subprocess

import pytest
from conftest import REPO_ROOT

import crucible

SHIM = REPO_ROOT / "bin" / "crucible"
SKILLS = ["create-task", "init", "run", "report", "mine"]


def test_plugin_json():
    data = json.loads((REPO_ROOT / ".claude-plugin" / "plugin.json").read_text())
    assert data["name"] == "crucible"
    assert data["version"] == crucible.__version__
    assert data["license"] == "MIT"
    assert data["description"] and data["keywords"]


def test_marketplace_json():
    data = json.loads((REPO_ROOT / ".claude-plugin" / "marketplace.json").read_text())
    assert data["name"] == "crucible"
    assert data["owner"]["name"]
    assert len(data["plugins"]) == 1
    assert data["plugins"][0]["name"] == "crucible"
    assert data["plugins"][0]["source"] == "./"


def test_license_and_old_skill_gone():
    assert "MIT License" in (REPO_ROOT / "LICENSE").read_text()
    assert not (REPO_ROOT / ".claude" / "skills" / "create-task").exists()


@pytest.mark.parametrize("name", SKILLS)
def test_skill_frontmatter(name):
    text = (REPO_ROOT / "skills" / name / "SKILL.md").read_text()
    m = re.match(r"---\n(.*?)\n---\n", text, re.DOTALL)
    assert m, "missing frontmatter"
    fm = m.group(1)
    assert f"name: {name}\n" in fm + "\n"
    assert "description:" in fm
    assert ("disable-model-invocation: true" in fm) == (name in ("init", "run", "mine"))
    assert "uv run crucible" not in text


def test_shim_is_executable():
    assert os.access(SHIM, os.X_OK)


def _shim_env(tmp_path):
    env = {k: v for k, v in os.environ.items() if k not in ("CRUCIBLE_HOME", "VIRTUAL_ENV")}
    env["CRUCIBLE_VENV_DIR"] = str(tmp_path / "venvs")
    return env


def _status():
    return subprocess.run(["git", "status", "--short"], cwd=REPO_ROOT, capture_output=True, check=False,
                          text=True).stdout


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not installed")
def test_shim_runs_from_other_cwd_and_reuses_venv(tmp_path):
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    env = _shim_env(tmp_path)
    before = _status()
    for _ in range(2):
        r = subprocess.run([str(SHIM), "--version"], cwd=cwd, env=env, capture_output=True, check=False,
                           text=True)
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == crucible.__version__
    assert len(list((tmp_path / "venvs").iterdir())) == 1
    assert _status() == before


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not installed")
def test_shim_follows_symlink(tmp_path):
    link = tmp_path / "link"
    link.symlink_to(SHIM)
    r = subprocess.run([str(link), "--version"], cwd=tmp_path, env=_shim_env(tmp_path),
                       capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == crucible.__version__


def test_shim_without_uv_exits_127(tmp_path):
    env = _shim_env(tmp_path)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for tool in ("dirname", "readlink", "cksum", "cut"):
        found = shutil.which(tool)
        if found:
            (bindir / tool).symlink_to(found)
    env["PATH"] = str(bindir)
    r = subprocess.run(["/bin/sh", str(SHIM), "--version"], cwd=tmp_path, env=env,
                       capture_output=True, text=True, check=False)
    assert r.returncode == 127
    assert "uv" in r.stderr and "install" in r.stderr.lower()


@pytest.mark.parametrize("name", SKILLS)
def test_skill_has_no_positional_argument_placeholders(name):
    # Claude Code substitutes $0, $1, ... and $ARGUMENTS[N] in skill text with the invocation's
    # arguments, so a shell `$0` inside a code block is corrupted (found in the plugin e2e run).
    text = (REPO_ROOT / "skills" / name / "SKILL.md").read_text()
    assert not re.findall(r"\$\{?[0-9]\b|\$ARGUMENTS\[", text)


def test_five_skills_and_mine_frontmatter():
    names = sorted(p.parent.name for p in (REPO_ROOT / "skills").glob("*/SKILL.md"))
    assert names == sorted(SKILLS)
    text = (REPO_ROOT / "skills" / "mine" / "SKILL.md").read_text()
    assert 'argument-hint: "[--limit N] [--local] [--max-lines N]"' in text


def test_mine_skill_documents_real_commands():
    text = (REPO_ROOT / "skills" / "mine" / "SKILL.md").read_text()
    for cmd in ("crucible init", "crucible mine --json", "crucible mine --verify", "crucible mine scaffold",
                "crucible mine show", "crucible mine leakcheck", "crucible mine prove",
                "crucible task validate"):
        assert cmd in text
    assert "/crucible:run" in text


def test_docs_mention_mine():
    assert "`mine`" in (REPO_ROOT / "docs" / "plugin.md").read_text()
    readme = (REPO_ROOT / "README.md").read_text()
    assert "/crucible:mine" in readme and "mine.yaml" in readme
    assert "/crucible:mine" in (REPO_ROOT / "skills" / "create-task" / "SKILL.md").read_text()
