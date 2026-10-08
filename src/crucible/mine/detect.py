"""Preset-driven suite detection: scan the repo for each preset's markers.

Rules (docs/design.md, "Detection"):

- the repo root and up to three levels below it are scanned, skipping dot directories, vendored
  and build output (`node_modules`, `vendor`, `target`, `build`, ...), virtualenvs and fixture
  directories;
- a preset matches a directory when any of its markers matches (a file glob in that directory,
  optionally with a regex over its content, optionally also needing test files below it);
- a `fallback` preset (generic whole-suite runners such as `npm test`) counts only in a
  directory where no other preset matched;
- looking for test files ("evidence") skips only dependency and VCS directories (`EVIDENCE_SKIP`),
  not every name the project scan skips: `build`, `out`, `deps` or `bin` can hold real tests;
- one suite per (directory, preset); several presets may match one directory (a Django backend
  and its Jest tooling), unless one supersedes the other;
- a preset with `evidence` only matches a directory with test sources below it;
- an `aggregator` marker (a Maven parent pom with `<modules>`, a Gradle settings file with
  `include`, a Cargo `[workspace]`, a `.sln`, an Elixir umbrella) makes the directory build
  everything below it, so same-preset matches below it are not separate suites;
- suites are listed deepest directory first so a nested suite owns its files before a
  broader glob of an enclosing suite can claim them.
"""

import os
import re
import textwrap
from dataclasses import dataclass
from pathlib import Path

from crucible.mine import config, jdk, presets
from crucible.mine.presets import Preset

MAX_DEPTH = 3
SKIP_DIRS = {
    # dependencies and build output
    "node_modules", "vendor", "target", "build", "dist", "out", "bin", "obj", "deps", "_build",
    "site-packages", "__pycache__", "venv", "env", "coverage", "Pods",
    # sample projects used by tests of other tools
    "testdata", "fixtures", "__fixtures__",
}
# Evidence for "this project has tests" skips only dependency trees, VCS data and package caches:
# `target` (cargo packages copy tests/ under target/package; Maven puts reports there) and
# virtualenvs hold copies that are not the project's tests. Generic names (`build`, `out`,
# `bin`, `dist`, `env`, `deps`, `obj`, `testdata`) are scanned: they can hold real test sources
# (the cost is a false positive from a generated copy, which calibration then exposes).
EVIDENCE_SKIP = {"node_modules", "vendor", "target", "site-packages", "__pycache__", "venv",
                 "Pods", "_build"}
_MAX_EVIDENCE_FILES = 200_000
_UNSAFE = re.compile(r"[\x00-\x1f\x7f*?]")


def _scannable(directory: Path) -> bool:
    name = directory.name
    if name.startswith(".") or name in SKIP_DIRS or name.endswith(".egg-info"):
        return False
    if _UNSAFE.search(name):  # would break globs, YAML comments or log lines
        return False
    return not (directory / "pyvenv.cfg").is_file()


def _scan_dirs(root: Path) -> list[Path]:
    """The root, then its sub-directories to MAX_DEPTH, each level sorted by name."""
    out, level = [root], [root]
    for _ in range(MAX_DEPTH):
        nxt = []
        for d in level:
            try:
                subs = sorted(e for e in d.iterdir() if e.is_dir() and _scannable(e))
            except OSError:
                continue
            nxt.extend(subs)
        out.extend(nxt)
        level = nxt
    return out


@dataclass
class _Match:
    directory: Path
    preset: Preset
    marker: str  # human description of what matched
    strong: bool  # matched through a non-weak marker
    aggregator: bool
    wins_over: frozenset[str]


@dataclass(frozen=True)
class Detection:
    """A detected suite and where it came from."""
    suite: config.Suite
    preset: str
    marker: str
    notes: str
    extra: tuple[str, ...] = ()  # more comment text (e.g. the Java version the build needs)

    def comment(self) -> list[str]:
        where = "the repo root" if self.suite.cwd == "." else f"`{_clean(self.suite.cwd)}/`"
        lines = [
            f"Detected in {where} by preset `{self.preset}` (marker: {_clean(self.marker)}).",
            ("This is a guess, not proof: run `crucible mine calibrate` to check it against real "
             "candidates, and fix it from what the failures say."),
        ]
        if self.notes:
            lines += textwrap.wrap(f"Note: {self.notes}", width=94)
        lines += self.extra
        return [ln for part in lines for ln in textwrap.wrap(part, width=94) or [""]]


def _clean(text: str) -> str:
    return re.sub(r"[^\x20-\x7e]", "?", text)


def _rel(root: Path, d: Path) -> str:
    return "." if d == root else d.relative_to(root).as_posix()


def _match_dir(d: Path, preset: Preset) -> _Match | None:
    hits = [m for m in preset.markers if presets.marker_matches(m, d)
            and (not m.evidence or _has_evidence(d, m.evidence))]
    if not hits:
        return None
    strong = [m for m in hits if not m.weak]
    shown = (strong or hits)[0]
    return _Match(
        d, preset, shown.describe(), bool(strong), any(m.aggregator for m in hits),
        frozenset(w for m in hits for w in m.wins_over))


def _has_project_file(d: Path, preset: Preset) -> bool:
    return any(presets.glob_files(d, g) for g in preset.project_files)


def _retarget_weak(matches: list[_Match], root: Path) -> list[_Match]:
    """A directory matched only by weak markers (a `conftest.py`-only `tests/`) is not its own
    project when an enclosing directory holds the ecosystem's project files: the enclosing
    directory gets the suite instead."""
    have = {(m.directory, m.preset.name) for m in matches}
    out = []
    for m in matches:
        if m.strong or not m.preset.project_files or _has_project_file(m.directory, m.preset):
            out.append(m)
            continue
        parents = [p for p in m.directory.parents if p == root or root in p.parents]
        target = next((p for p in parents if _has_project_file(p, m.preset)), None)
        if target is None:
            out.append(m)
        elif (target, m.preset.name) not in have:
            have.add((target, m.preset.name))
            out.append(_Match(target, m.preset, f"{m.marker} in {_rel(root, m.directory)}/", False,
                              False, frozenset()))
    return out


def _vendored(directory: Path) -> bool:
    name = directory.name
    return (name.startswith(".") or name in EVIDENCE_SKIP or name.endswith(".egg-info")
            or (directory / "pyvenv.cfg").is_file())


def _has_evidence(d: Path, globs: tuple[str, ...]) -> bool:
    seen = 0
    for base, dirs, files in os.walk(d):
        dirs[:] = sorted(x for x in dirs if not _vendored(Path(base) / x))
        for name in files:
            seen += 1
            rel = (Path(base) / name).relative_to(d).as_posix()
            if config.path_matches(rel, list(globs)):
                return True
        if seen > _MAX_EVIDENCE_FILES:
            return False
    return False


def _resolve(matches: list[_Match]) -> list[_Match]:
    """Per directory: marker-level `wins_over` first, then preset-level `supersedes`."""
    by_dir: dict[Path, list[_Match]] = {}
    for m in matches:
        by_dir.setdefault(m.directory, []).append(m)
    kept = []
    for group in by_dir.values():
        beaten = {w for m in group if m.strong for w in m.wins_over}
        group = [m for m in group if m.preset.name not in beaten]
        superseded = {s for m in group for s in m.preset.supersedes}
        kept.extend(m for m in group if m.preset.name not in superseded)
    return kept


def _drop_fallbacks(matches: list[_Match]) -> list[_Match]:
    """A fallback preset only counts in a directory where no other preset matched."""
    framework_dirs = {m.directory for m in matches if not m.preset.fallback}
    return [m for m in matches if not (m.preset.fallback and m.directory in framework_dirs)]


def _drop_aggregated(matches: list[_Match]) -> list[_Match]:
    aggregators = {(m.directory, m.preset.name) for m in matches if m.aggregator}
    return [m for m in matches
            if not any((p, m.preset.name) in aggregators for p in m.directory.parents)]


def _unique_name(base: str, preset: str, used: set[str]) -> str:
    name = re.sub(r"[^A-Za-z0-9_.-]+", "-", base).strip(".-_") or preset
    candidates = [name, f"{name}-{preset}"]
    candidates += [f"{name}-{preset}-{i}" for i in range(2, 100)]
    for c in candidates:
        c = c[:config.SUITE_NAME_MAX].rstrip(".-_")
        if c not in used:
            used.add(c)
            return c
    raise AssertionError("unreachable: no unique suite name")


def detect(repo_root: Path, catalog: dict[str, Preset] | None = None,
           jdk_lookup: jdk.JdkLookup | None = None) -> list[Detection]:
    """Detect the repo's test suites from the presets (the shipped ones by default). For Maven
    and Gradle suites the Java version the build needs is read and, when `jdk_lookup(version)`
    (default: this machine's JDKs) finds a JDK, set as `env: JAVA_HOME`."""
    root = Path(repo_root)
    catalog = presets.shipped() if catalog is None else catalog
    matches = []
    for d in _scan_dirs(root):
        for preset in catalog.values():
            m = _match_dir(d, preset)
            if m:
                matches.append(m)
    matches = _retarget_weak(matches, root)
    matches = [m for m in matches if not m.preset.evidence or _has_evidence(m.directory, m.preset.evidence)]
    matches = _drop_aggregated(_drop_fallbacks(_resolve(matches)))
    matches.sort(key=lambda m: (-len(m.directory.relative_to(root).parts), _rel(root, m.directory),
                                m.preset.name))
    used: set[str] = set()
    found = []
    for m in matches:
        rel = _rel(root, m.directory)
        name = _unique_name(m.directory.name if rel != "." else m.preset.name, m.preset.name, used)
        fields = presets.build_fields(m.preset, rel, m.directory, root)
        extra: tuple[str, ...] = ()
        if m.preset.name in ("maven", "gradle"):
            req = jdk.required_java(m.directory, root, m.preset.name)
            if req:
                home = (jdk_lookup or jdk.find_local_jdk)(req.version)
                if home and "JAVA_HOME" not in fields["env"]:
                    fields["env"] = {**fields["env"], "JAVA_HOME": home}
                extra = tuple(ln for part in jdk.describe(req, home)
                              for ln in textwrap.wrap(part, width=94))
        suite = config.Suite(name=name, cwd=rel, **fields)
        notes = " ".join([m.preset.notes] + [
            note for globs, note in m.preset.notes_when
            if any(presets.glob_files(m.directory, g) for g in globs)])
        found.append(Detection(suite, m.preset.name, m.marker, notes, extra))
    return found

