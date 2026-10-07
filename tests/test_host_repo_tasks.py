import json
import os
import subprocess
from pathlib import Path

import pytest

from crucible import workspace
from crucible.cli import main
from crucible.task import load_task

FAKE = Path(__file__).parent / "fake_claude.py"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
}


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
        env={**os.environ, **GIT_ENV},
    ).stdout.strip()


def commit(repo, msg):
    git(repo, "add", "-A", "-f")
    git(repo, "commit", "-q", "-m", msg)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """Initialised git repo with 2 commits, .crucible/ committed, cwd inside it."""
    monkeypatch.delenv("CRUCIBLE_HOME")
    r = tmp_path / "proj"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / "f.txt").write_text("one\n")
    commit(r, "first")
    (r / "src").mkdir()
    (r / "src" / "app.py").write_text("print('hi')\n")
    commit(r, "second")
    r = r.resolve()
    monkeypatch.chdir(r)
    assert main(["init"]) == 0
    commit(r, "init crucible")
    return r


def task_dir(repo, name="t1"):
    return repo / ".crucible" / "tasks" / name


def fm(repo, name="t1"):
    text = (task_dir(repo, name) / "task.md").read_text()
    out = {}
    for line in text.split("---")[1].splitlines():
        if ":" in line and not line.startswith("#"):
            k, v = line.split(":", 1)
            out[k.strip()] = v.split(" #")[0].strip()
    return out


def test_task_new_in_repo_home(repo, capsys):
    assert main(["task", "new", "t1"]) == 0
    d = task_dir(repo)
    meta = fm(repo)
    assert meta["repo"] == "../../.."
    assert (d / meta["repo"]).resolve() == repo
    assert meta["ref"] == git(repo, "rev-parse", "HEAD") and len(meta["ref"]) == 40
    assert meta["check"] == "./check.sh"
    assert not (d / "fixture").exists()
    capsys.readouterr()
    assert main(["task", "validate", str(d)]) == 1
    err = capsys.readouterr().err
    assert "placeholder" in err
    for name in ("What", "Why", "Acceptance criteria"):
        assert f"'## {name}'" in err
    assert "`repo`" not in err and "`ref`" not in err and "`check`" not in err  # only placeholders


def test_stub_check_exits_nonzero(repo):
    main(["task", "new", "t1"])
    stub = task_dir(repo) / "check.sh"
    proc = subprocess.run(["sh", str(stub)], capture_output=True, text=True, check=False)
    assert proc.returncode != 0
    assert "write your check" in proc.stdout + proc.stderr


def test_ref_option_stores_full_sha(repo):
    assert main(["task", "new", "t2", "--ref", "HEAD~2"]) == 0
    assert fm(repo, "t2")["ref"] == git(repo, "rev-parse", "HEAD~2")
    assert len(fm(repo, "t2")["ref"]) == 40


def test_bad_ref_exits_1_and_creates_nothing(repo, capsys):
    assert main(["task", "new", "t3", "--ref", "nope"]) == 1
    assert "nope" in capsys.readouterr().err
    assert not task_dir(repo, "t3").exists()


def test_dirty_tree_warns_clean_does_not(repo, capsys):
    capsys.readouterr()
    assert main(["task", "new", "clean1"]) == 0
    cap = capsys.readouterr()
    assert "uncommitted" not in (cap.out + cap.err)
    (repo / "f.txt").write_text("changed\n")
    assert main(["task", "new", "dirty1"]) == 0
    cap = capsys.readouterr()
    assert "uncommitted changes are not part of the task" in cap.err
    (repo / "untracked.txt").write_text("u\n")
    git(repo, "checkout", "f.txt")
    assert main(["task", "new", "dirty2"]) == 0
    assert "uncommitted" in capsys.readouterr().err


def test_task_new_from_subdir(repo):
    sub = repo / "src"
    os.chdir(sub)
    assert main(["task", "new", "t1"]) == 0
    assert fm(repo)["repo"] == "../../.."


def test_validate_accepts_ancestor_repo(repo):
    main(["task", "new", "t1"])
    d = task_dir(repo)
    text = (d / "task.md").read_text()
    text = text.replace("_(Describe the change the agent should make.)_", "Do it.")
    text = text.replace("_(Explain the motivation.)_", "Because.")
    text = text.replace("_(One concrete, checkable criterion per bullet.)_", "It works.")
    (d / "task.md").write_text(text)
    t = load_task(d)
    assert t.repo_path == repo
    assert t.ref == git(repo, "rev-parse", "HEAD")


def test_standalone_home_keeps_fixture(crucible_home_standalone):
    assert main(["task", "new", "demo"]) == 0
    d = crucible_home_standalone / "tasks" / "demo"
    assert (d / "fixture" / ".gitkeep").is_file()
    assert "repo: fixture" in (d / "task.md").read_text()
    assert not (d / "check.sh").exists()


@pytest.fixture
def crucible_home_standalone(tmp_path, monkeypatch):
    home = tmp_path / "standalone"
    (home / "tasks").mkdir(parents=True)
    (home / "pricing.yaml").write_text("models: {}\n")
    monkeypatch.setenv("CRUCIBLE_HOME", str(home))
    return home


def hidden_repo(repo):
    d = task_dir(repo)
    (d / "hidden_tests").mkdir(parents=True)
    (d / "hidden_tests" / "secret_test.py").write_text("assert False\n")
    res = repo / ".crucible" / "results" / "t1" / "b1" / "a-1"
    res.mkdir(parents=True)
    (res / "record.json").write_text("{}")
    (repo / ".crucible" / "notes.txt").write_text("n\n")
    (repo / "pkg" / ".crucible").mkdir(parents=True)  # nested dir named .crucible is host content
    (repo / "pkg" / ".crucible" / "keep.txt").write_text("k\n")
    commit(repo, "commit crucible data")


def finish_task(repo):
    d = task_dir(repo)
    text = (d / "task.md").read_text()
    text = text.replace("_(Describe the change the agent should make.)_", "Do it.")
    text = text.replace("_(Explain the motivation.)_", "Because.")
    text = text.replace("_(One concrete, checkable criterion per bullet.)_", "It works.")
    (d / "task.md").write_text(text)
    (d / "check.sh").write_text("#!/bin/sh\ntest -f agent.txt\n")
    return d


def test_workspace_excludes_crucible_dir(repo):
    main(["task", "new", "t1"])
    d = finish_task(repo)
    hidden_repo(repo)
    # ref is the pre-hidden-data commit; re-point at current HEAD where .crucible has everything
    text = (d / "task.md").read_text().replace(fm(repo)["ref"], git(repo, "rev-parse", "HEAD"))
    (d / "task.md").write_text(text)
    assert git(repo, "ls-tree", "-r", "--name-only", "HEAD").count(".crucible/") > 3
    ws = workspace.create(load_task(d), "b", "x-1")
    assert not (ws / ".crucible").exists()
    assert not [p for p in ws.rglob("*") if ".crucible" in p.relative_to(ws).parts[:1]]
    tracked = git(ws, "ls-files").splitlines()
    assert not any(t.split("/")[0] == ".crucible" for t in tracked)
    assert (ws / "f.txt").read_text() == "one\n"
    assert (ws / "src" / "app.py").is_file()
    assert (ws / "pkg" / ".crucible" / "keep.txt").is_file()


def test_exclusion_keeps_baseline_reproducible(repo):
    main(["task", "new", "t1"])
    d = finish_task(repo)
    hidden_repo(repo)
    t = load_task(d)
    w1 = workspace.create(t, "b", "x-1")
    w2 = workspace.create(t, "b", "y-1")
    assert workspace.baseline_sha(w1) == workspace.baseline_sha(w2)


def test_e2e_run_from_subdir_writes_results_in_crucible(repo, monkeypatch):
    main(["task", "new", "t1"])
    d = finish_task(repo)
    hidden_repo(repo)
    text = (d / "task.md").read_text().replace(fm(repo)["ref"], git(repo, "rev-parse", "HEAD"))
    (d / "task.md").write_text(text)
    monkeypatch.setenv("CRUCIBLE_CLAUDE_BIN", str(FAKE))
    monkeypatch.setenv("FAKE_CLAUDE_WRITE", json.dumps({"agent.txt": "x\n"}))
    os.chdir(repo / "src")
    assert main(["run", str(d), "--configs", "sonnet:medium"]) == 0
    recs = list((repo / ".crucible" / "results" / "t1").glob("*/*/record.json"))
    recs = [r for r in recs if r.parent.parent.name != "b1"]  # b1 is the committed fake result
    assert len(recs) == 1
    rec = json.loads(recs[0].read_text())
    assert rec["status"] == "completed"
    assert rec["check"]["passed"] is True
    assert not (repo / "src" / "results").exists()


def test_workspace_excludes_crucible_independent_of_task_new(repo):
    d = task_dir(repo)
    d.mkdir(parents=True)
    (d / "task.md").write_text(
        f"---\nid: t1\nkind: feature\nrepo: ../../..\nref: {git(repo, 'rev-parse', 'HEAD')}\n---\n"
        "\n## What\n\nDo it.\n\n## Why\n\nBecause.\n\n## Acceptance criteria\n\n- It works.\n"
    )
    hidden_repo(repo)
    t = load_task(d)
    t.ref = git(repo, "rev-parse", "HEAD")
    assert "hidden_tests/secret_test.py" in git(repo, "ls-tree", "-r", "--name-only", t.ref)
    ws = workspace.create(t, "b", "x-1")
    assert not (ws / ".crucible").exists()
    assert (ws / "src" / "app.py").is_file()
