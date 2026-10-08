"""`crucible mine calibrate`: prove that each suite's config really runs the repo's tests.

Per suite: (1) `setup` at HEAD in a fresh export; (2) verify up to N newest candidates owned by
the suite (the fail-before / pass-after check is the language-agnostic proof that the command runs
the selected tests); (3) a status and hints from exit codes and log tails (`diagnostics`).
Results are written to `<home>/mine/calibration.json`. Never calls `claude`.
"""

import hashlib
import json
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from crucible import paths, workspace
from crucible.mine import diagnostics
from crucible.mine import verify as vf
from crucible.mine.candidates import Candidate, MineGitError
from crucible.mine.config import Suite

DEFAULT_CANDIDATES = 3
FILE_NAME = "calibration.json"
OK_STATUSES = ("ok", "no-candidates")
STATUSES = ("ok", "setup-failed", "no-tests-ran", "not-selective", "failing", "timeout",
            "no-candidates", "mixed")


def calibration_dir() -> Path:
    return paths.home() / "mine" / "calibrate"


def calibration_file() -> Path:
    return paths.home() / "mine" / FILE_NAME


def config_hash(suites: list[Suite]) -> str:
    """Hash of every suite's settings plus the scripts digest: what a calibration is valid for."""
    base = json.dumps([s.settings_hash() for s in suites])
    return hashlib.sha256((base + vf.scripts_digest()).encode()).hexdigest()


@dataclass
class SetupCheck:
    ok: bool
    seconds: float = 0.0
    log: str = ""
    tail: str = ""
    timed_out: bool = False

    def to_dict(self) -> dict:
        return {"ok": self.ok, "seconds": self.seconds, "log": self.log, "tail": self.tail}


@dataclass
class Sample:
    id: str
    outcome: str  # "verified" | "rejected" | "error"
    reason: str = ""
    cached: bool = False
    logs: dict = field(default_factory=dict)
    tail: str = ""
    klass: str = ""  # internal: the status this sample points at ("" when verified)

    def to_dict(self) -> dict:
        return {"id": self.id, "outcome": self.outcome, "reason": self.reason,
                "cached": self.cached, "logs": self.logs, "tail": self.tail}


@dataclass
class SuiteReport:
    name: str
    status: str
    setup: SetupCheck
    samples: list[Sample] = field(default_factory=list)
    hints: list[diagnostics.Hint] = field(default_factory=list)
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status in OK_STATUSES

    def to_dict(self) -> dict:
        return {"name": self.name, "status": self.status, "detail": self.detail,
                "setup": self.setup.to_dict(), "samples": [s.to_dict() for s in self.samples],
                "hints": [h.to_dict() for h in self.hints]}


@dataclass
class Report:
    config_hash: str
    suites: list[SuiteReport]

    @property
    def ok(self) -> bool:
        return all(s.ok for s in self.suites)

    def to_dict(self) -> dict:
        return {"config_hash": self.config_hash, "ok": self.ok,
                "suites": [s.to_dict() for s in self.suites]}


# --- setup at HEAD ---

def check_setup(repo: Path, suite: Suite, ref: str = "HEAD") -> SetupCheck:
    """Run the suite's `setup` in a fresh export of `ref`. No setup configured: ok, 0 s."""
    if not suite.setup:
        return SetupCheck(ok=True)
    log_dir = calibration_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log = log_dir / f"{suite.name}.setup.log"
    root = paths.work_dir() / "mine"
    root.mkdir(parents=True, exist_ok=True)
    ws = Path(tempfile.mkdtemp(prefix="calibrate-", dir=root))
    try:
        workspace.export_tree(repo, ref, ws)
        code, secs = vf._run_step(suite.setup, ws / suite.cwd, vf.step_env(suite, []),
                                  suite.setup_timeout, log)
    finally:
        shutil.rmtree(ws, ignore_errors=True)
    text = log.read_text(encoding="utf-8", errors="replace")
    return SetupCheck(ok=code == 0, seconds=round(secs, 3), log=str(log),
                      tail=diagnostics.tail_lines(text), timed_out=code is None)


# --- samples ---

def owned_candidates(suite: Suite, suites: list[Suite], candidates: list[Candidate],
                     ) -> list[Candidate]:
    """Candidates (given order, newest first) owned by `suite` that carry a runnable file for it."""
    out = []
    for c in candidates:
        if suite.name not in c.suites:
            continue
        if any(s.name == suite.name for s, _ in vf.suite_plan(c, vf.involved_suites(c, suites))):
            out.append(c)
    return out


def _last_log(res: vf.VerifyResult) -> tuple[str, str]:
    """(key, path) of the last step that ran, or ("", "")."""
    if not res.logs:
        return "", ""
    key = list(res.logs)[-1]
    return key, res.logs[key]


def _read(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _sample_from(res: vf.VerifyResult, table: diagnostics.Table,
                 ) -> tuple[Sample, list[diagnostics.Hint]]:
    key, path = _last_log(res)
    text = _read(path) if path else ""
    body, code = diagnostics.split_log(text)
    tail = diagnostics.tail_lines(text)
    if res.verified:
        return Sample(res.id, "verified", cached=res.cached, logs=res.logs, tail=tail), []
    stage = "setup" if key.rsplit("-", 1)[-1] == "setup" else "test"
    hints = diagnostics.diagnose(body, code, stage, table) if text else []
    no_tests = any(h.status == "no-tests-ran" for h in hints)
    reason = res.reason
    if reason == "timeout":
        klass = "timeout"
    elif reason.startswith("setup failed"):
        klass = "setup-failed"
    elif reason == "passes at base":
        klass = "no-tests-ran" if no_tests else "not-selective"
    elif reason.startswith("fails at merge"):
        klass = "no-tests-ran" if no_tests else "failing"
    else:
        klass = "failing"
    return Sample(res.id, "rejected", reason, res.cached, res.logs, tail, klass), hints


def _status(samples: list[Sample]) -> tuple[str, str]:
    """(status, detail) from the samples."""
    if not samples:
        return "no-candidates", "no candidate to sample"
    if any(s.outcome == "verified" for s in samples):
        n = sum(s.outcome == "verified" for s in samples)
        return "ok", f"{n} of {len(samples)} sampled candidate(s) verified"
    classes = [s.klass for s in samples]
    kinds = list(dict.fromkeys(classes))
    if len(kinds) == 1:
        return kinds[0], f"{len(samples)} sampled candidate(s): {samples[0].reason}"
    counts = ", ".join(f"{classes.count(k)} {k}" for k in kinds)
    return "mixed", counts


def calibrate_suite(
    repo: Path, suite: Suite, suites: list[Suite], candidates: list[Candidate], *, n: int,
    timeout: float | None, force: bool, memo: dict, table: diagnostics.Table,
) -> SuiteReport:
    """Calibrate one suite (see module docstring). `memo` shares verify results between suites
    within one run (transient results are never cached on disk, so a candidate shared by two
    suites would otherwise run twice)."""
    setup = check_setup(repo, suite)
    if not setup.ok:
        status = "timeout" if setup.timed_out else "setup-failed"
        body, code = diagnostics.split_log(_read(setup.log))
        hints = diagnostics.diagnose(body, code, "setup", table)
        hints += _status_hints(status, table, hints)
        return SuiteReport(suite.name, status, setup, [], hints,
                           f"setup {'timed out' if setup.timed_out else 'failed'} at HEAD")
    samples: list[Sample] = []
    hints: list[diagnostics.Hint] = []
    for c in owned_candidates(suite, suites, candidates)[:n]:
        res = memo.get(c.merge)
        if res is None:
            try:
                res = vf.verify_candidate(repo, c, vf.involved_suites(c, suites),
                                          timeout=timeout, force=force)
            except (MineGitError, workspace.WorkspaceError) as e:
                samples.append(Sample(c.id, "error", str(e), klass="failing"))
                continue
            memo[c.merge] = res
        sample, found = _sample_from(res, table)
        samples.append(sample)
        hints += found
    status, detail = _status(samples)
    if status != "ok":
        hints += _status_hints(status, table, hints)
    return SuiteReport(suite.name, status, setup, samples, _dedupe(hints), detail)


def _status_hints(status: str, table: diagnostics.Table, have: list[diagnostics.Hint],
                  ) -> list[diagnostics.Hint]:
    text = table.status_hints.get(status)
    if not text or any(h.hint == text for h in have):
        return []
    return [diagnostics.Hint(f"status-{status}", "status", text)]


def _dedupe(hints: list[diagnostics.Hint]) -> list[diagnostics.Hint]:
    seen, out = set(), []
    for h in hints:
        if h.hint not in seen:
            seen.add(h.hint)
            out.append(h)
    return out


def calibrate(
    repo: Path, suites: list[Suite], candidates: list[Candidate], *, names: list[str] | None = None,
    n: int = DEFAULT_CANDIDATES, timeout: float | None = None, force: bool = False,
) -> Report:
    """Calibrate the suites in `names` (default all), in config order. Raises
    `vf.ScriptsLinkError`, `workspace.WorkspaceError` (HEAD cannot be exported)."""
    vf.scripts_files()  # refuse symlinks out of the home before anything runs
    table = diagnostics.load_table()
    memo: dict = {}
    reports = [calibrate_suite(repo, s, suites, candidates, n=n, timeout=timeout, force=force,
                               memo=memo, table=table)
               for s in suites if not names or s.name in names]
    return Report(config_hash(suites), reports)


# --- calibration.json ---

def save(report: Report, suites: list[Suite]) -> None:
    """Record the report. A partial run (`--suite`) merges into an existing file of the same
    config hash; a different hash starts over."""
    f = calibration_file()
    entries: dict = {}
    try:
        old = json.loads(f.read_text(encoding="utf-8"))
        if old.get("config_hash") == report.config_hash:
            entries = old.get("suites", {})
    except (OSError, ValueError, AttributeError):
        pass
    for s in report.suites:
        entries[s.name] = {**s.to_dict(), "calibrated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"config_hash": report.config_hash, "suites": entries}, indent=2)
                 + "\n", encoding="utf-8")


def note(suites: list[Suite]) -> str | None:
    """One line for the listing / `--verify` when the current config is uncalibrated or its last
    calibration failed; None when it is fine (or there are no suites)."""
    if not suites:
        return None
    try:
        h = config_hash(suites)
        data = json.loads(calibration_file().read_text(encoding="utf-8"))
    except (OSError, ValueError, vf.ScriptsLinkError):
        data = None
    entries = data.get("suites", {}) if isinstance(data, dict) and data.get("config_hash") == h \
        else None
    if entries is None or any(s.name not in entries for s in suites):
        return "warning: mine.yaml is not calibrated; run `crucible mine calibrate` first"
    bad = [f"{s.name} ({entries[s.name].get('status')})" for s in suites
           if entries[s.name].get("status") not in OK_STATUSES]
    if bad:
        return ("warning: last calibration was not ok: " + ", ".join(bad)
                + "; run `crucible mine calibrate`")
    return None
