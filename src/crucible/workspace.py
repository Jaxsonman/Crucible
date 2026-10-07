"""Isolated per-attempt workspaces. See docs/design.md."""

import os
import shutil
import subprocess
from pathlib import Path

from crucible import paths
from crucible.task import Task

# Identity and date are fixed so the baseline SHA is reproducible everywhere.
_FIXED_ENV = {
    "GIT_AUTHOR_NAME": "Crucible",
    "GIT_AUTHOR_EMAIL": "crucible@localhost",
    "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+0000",
    "GIT_COMMITTER_NAME": "Crucible",
    "GIT_COMMITTER_EMAIL": "crucible@localhost",
    "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+0000",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}
_FIXED_CONFIG = (
    "-c", "commit.gpgsign=false",
    "-c", "core.hooksPath=" + os.devnull,
    "-c", "core.autocrlf=false",
    "-c", "core.safecrlf=false",
    "-c", "core.fsmonitor=false",
    "-c", "init.defaultBranch=main",
    "-c", "color.ui=never",
)


class WorkspaceError(Exception):
    pass


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(_FIXED_ENV)
    return env


def _git(cwd: Path, *args: str, extra: tuple[str, ...] = ()) -> str:
    cmd = ["git", *_FIXED_CONFIG, "-C", str(cwd), *args, *extra]
    try:
        proc = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace", env=_env(), check=False)
    except FileNotFoundError as e:
        raise WorkspaceError("git executable not found") from e
    if proc.returncode != 0:
        raise WorkspaceError(f"git {' '.join(args)} failed in {cwd}: {proc.stderr.strip()}")
    return proc.stdout


def _safe_component(name: str, what: str) -> str:
    if not name or name in (".", "..") or "/" in name or "\\" in name or os.sep in name:
        raise WorkspaceError(f"invalid {what} {name!r}")
    return name


def export_tree(repo: Path, ref: str, dest: Path) -> None:
    """Export the tree at `ref` of git repo `repo` into existing dir `dest`, minus the top-level
    .crucible/ (hidden tests, tasks, results)."""
    env = _env()
    check = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"{ref}^{{tree}}"],
        capture_output=True, encoding="utf-8", errors="replace", env=env, check=False,
    )
    if check.returncode != 0:
        raise WorkspaceError(f"ref {ref!r} does not exist in git repository {repo}")
    archive = subprocess.Popen(
        ["git", "-C", str(repo), "archive", "--format=tar", ref, "--", ".", ":(exclude).crucible"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
    )
    tar = subprocess.run(
        ["tar", "-x", "-C", str(dest)], stdin=archive.stdout, capture_output=True, env=env, check=False
    )
    archive.stdout.close()
    err = archive.stderr.read().decode(errors="replace").strip()
    archive.stderr.close()
    if archive.wait() != 0 or tar.returncode != 0:
        raise WorkspaceError(
            f"failed to export {ref!r} from {repo}: {err or tar.stderr.decode(errors='replace')}"
        )


def create(task: Task, batch_id: str, attempt_slug: str) -> Path:
    """Create a fresh workspace with a baseline commit. Use baseline_sha() for its SHA."""
    root = paths.work_dir()
    ws = root / _safe_component(batch_id, "batch id") / _safe_component(attempt_slug, "attempt slug")
    if ws.exists():
        raise WorkspaceError(f"workspace {ws} already exists")
    ws.parent.mkdir(parents=True, exist_ok=True)
    ws.mkdir()
    try:
        if task.repo == "fixture":
            shutil.copytree(
                task.repo_path, ws, symlinks=True, dirs_exist_ok=True,
                ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc", ".DS_Store"),
            )
        else:
            export_tree(task.repo_path, task.ref, ws)
        _git(ws, "init", "-q", extra=("--template=",))
        _git(ws, "add", "-A", "-f")
        _git(ws, "commit", "-q", "--allow-empty", "--no-verify", "-m", "crucible baseline")
    except BaseException:
        shutil.rmtree(ws, ignore_errors=True)
        raise
    return ws


def baseline_sha(workspace: Path) -> str:
    """SHA of the baseline commit (the workspace's only commit)."""
    return _git(workspace, "rev-list", "--max-parents=0", "HEAD").strip()


def diff(workspace: Path) -> str:
    """Changes in the workspace vs. the baseline, including new and deleted files."""
    base = baseline_sha(workspace)
    _git(workspace, "add", "-A")
    return _git(workspace, "diff", "--cached", "--no-color", "--no-ext-diff", base)


def cleanup(workspace: Path) -> None:
    """Remove a workspace directory. Refuses anything outside paths.work_dir()."""
    root = paths.work_dir().resolve()
    target = Path(workspace).resolve()
    if target == root or root not in target.parents:
        raise WorkspaceError(f"refusing to remove {target}: not inside {root}")
    shutil.rmtree(target)
