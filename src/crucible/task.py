"""Task document parsing and validation. See docs/design.md (Task document)."""

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
PLACEHOLDER_RE = re.compile(r"_\(.*?\)_", re.DOTALL)
SECTIONS = ("What", "Why", "Acceptance criteria")
BULLET_RE = re.compile(r"^\s*[-*]\s+(.*\S)\s*$")

TEMPLATE = """---
id: {id}
kind: feature
repo: fixture
# check: ./check.sh
---

## What

_(Describe the change the agent should make.)_

## Why

_(Explain the motivation.)_

## Acceptance criteria

- _(One concrete, checkable criterion per bullet.)_
"""


class TaskError(Exception):
    pass


@dataclass
class Task:
    id: str
    kind: str
    repo: str
    ref: str | None
    check: str | None
    timeout_minutes: int
    max_turns: int
    what: str
    why: str
    criteria: list[str]
    task_dir: Path
    source: str | None = None  # mined tasks: PR URL, or `commit:<merge sha>`

    @property
    def repo_path(self) -> Path:
        """Resolved repository path (the fixture dir, or the repo resolved against task_dir)."""
        if self.repo == "fixture":
            return self.task_dir / "fixture"
        return (self.task_dir / self.repo).resolve()

    @property
    def check_path(self) -> Path | None:
        return (self.task_dir / self.check).resolve() if self.check else None


def _split_frontmatter(text: str) -> tuple[str, str] | None:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[1:i]), "\n".join(lines[i + 1 :])
    return None


def _sections(body: str) -> dict[str, str]:
    out: dict[str, list[str]] = {}
    current = None
    for line in body.splitlines():
        m = re.match(r"^##\s+(.*?)\s*$", line)
        if m:
            current = m.group(1)
            out.setdefault(current, [])
        elif current is not None:
            out[current].append(line)
    return {k: "\n".join(v).strip() for k, v in out.items()}


def _positive_int(fm: dict, key: str, default: int, fail) -> int:
    value = fm.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        fail(f"`{key}` must be a positive integer, got {value!r}")
    return value


def _criteria(text: str) -> tuple[list[str], bool]:
    """Parse the Acceptance criteria section. Returns (criteria, stray).

    A bullet (`-` or `*`) at column 0, or any bullet while none is open, starts a criterion.
    Any other non-blank line continues the open criterion (Markdown lazy continuation), joined
    with one space after stripping its whitespace; an indented nested bullet (`  - sub`) is a
    continuation too and keeps its `- ` text. A blank line closes the open criterion. `stray`
    is True when a non-blank line had no open criterion to continue.
    """
    criteria: list[str] = []
    is_open = False
    stray = False
    for line in text.splitlines():
        if not line.strip():
            is_open = False
            continue
        m = BULLET_RE.match(line)
        if m and (not is_open or not line[:1].isspace()):
            criteria.append(m.group(1))
            is_open = True
        elif is_open:
            criteria[-1] += " " + line.strip()
        else:
            stray = True
    return criteria, stray


def load_task(path: str | Path) -> Task:
    """Load a task from a task directory or its task.md. Raises TaskError."""
    path = Path(path)
    task_md = path / "task.md" if path.is_dir() else path
    task_dir = task_md.parent.resolve()
    task_md = task_dir / task_md.name

    def fail(msg: str):
        raise TaskError(f"{task_md}: {msg}")

    if not task_md.is_file():
        fail("file not found")
    parts = _split_frontmatter(task_md.read_text(encoding="utf-8"))
    if parts is None:
        fail("missing frontmatter (expected a leading '---' block)")
    raw_fm, body = parts
    try:
        fm = yaml.safe_load(raw_fm) or {}
    except yaml.YAMLError as e:
        fail(f"invalid frontmatter YAML: {e}")
    if not isinstance(fm, dict):
        fail("frontmatter must be a mapping")

    task_id = fm.get("id")
    if task_id is None:
        fail("missing `id` in frontmatter")
    task_id = str(task_id)
    if task_id != task_dir.name:
        fail(f"`id` {task_id!r} must equal the directory name {task_dir.name!r}")
    if not ID_RE.match(task_id):
        fail(f"`id` {task_id!r} must match {ID_RE.pattern}")

    kind = fm.get("kind")
    if not kind or not isinstance(kind, str):
        fail("missing `kind` in frontmatter")

    repo = fm.get("repo")
    if not repo or not isinstance(repo, str):
        fail("missing `repo` in frontmatter")
    ref = fm.get("ref")
    ref = str(ref) if ref is not None else None
    if repo == "fixture":
        if not (task_dir / "fixture").is_dir():
            fail("`repo` is 'fixture' but the fixture/ directory does not exist")
    else:
        repo_path = (task_dir / repo).resolve()
        if not repo_path.is_dir() or not (repo_path / ".git").exists():
            fail(f"`repo` {repo!r} is not an existing git repository ({repo_path})")
        if not ref:
            fail("`ref` is required when `repo` is not 'fixture'")

    check = fm.get("check")
    if check is not None and (not isinstance(check, str) or not (task_dir / check).is_file()):
        fail(f"`check` {check!r} does not point to an existing file in the task directory")

    source = fm.get("source")
    if source is not None and (not isinstance(source, str) or not source.strip()):
        fail(f"`source` must be a non-empty string, got {source!r}")

    timeout = _positive_int(fm, "timeout_minutes", 30, fail)
    max_turns = _positive_int(fm, "max_turns", 100, fail)

    sections = _sections(body)
    for name in SECTIONS:
        if name not in sections:
            fail(f"missing section '## {name}'")
        if not sections[name]:
            fail(f"section '## {name}' is empty")
    criteria, stray = _criteria(sections["Acceptance criteria"])
    if not criteria:
        fail("section '## Acceptance criteria' has no bullet criteria")
    if stray:
        fail("section '## Acceptance criteria' has text outside bullets")
    placeholders = [n for n in SECTIONS if PLACEHOLDER_RE.search(sections[n])]
    if placeholders:
        names = ", ".join(f"'## {n}'" for n in placeholders)
        fail(f"template placeholder text still present in section(s): {names}")

    return Task(
        id=task_id,
        kind=kind,
        repo=repo,
        ref=ref,
        check=check,
        timeout_minutes=timeout,
        max_turns=max_turns,
        what=sections["What"],
        why=sections["Why"],
        criteria=criteria,
        task_dir=task_dir,
        source=source,
    )


def scaffold_task(tasks_dir: Path, task_id: str) -> Path:
    """Create tasks/<id>/ from the template. Raises TaskError if invalid or existing."""
    if not ID_RE.match(task_id):
        raise TaskError(f"invalid task id {task_id!r}: must match {ID_RE.pattern}")
    task_dir = tasks_dir / task_id
    if task_dir.exists():
        raise TaskError(f"{task_dir} already exists; refusing to overwrite")
    (task_dir / "fixture").mkdir(parents=True)
    (task_dir / "fixture" / ".gitkeep").touch()
    (task_dir / "task.md").write_text(TEMPLATE.format(id=task_id), encoding="utf-8")
    return task_dir


REPO_TEMPLATE = """---
id: {id}
kind: {kind}
repo: {repo}
ref: {ref}
{extra}check: ./check.sh
---

## What

_(Describe the change the agent should make.)_

## Why

_(Explain the motivation.)_

## Acceptance criteria

- _(One concrete, checkable criterion per bullet.)_
"""

CHECK_STUB = """#!/usr/bin/env bash
# Run with the workspace as cwd; exit 0 only when the task is done.
echo "write your check: edit this script (it exits 1 until you do)" >&2
exit 1
"""


def _git_out(cwd: Path, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(cwd), *args], capture_output=True, encoding="utf-8",
            errors="replace", check=False,
        )
    except FileNotFoundError as e:
        raise TaskError("git executable not found") from e
    if proc.returncode != 0:
        raise TaskError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def repo_task_md(task_id: str, repo: str, ref: str, kind: str = "feature",
                 extra_front_matter: str = "") -> str:
    """task.md text for a repo-relative task (placeholder sections). `extra_front_matter` is
    whole `key: value` lines inserted before `check:`."""
    return REPO_TEMPLATE.format(id=task_id, kind=kind, repo=repo, ref=ref, extra=extra_front_matter)


def repo_relative(top: Path, task_dir: Path) -> str:
    """The `repo:` value for a task in `task_dir` whose repository root is `top`."""
    return Path(os.path.relpath(top, task_dir)).as_posix()


def scaffold_repo_task(tasks_dir: Path, task_id: str, ref: str = "HEAD") -> tuple[Path, bool]:
    """Create tasks/<id>/ pointing at the enclosing git repo at `ref` (resolved to a full SHA).

    Returns (task_dir, dirty). `dirty` means uncommitted changes outside .crucible/ exist.
    Raises TaskError if the id is invalid, the task exists, or the ref does not resolve.
    """
    if not ID_RE.match(task_id):
        raise TaskError(f"invalid task id {task_id!r}: must match {ID_RE.pattern}")
    tasks_dir = tasks_dir.resolve()
    task_dir = tasks_dir / task_id
    if task_dir.exists():
        raise TaskError(f"{task_dir} already exists; refusing to overwrite")
    tasks_dir.mkdir(parents=True, exist_ok=True)
    top = Path(_git_out(tasks_dir, "rev-parse", "--show-toplevel")).resolve()
    try:
        sha = _git_out(top, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    except TaskError:
        raise TaskError(f"ref {ref!r} does not exist in git repository {top}") from None
    dirty = bool(_git_out(top, "status", "--porcelain", "--", ".", ":(exclude).crucible"))
    task_dir.mkdir()
    (task_dir / "task.md").write_text(
        repo_task_md(task_id, repo_relative(top, task_dir), sha),
        encoding="utf-8",
    )
    check = task_dir / "check.sh"
    check.write_text(CHECK_STUB, encoding="utf-8")
    check.chmod(0o755)
    return task_dir, dirty
