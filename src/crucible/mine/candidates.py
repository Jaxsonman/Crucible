"""Mining candidates: the model, the filters and the local-git source.

Reads git only (`log`, `diff`); never changes HEAD, the index or any ref. The `gh` source
(`gh.py`) builds `Candidate`s with `make_candidate` and the same filters; verification
(`verify.py`) uses `test_files` / `other_files` statuses to overlay and delete files.
"""

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from crucible.mine.config import MineConfig, Suite, path_matches
from crucible.task import _split_frontmatter

DEFAULT_LIMIT = 300
MANIFEST_RE = re.compile(r"(?:requirements[^/]*\.txt|package\.json|pyproject\.toml)\Z")
_STATUS = {"A": "added", "D": "deleted"}


class MineGitError(RuntimeError):
    """A git command needed for mining failed."""


@dataclass(frozen=True)
class FileChange:
    path: str
    status: str  # "added" | "modified" | "deleted"
    added: int = 0
    deleted: int = 0  # binary files count 0 lines

    def to_dict(self) -> dict:
        return {"path": self.path, "status": self.status, "added": self.added,
                "deleted": self.deleted}


@dataclass(frozen=True)
class Candidate:
    id: str  # PR number, or short merge SHA when there is none
    title: str
    base: str  # first parent of `merge` (full SHA); "" for a root commit
    merge: str  # full SHA
    test_files: list[FileChange]
    other_files: list[FileChange]
    lines_changed: int  # added + deleted over every changed file
    suites: list[str]  # suites owning the test files, config order (first matching suite owns a file)
    suggested_kind: str
    source: str  # PR URL, or `commit:<merge sha>` without gh
    url: str | None = None
    labels: list[str] = field(default_factory=list)
    branch: str | None = None
    issues: list[dict] = field(default_factory=list)  # {number, title, body} from gh
    body: str = ""
    counted_lines: int | None = None  # same, ignoring `exclude_paths` files; the size filter's input

    @property
    def suite(self) -> str | None:
        """The first suite (kept for compatibility); None without test files."""
        return self.suites[0] if self.suites else None

    @property
    def size(self) -> int:
        """Lines the size cap applies to (`lines_changed` when `counted_lines` was not set)."""
        return self.lines_changed if self.counted_lines is None else self.counted_lines

    def to_dict(self) -> dict:
        return {
            "id": self.id, "title": self.title, "base": self.base, "merge": self.merge,
            "test_files": [f.to_dict() for f in self.test_files],
            "other_files": [f.to_dict() for f in self.other_files],
            "lines_changed": self.lines_changed, "counted_lines": self.size, "suite": self.suite,
            "suites": list(self.suites),
            "suggested_kind": self.suggested_kind, "source": self.source, "url": self.url,
            "labels": self.labels, "branch": self.branch, "issues": self.issues,
            "body": self.body,
        }


@dataclass(frozen=True)
class Excluded:
    candidate: Candidate
    reason: str

    def to_dict(self) -> dict:
        return {**self.candidate.to_dict(), "reason": self.reason}


@dataclass(frozen=True)
class MinedTask:
    ref: str | None
    source: str
    merge: str | None = None  # from the task's `.mine.json`


# --- git ---

def _git(repo: Path, *args: str) -> bytes:
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    try:
        proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, env=env, check=False)
    except FileNotFoundError as e:
        raise MineGitError("git executable not found") from e
    if proc.returncode != 0:
        msg = proc.stderr.decode("utf-8", "replace").strip()
        raise MineGitError(f"git {' '.join(args[:2])} failed: {msg}")
    return proc.stdout


def file_changes(repo: Path, base: str, merge: str) -> list[FileChange]:
    """Files changed from `base` to `merge`, with status and line counts. Renames are reported
    as a delete plus an add (`--no-renames`), so each path has one unambiguous status."""
    names = _git(repo, "diff", "--name-status", "-z", "--no-renames", base, merge)
    nums = _git(repo, "diff", "--numstat", "-z", "--no-renames", base, merge)
    counts: dict[str, tuple[int, int]] = {}
    for rec in filter(None, nums.decode("utf-8", "replace").split("\0")):
        a, d, path = rec.split("\t", 2)
        counts[path] = (int(a) if a.isdigit() else 0, int(d) if d.isdigit() else 0)  # '-' = binary
    toks = names.decode("utf-8", "replace").split("\0")
    out = []
    for i in range(0, len(toks) - 1, 2):
        code, path = toks[i], toks[i + 1]
        status = _STATUS.get(code[:1], "modified")
        out.append(FileChange(path, status, *counts.get(path, (0, 0))))
    return sorted(out, key=lambda f: f.path)


EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbb4fa4d"


def diff_text(repo: Path, base: str, merge: str, paths: list[str]) -> str:
    """Unified diff base->merge limited to exactly `paths` ("" when there are none)."""
    if not paths:
        return ""
    out = _git(repo, "diff", "--no-renames", "--no-color", "--no-ext-diff", base or EMPTY_TREE,
               merge, "--", *[f":(literal){p}" for p in paths])
    return out.decode("utf-8", "replace")


# --- classification helpers ---

def parse_pr_number(subject: str) -> int | None:
    """PR number from `Merge pull request #N ...` or a trailing `(#N)`."""
    m = re.match(r"Merge pull request #(\d+)\b", subject) or re.search(r"\(#(\d+)\)\s*$", subject)
    return int(m.group(1)) if m else None


_KIND_BRANCH = (
    (("fix/", "bugfix/", "hotfix/"), "bugfix"),
    (("refactor/",), "refactor"),
    (("feat/", "feature/"), "feature"),
)
_KIND_TITLE = (
    (re.compile(r"\b(?:fix|fixes|fixed|bug|bugfix)\b", re.IGNORECASE), "bugfix"),
    (re.compile(r"\b(?:refactor|refactors|refactored|refactoring|cleanup|rename)\b",
                re.IGNORECASE), "refactor"),
)
_KIND_LABEL = (
    (("bug", "bugfix", "fix"), "bugfix"),
    (("refactor", "cleanup"), "refactor"),
    (("feature", "enhancement", "feat"), "feature"),
)


def suggest_kind(labels: list[str], branch: str | None, title: str) -> str:
    """Labels first, then branch prefix, then title words, else `feature`."""
    for label in labels:
        for words, kind in _KIND_LABEL:
            if label.strip().lower() in words:
                return kind
    if branch:
        # `owner/fix/x` (from a PR merge subject) still counts as the `fix/` prefix
        parts = branch.split("/")
        for start in range(min(2, len(parts))):
            tail = "/".join(parts[start:])
            for prefixes, kind in _KIND_BRANCH:
                if tail.startswith(prefixes):
                    return kind
    for rx, kind in _KIND_TITLE:
        if rx.search(title):
            return kind
    return "feature"


def owning_suite(path: str, suites: list[Suite]) -> str | None:
    """The first suite (config order) whose `test_paths` match `path`."""
    return next((s.name for s in suites if path_matches(path, s.test_paths)), None)


def split_files(
    changes: list[FileChange], suites: list[Suite]
) -> tuple[list[FileChange], list[FileChange], list[str]]:
    """(test files, other files, suites owning test files in config order). A test file
    matches some suite's `test_paths`; the first matching suite owns it."""
    tests, other, owners = [], [], set()
    for f in changes:
        owner = owning_suite(f.path, suites)
        if owner:
            tests.append(f)
            owners.add(owner)
        else:
            other.append(f)
    return tests, other, [s.name for s in suites if s.name in owners]


def make_candidate(
    repo: Path, cfg: MineConfig, *, id: str, title: str, base: str, merge: str, source: str,
    labels: list[str] | None = None, branch: str | None = None, **extra,
) -> Candidate:
    """Build a candidate; the file split and line counts always come from the local diff.
    `extra` may carry `url`, `issues` and `body`. An empty `base` (root commit) has no files."""
    changes = file_changes(repo, base, merge) if base else []
    tests, other, owners = split_files(changes, cfg.suites)
    labels = labels or []
    return Candidate(
        id=id, title=title, base=base, merge=merge, test_files=tests, other_files=other,
        lines_changed=sum(f.added + f.deleted for f in changes),
        counted_lines=sum(f.added + f.deleted for f in changes
                          if f in tests or not path_matches(f.path, cfg.exclude_paths)),
        suites=owners,
        suggested_kind=suggest_kind(labels, branch, title), source=source,
        labels=labels, branch=branch, **extra,
    )


# --- local source ---

_MERGE_PR_FROM_FULL = re.compile(r"Merge pull request #\d+ from (\S+)")
_MERGE_BRANCH = re.compile(r"Merge branch '([^']+)'")


def _title_and_branch(subject: str, rest: str) -> tuple[str, str | None]:
    """A `Merge pull request` subject is useless as a title: the PR title is the first body line."""
    m = _MERGE_PR_FROM_FULL.match(subject)
    if m:
        first = next((ln.strip() for ln in rest.splitlines() if ln.strip()), "")
        return first or subject, m.group(1)
    m = _MERGE_BRANCH.match(subject)
    return subject, m.group(1) if m else None


def local_candidates(
    repo: Path, cfg: MineConfig, branch: str = "HEAD", limit: int = DEFAULT_LIMIT
) -> list[Candidate]:
    """One candidate per first-parent commit of `branch` (newest first), unfiltered. Merge
    commits diff against their first parent; ordinary commits against their parent."""
    raw = _git(repo, "log", "--first-parent", "-n", str(limit), "-z",
               "--format=%H%x1f%P%x1f%B", branch, "--").decode("utf-8", "replace")
    out = []
    for rec in filter(None, raw.split("\0")):
        sha, parents, message = rec.strip("\n").split("\x1f", 2)
        subject, _, rest = message.partition("\n")
        base = parents.split()[0] if parents else ""
        title, br = _title_and_branch(subject, rest)
        pr = parse_pr_number(subject)
        out.append(make_candidate(
            repo, cfg, id=str(pr) if pr else sha[:8], title=title, base=base, merge=sha,
            source=f"commit:{sha}", branch=br, body=rest.strip(),
        ))
    return out


# --- already-mined tasks ---

def _mine_meta_merge(task_dir: Path, source: str) -> str | None:
    """The merge SHA recorded by `scaffold` in `.mine.json` (None if absent or for another source)."""
    try:
        meta = json.loads((task_dir / ".mine.json").read_text(encoding="utf-8"))
        if meta.get("source") in (None, source) and meta.get("merge"):
            return str(meta["merge"])
    except (OSError, ValueError, AttributeError):
        pass
    return None


def mined_tasks(tasks_dir: Path) -> list[MinedTask]:
    """`ref`, `source` and (from `.mine.json`) `merge` of every task under `tasks_dir`
    (unreadable ones are skipped)."""
    out = []
    for md in sorted(Path(tasks_dir).glob("*/task.md")):
        try:
            parts = _split_frontmatter(md.read_text(encoding="utf-8"))
            fm = yaml.safe_load(parts[0]) if parts else None
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            continue
        if isinstance(fm, dict) and fm.get("source"):
            ref = fm.get("ref")
            out.append(MinedTask(str(ref) if ref else None, str(fm["source"]),
                                 _mine_meta_merge(md.parent, str(fm["source"]))))
    return out


def _already_mined(c: Candidate, mined: list[MinedTask]) -> bool:
    for t in mined:
        if t.merge == c.merge:  # same merge commit, whichever source scaffolded the task
            return True
        if t.source in (c.source, f"commit:{c.merge}"):
            return True
        if c.base and t.ref == c.base and c.merge in t.source:
            return True
    return False


# --- filters ---

def exclusion_reason(
    c: Candidate, cfg: MineConfig, max_lines: int, mined: list[MinedTask]
) -> str | None:
    """Why `c` is not a usable candidate, or None."""
    if not c.base:
        return "root commit"
    if not c.test_files:
        return "no test file changed"
    if not c.other_files:
        return "no non-test file changed"
    live = [f for f in c.other_files if not path_matches(f.path, cfg.exclude_paths)]
    if not live:
        return "only excluded paths changed besides tests"
    if all(MANIFEST_RE.match(Path(f.path).name) for f in live):
        return "dependency manifest only besides tests"
    if c.size > max_lines:
        return f"{c.size} lines changed (max {max_lines})"
    if _already_mined(c, mined):
        return "already mined"
    return None


def filter_candidates(
    cands: list[Candidate], cfg: MineConfig, max_lines: int, mined: list[MinedTask]
) -> tuple[list[Candidate], list[Excluded]]:
    kept, excluded = [], []
    for c in cands:
        reason = exclusion_reason(c, cfg, max_lines, mined)
        if reason:
            excluded.append(Excluded(c, reason))
        else:
            kept.append(c)
    return kept, excluded
