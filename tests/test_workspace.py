import os
import subprocess
from pathlib import Path

import pytest

from crucible import paths, workspace
from crucible.task import load_task
from crucible.workspace import WorkspaceError

BODY = """
## What

Do the thing.

## Why

Because.

## Acceptance criteria

- It works.
"""


def write_task(home, repo="fixture", ref=None, name="t1"):
    d = home / "tasks" / name
    d.mkdir(parents=True)
    fm = f"id: {name}\nkind: feature\nrepo: {repo}"
    if ref:
        fm += f"\nref: {ref}"
    if repo == "fixture":
        (d / "fixture").mkdir()
        (d / "fixture" / "a.txt").write_text("alpha\n")
        (d / "fixture" / "b.txt").write_text("bravo\n")
        (d / "fixture" / "sub").mkdir()
        (d / "fixture" / "sub" / "c.txt").write_text("charlie\n")
    (d / "check.sh").write_text("#!/bin/sh\nexit 0\n")
    fm += "\ncheck: ./check.sh"
    (d / "task.md").write_text(f"---\n{fm}\n---\n{BODY}")
    return load_task(d)


def git(repo, *args, env=None):
    full_env = {**os.environ, **(env or {})}
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=full_env
    ).stdout.strip()


@pytest.fixture
def fixture_task(crucible_home):
    return write_task(crucible_home)


def make_git_repo(path: Path):
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "t@example.com")
    git(path, "config", "user.name", "T")
    git(path, "config", "commit.gpgsign", "false")
    (path / "one.txt").write_text("one\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "first")
    git(path, "tag", "v1")
    (path / "two.txt").write_text("two\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "second (the 'solution')")
    git(path, "branch", "feature")
    return path


def test_same_baseline_sha(fixture_task):
    w1 = workspace.create(fixture_task, "batch", "opus-high-1")
    w2 = workspace.create(fixture_task, "batch", "sonnet-medium-1")
    assert w1 != w2
    sha = workspace.baseline_sha(w1)
    assert len(sha) == 40
    assert sha == workspace.baseline_sha(w2)


def test_sha_independent_of_global_git_config(fixture_task, tmp_path, monkeypatch):
    w1 = workspace.create(fixture_task, "b1", "a-1")
    home2 = tmp_path / "otherhome"
    home2.mkdir()
    cfg = home2 / ".gitconfig"
    cfg.write_text(
        "[user]\n\tname = Someone Else\n\temail = else@example.com\n"
        "[commit]\n\tgpgsign = true\n[core]\n\tautocrlf = true\n\thooksPath = /nonexistent\n"
        "[init]\n\tdefaultBranch = trunk\n"
    )
    monkeypatch.setenv("HOME", str(home2))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Env Author")
    monkeypatch.setenv("GIT_AUTHOR_DATE", "2001-01-01T00:00:00Z")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "env@example.com")
    w2 = workspace.create(fixture_task, "b2", "a-1")
    assert workspace.baseline_sha(w1) == workspace.baseline_sha(w2)


def test_hooks_do_not_run(fixture_task, tmp_path, monkeypatch):
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    marker = tmp_path / "hook-ran"
    hook = hooks / "pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n")
    hook.chmod(0o755)
    cfg = tmp_path / "gitconfig"
    cfg.write_text(f"[core]\n\thooksPath = {hooks}\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    w = workspace.create(fixture_task, "b", "a-1")
    assert len(workspace.baseline_sha(w)) == 40
    assert not marker.exists()


def test_isolation_between_workspaces_and_tasks(fixture_task):
    w1 = workspace.create(fixture_task, "b", "x-1")
    w2 = workspace.create(fixture_task, "b", "y-1")
    (w1 / "new.txt").write_text("hi")
    assert not (w2 / "new.txt").exists()
    assert not (fixture_task.task_dir / "fixture" / "new.txt").exists()
    assert not list(paths.tasks_dir().rglob("new.txt"))


def test_diff_shows_add_modify_delete(fixture_task):
    w = workspace.create(fixture_task, "b", "x-1")
    (w / "added.txt").write_text("added\n")
    (w / "a.txt").write_text("changed\n")
    (w / "b.txt").unlink()
    d = workspace.diff(w)
    assert "added.txt" in d and "+added" in d
    assert "a.txt" in d and "+changed" in d and "-alpha" in d
    assert "b.txt" in d and "deleted file mode" in d
    assert "c.txt" not in d


def test_diff_empty_when_unchanged(fixture_task):
    w = workspace.create(fixture_task, "b", "x-1")
    assert workspace.diff(w) == ""


def test_git_repo_task_exports_ref_only(crucible_home, tmp_path):
    repo = make_git_repo(tmp_path / "src-repo")
    task = write_task(crucible_home, repo=str(repo), ref="v1")
    w = workspace.create(task, "b", "x-1")
    assert (w / "one.txt").read_text() == "one\n"
    assert not (w / "two.txt").exists()
    assert git(w, "log", "--all", "--oneline").count("\n") == 0
    assert len(git(w, "log", "--all", "--format=%H").splitlines()) == 1
    assert git(w, "rev-parse", "HEAD") == workspace.baseline_sha(w)
    # a branch name works too
    w2 = workspace.create(task_with_ref(crucible_home, repo, "feature", "t2"), "b", "y-1")
    assert (w2 / "two.txt").exists()
    assert len(git(w2, "log", "--all", "--format=%H").splitlines()) == 1


def task_with_ref(home, repo, ref, name):
    return write_task(home, repo=str(repo), ref=ref, name=name)


def test_missing_ref_fails_clearly(crucible_home, tmp_path):
    repo = make_git_repo(tmp_path / "src-repo")
    task = write_task(crucible_home, repo=str(repo), ref="nope")
    with pytest.raises(WorkspaceError, match="nope"):
        workspace.create(task, "b", "x-1")
    assert not (paths.work_dir() / "b" / "x-1").exists()


def test_task_dir_files_not_in_workspace(fixture_task, crucible_home, tmp_path):
    w = workspace.create(fixture_task, "b", "x-1")
    assert not (w / "check.sh").exists()
    assert not (w / "task.md").exists()
    assert (fixture_task.task_dir / "check.sh").exists()
    repo = make_git_repo(tmp_path / "src-repo")
    task = write_task(crucible_home, repo=str(repo), ref="v1", name="t3")
    w3 = workspace.create(task, "b", "z-1")
    assert not (w3 / "check.sh").exists()
    assert not (w3 / "task.md").exists()


def test_fixture_caches_not_copied(fixture_task):
    fixture = fixture_task.task_dir / "fixture"
    (fixture / "__pycache__").mkdir()
    (fixture / "__pycache__" / "m.cpython-313.pyc").write_bytes(b"x")
    (fixture / ".DS_Store").write_bytes(b"x")
    w = workspace.create(fixture_task, "b", "c-1")
    assert not (w / "__pycache__").exists()
    assert not (w / ".DS_Store").exists()


def test_workspace_outside_home(fixture_task):
    w = workspace.create(fixture_task, "b", "x-1")
    assert w == paths.work_dir() / "b" / "x-1"
    assert not w.resolve().is_relative_to(paths.home().resolve())


def test_cleanup_removes_and_writes_only_in_work_dir(fixture_task, crucible_home, tmp_path):
    before = {p for p in tmp_path.rglob("*") if paths.work_dir() not in p.parents}
    w = workspace.create(fixture_task, "b", "x-1")
    workspace.diff(w)
    workspace.cleanup(w)
    assert not w.exists()
    after = {p for p in tmp_path.rglob("*") if paths.work_dir() not in p.parents}
    assert after == before - {p for p in before if not p.exists()}
    assert after == before


def test_cleanup_refuses_outside_work_dir(tmp_path):
    victim = tmp_path / "victim"
    victim.mkdir()
    with pytest.raises(WorkspaceError):
        workspace.cleanup(victim)
    assert victim.exists()


def test_create_refuses_existing_workspace(fixture_task):
    workspace.create(fixture_task, "b", "x-1")
    with pytest.raises(WorkspaceError, match="exists"):
        workspace.create(fixture_task, "b", "x-1")


def test_rejects_path_traversal_in_names(fixture_task):
    with pytest.raises(WorkspaceError):
        workspace.create(fixture_task, "..", "x-1")
    with pytest.raises(WorkspaceError):
        workspace.create(fixture_task, "b", "../../etc")
