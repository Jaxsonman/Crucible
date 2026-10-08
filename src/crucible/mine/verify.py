"""Fail-before / pass-after verification of mining candidates.

Runs the repo's own tests in two throwaway exports (base with the PR's test files overlaid,
then merge). Never calls `claude`; never touches the user's checkout, index or refs.
"""

import hashlib
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from crucible import executor, paths, workspace
from crucible.mine import placeholders
from crucible.mine.candidates import Candidate, _git, owning_suite
from crucible.mine.config import DEFAULT_TEST_TIMEOUT_S, Suite, default_is_runnable

DEFAULT_TIMEOUT_S = DEFAULT_TEST_TIMEOUT_S
SCRIPTS_DIRNAME = "scripts"

# Outcomes that depend on the machine or the time limit, not on the candidate: never cached
# (the logs are still written), so a rerun with a longer timeout or a fixed setup really runs.
TRANSIENT_REASONS = frozenset({"timeout", "setup failed at base", "setup failed at merge"})


def is_transient(reason: str) -> bool:
    """`timeout` or `setup failed at <side>`, with or without a ` (<suite>)` suffix."""
    return reason in TRANSIENT_REASONS or any(
        reason.startswith(r + " (") for r in TRANSIENT_REASONS)

is_runnable_test = default_is_runnable  # the rule for a suite without `runnable` globs


def runnable_files(c: Candidate, suites: "Suite | list[Suite] | None" = None) -> list[str]:
    """The candidate's non-deleted runnable test files: what the test command is given. With
    `suites`, each file follows the rule of the suite owning it (its `runnable` globs, else the
    built-in rule); without, the built-in rule."""
    live = [f.path for f in c.test_files if f.status != "deleted"]
    if suites is None:
        return [p for p in live if default_is_runnable(p)]
    return [f for _, files in suite_plan_for(as_suites(suites), live, only_runnable=True)
            for f in files]


def as_suites(suites: "Suite | list[Suite]") -> list[Suite]:
    return [suites] if isinstance(suites, Suite) else list(suites)


def involved_suites(c: Candidate, suites: "Suite | list[Suite]") -> list[Suite]:
    """The suites (config order) that own `c`'s test files; all of `suites` if `c` names none."""
    all_s = as_suites(suites)
    mine = [s for s in all_s if s.name in c.suites]
    return mine or all_s


def scripts_dir() -> Path:
    """Wrapper scripts for test commands live in `<home>/scripts/` (`.crucible/scripts/`)."""
    return paths.home() / SCRIPTS_DIRNAME


class ScriptsLinkError(ValueError):
    """A symlink in the scripts dir points outside the Crucible home (or at nothing)."""


def scripts_files() -> list[tuple[str, Path]]:
    """(relative posix name, resolved file) for every file under the scripts dir, symlinks
    followed (linked directories are walked). A link whose target lies outside the Crucible
    home, or is missing, raises `ScriptsLinkError`."""
    root = scripts_dir()
    if not root.is_dir():
        return []
    home = paths.home().resolve()
    out: list[tuple[str, Path]] = []

    def walk(d: Path, rel: str, seen: frozenset) -> None:
        for entry in sorted(d.iterdir(), key=lambda p: p.name):
            name = f"{rel}{entry.name}"
            target = entry.resolve()
            if entry.is_symlink():
                if not target.exists():
                    raise ScriptsLinkError(f"scripts/{name} is a broken symlink")
                if not target.is_relative_to(home):
                    raise ScriptsLinkError(
                        f"scripts/{name} links to {target}, outside the Crucible home "
                        f"({home}); copy the file into the scripts dir or link inside the home")
            if target.is_dir():
                if target not in seen:  # a link loop is walked once
                    walk(entry, name + "/", seen | {target})
            else:
                out.append((name, target))

    walk(root, "", frozenset({root.resolve()}))
    return out


def scripts_digest() -> str:
    """Digest of the scripts dir (relative names, exec bits, resolved contents, so editing a
    link target changes it); "" when there are none. Raises `ScriptsLinkError`."""
    files = scripts_files()
    if not files:
        return ""
    h = hashlib.sha256()
    for name, f in files:
        h.update(name.encode() + b"\0")
        h.update((b"x" if os.access(f, os.X_OK) else b"-") + f.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def suites_hash(suites: list[Suite]) -> str:
    """Hash of every involved suite's settings. One suite: that suite's own `settings_hash()`
    (so single-suite cache entries stay valid). When the home has wrapper scripts their digest
    is mixed in, so editing a script invalidates the cache."""
    if len(suites) == 1:
        base = suites[0].settings_hash()
    else:
        base = hashlib.sha256(json.dumps([s.settings_hash() for s in suites]).encode()).hexdigest()
    digest = scripts_digest()
    return hashlib.sha256((base + digest).encode()).hexdigest() if digest else base


def suite_plan(c: Candidate, suites: list[Suite]) -> list[tuple[Suite, list[str]]]:
    """(suite, its runnable files) for every suite in order that has some."""
    live = [f.path for f in c.test_files if f.status != "deleted"]
    return suite_plan_for(suites, live, only_runnable=True)


def suite_plan_for(suites: list[Suite], files: list[str], *, only_runnable: bool = False,
                   ) -> list[tuple[Suite, list[str]]]:
    """Split `files` by owner: each belongs to the first suite whose `test_paths` match it.
    With `only_runnable`, a suite keeps just the files its own runnable rule accepts."""
    plan = []
    for s in suites:
        own = [f for f in files if owning_suite(f, suites) == s.name
               and (not only_runnable or s.is_runnable(f))]
        if own:
            plan.append((s, own))
    return plan


@dataclass
class VerifyResult:
    id: str
    merge: str
    outcome: str  # "verified" | "rejected"
    reason: str = ""
    cached: bool = False
    suite: str = ""  # suite names joined by `+`
    timings: dict = field(default_factory=dict)
    logs: dict = field(default_factory=dict)
    kept: list[str] = field(default_factory=list)  # workspace dirs kept by --keep
    command: str = ""

    @property
    def verified(self) -> bool:
        return self.outcome == "verified"

    def label(self) -> str:
        return "yes" if self.verified else f"no: {self.reason}"

    def to_dict(self) -> dict:
        return {"id": self.id, "merge": self.merge, "outcome": self.outcome,
                "reason": self.reason, "cached": self.cached, "suite": self.suite,
                "suites": self.suite.split("+") if self.suite else [],
                "timings": self.timings, "logs": self.logs, "kept": self.kept,
                "command": self.command}


# --- cache ---

def cache_dir() -> Path:
    return paths.home() / "mine" / "verify"


def _cache_file(merge: str) -> Path:
    return cache_dir() / f"{merge}.json"


def cached_result(c: Candidate, suites: "Suite | list[Suite]") -> VerifyResult | None:
    """The cached result for `c`, only if it was produced with the involved suites' current
    settings."""
    involved = involved_suites(c, suites)
    try:
        data = json.loads(_cache_file(c.merge).read_text(encoding="utf-8"))
        if data["suite_hash"] != suites_hash(involved):
            return None
        return VerifyResult(
            id=c.id, merge=c.merge, outcome=data["outcome"], reason=data.get("reason", ""),
            cached=True, suite=data.get("suite", ""), timings=data.get("timings", {}),
            logs=data.get("logs", {}), command=data.get("command", ""),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def verified_label(c: Candidate, suites: list[Suite]) -> str | None:
    """`yes`, `no: <reason>`, or None (not verified / stale cache) for the listing."""
    if not c.suites or any(n not in {s.name for s in suites} for n in c.suites):
        return None
    r = cached_result(c, suites)
    return r.label() if r else None


def _write_cache(c: Candidate, suites: list[Suite], r: VerifyResult) -> None:
    d = cache_dir()
    d.mkdir(parents=True, exist_ok=True)
    _cache_file(c.merge).write_text(json.dumps({
        "id": c.id, "base": c.base, "merge": c.merge, "suite": "+".join(s.name for s in suites),
        "suites": [s.name for s in suites],
        "suite_hash": suites_hash(suites), "outcome": r.outcome, "reason": r.reason,
        "command": r.command, "timings": r.timings, "logs": r.logs,
        "verified_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }, indent=2) + "\n", encoding="utf-8")


# --- command building ---

def build_command(suite: Suite, test_files: list[str]) -> str:
    """Fill the suite's `test_command` placeholders (see `placeholders`) from the repo-relative
    `test_files`. Raises `placeholders.OutsideRoot` for a `module` filter / `{modules}` meeting
    a file outside its root."""
    return placeholders.render(placeholders.parse(suite.test_command), test_files, suite.cwd,
                               suite.module_root)


def step_env(suite: Suite, test_files: list[str]) -> dict[str, str]:
    """Environment of every setup/test step: the process env, the suite `env`, then Crucible's
    own variables (they win): the suite's runnable files for wrapper scripts, and the scripts
    dir when the home has one."""
    env = {**os.environ, **suite.env,
           "CRUCIBLE_TEST_FILES": "\n".join(test_files),
           "CRUCIBLE_TEST_FILES_CWD": "\n".join(os.path.relpath(f, suite.cwd) for f in test_files)}
    if scripts_digest():
        env["CRUCIBLE_SCRIPTS"] = str(scripts_dir().resolve())
    return env


# --- running ---

def _run_step(command: str, cwd: Path, env: dict, timeout: float, log: Path) -> tuple[int | None, float]:
    """Run `bash -c command` in its own process group, output to `log`. Returns (exit code,
    seconds); the code is None on timeout (group killed)."""
    start = time.monotonic()
    with open(log, "wb") as out:
        out.write(f"$ {command}\n(cwd {cwd})\n".encode())
        out.flush()
        try:
            proc = subprocess.Popen(
                ["bash", "-c", command], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                stdout=out, stderr=subprocess.STDOUT, start_new_session=True,
            )
        except OSError as e:
            out.write(f"could not start: {e}\n".encode())
            return 127, time.monotonic() - start
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            executor._terminate_group(proc)
            out.write(f"\ntimed out after {timeout}s and was killed\n".encode())
            return None, time.monotonic() - start
        except BaseException:
            executor._terminate_group(proc)
            raise
    executor._killpg(proc.pid, signal.SIGKILL)  # stray grandchildren
    return code, time.monotonic() - start


def blob_modes(repo: Path, merge: str, files: list[str]) -> dict[str, str]:
    """Git file modes (`100644`, `100755`, `120000`) of `files` at `merge`."""
    if not files:
        return {}
    out = _git(repo, "ls-tree", "-z", merge, "--", *files).decode("utf-8", "surrogateescape")
    modes = {}
    for rec in filter(None, out.split("\0")):
        meta, _, path = rec.partition("\t")
        modes[path] = meta.split()[0]
    return modes


class UnsafePathError(Exception):
    """A write would go through a symlinked directory or leave its root."""


UNSAFE_PATH = "unsafe path (symlink)"


def check_safe_target(target: Path, root: Path) -> None:
    """Raise UnsafePathError unless every existing directory between `root` and `target`
    is a real directory and the resolved parent stays inside `root`."""
    try:
        rel = target.relative_to(root)
    except ValueError:
        raise UnsafePathError(f"{target} is outside {root}") from None
    cur = root
    for part in rel.parts[:-1]:
        cur = cur / part
        if cur.is_symlink():
            raise UnsafePathError(f"{cur} is a symlink")
        if not cur.exists():
            break
    real_root = root.resolve()
    if not target.parent.resolve().is_relative_to(real_root):
        raise UnsafePathError(f"{target} resolves outside {root}")


def write_blob(repo: Path, merge: str, path: str, mode: str, target: Path, root: Path) -> None:
    """Write `merge:path` to `target` (under `root`), keeping the git mode: exec bit for
    100755, a real symlink for 120000. Raises UnsafePathError rather than write through a
    symlinked parent directory."""
    data = _git(repo, "cat-file", "blob", f"{merge}:{path}")
    check_safe_target(target, root)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink() or target.exists():
        target.unlink()
    if mode == "120000":
        os.symlink(os.fsdecode(data), target)
        return
    target.write_bytes(data)
    target.chmod(0o755 if mode == "100755" else 0o644)


def _overlay(repo: Path, c: Candidate, dest: Path) -> None:
    """Put the PR's test files at their merge versions into the base export."""
    modes = blob_modes(repo, c.merge, [f.path for f in c.test_files if f.status != "deleted"])
    for f in c.test_files:
        target = dest / f.path
        check_safe_target(target, dest)
        if f.status == "deleted":
            target.unlink(missing_ok=True)
            continue
        write_blob(repo, c.merge, f.path, modes.get(f.path, "100644"), target, dest)


def _make_dir(label: str, c: Candidate) -> Path:
    root = paths.work_dir() / "mine"
    root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f"{c.merge[:8]}-{label}-", dir=root))


def verify_candidate(
    repo: Path, c: Candidate, suites: "Suite | list[Suite]", *, timeout: float | None = None,
    keep: bool = False, force: bool = False,
) -> VerifyResult:
    """Verify one candidate, or return its cached result. `suites` are the suites owning its
    test files (a single Suite is accepted). Per side the tree is exported and the tests are
    overlaid once, then each suite in order runs setup and its test command with only its own
    runnable files. Base is verified if at least one suite fails; merge needs every suite to
    pass. `timeout` (seconds) overrides every suite's test-step `timeout`; the setup step always
    uses the suite's `setup_timeout`. Raises MineGitError / WorkspaceError for git problems and
    ScriptsLinkError for a scripts symlink out of the home."""
    scripts_files()  # refuse symlinks out of the home before anything runs (or is cached)
    involved = involved_suites(c, suites)
    multi = len(involved) > 1
    if not force:
        hit = cached_result(c, involved)
        if hit:
            return hit
    log_dir = cache_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    res = VerifyResult(id=c.id, merge=c.merge, outcome="rejected",
                       suite="+".join(s.name for s in involved))

    def finish(reason: str = "") -> VerifyResult:
        if reason:
            res.reason = reason
        else:
            res.outcome = "verified"
        if not is_transient(reason):
            _write_cache(c, involved, res)
        return res

    def tag(reason: str, suite: Suite) -> str:
        return f"{reason} ({suite.name})" if multi else reason

    plan = suite_plan(c, involved)
    if not plan:
        return finish("no runnable tests")
    for suite, files in plan:
        if suite.module_root is not None and any(
                os.path.relpath(f, suite.module_root).startswith("..") for f in files):
            return finish(tag("test outside module_root", suite))
    commands = {}
    for suite, files in plan:
        try:
            commands[suite.name] = build_command(suite, files)
        except placeholders.OutsideRoot:
            return finish(tag("test outside module_root", suite))
    res.command = (next(iter(commands.values())) if not multi
                   else "\n".join(f"[{n}] {cmd}" for n, cmd in commands.items()))

    plan_files = {s.name: files for s, files in plan}

    def step(side: str, suite: Suite, name: str, command: str, cwd: Path) -> int | None:
        key = f"{side}-{suite.name}-{name}" if multi else f"{side}-{name}"
        log = log_dir / f"{c.merge}.{key}.log"
        res.logs[key] = str(log)
        limit = suite.setup_timeout if name == "setup" else (
            suite.timeout if timeout is None else timeout)
        code, secs = _run_step(command, cwd, step_env(suite, plan_files[suite.name]), limit, log)
        res.timings[key] = round(secs, 3)
        return code

    dirs: list[Path] = []
    try:
        for side, ref in (("base", c.base), ("merge", c.merge)):
            ws = _make_dir(side, c)
            dirs.append(ws)
            workspace.export_tree(repo, ref, ws)
            if side == "base":
                try:
                    _overlay(repo, c, ws)
                except UnsafePathError:
                    return finish(UNSAFE_PATH)
            failed: list[str] = []
            for suite, _ in plan:
                cwd = ws / suite.cwd
                if suite.setup:
                    code = step(side, suite, "setup", suite.setup, cwd)
                    if code is None:
                        return finish("timeout")
                    if code != 0:
                        return finish(tag(f"setup failed at {side}", suite))
                code = step(side, suite, "test", commands[suite.name], cwd)
                if code is None:
                    return finish("timeout")
                if code != 0:
                    failed.append(suite.name)
                    if side == "merge":
                        return finish(tag("fails at merge", suite))
            if side == "base" and not failed:
                return finish("passes at base")
        return finish()
    finally:
        if keep:
            res.kept = [str(d) for d in dirs]
        else:
            for d in dirs:
                shutil.rmtree(d, ignore_errors=True)
