"""Framework presets: data files that seed the first guess of a suite.

Each `src/crucible/data/presets/<name>.yaml` describes one test runner (markers that detect it,
the paths and commands to start from). Language knowledge lives in those files, not in code;
see docs/design.md ("Presets") and CONTRIBUTING.md ("Adding a preset").

Fields (all validated here; an error names the file and the field, e.g.
`maven.yaml: markers[0].glob: ...`):

    name           must equal the file name
    markers        [{glob, contains?, weak?, aggregator?, wins_over?}]  what detects the runner
    project_files  globs that make a directory a project of this ecosystem (used by `weak`)
    evidence       globs (relative to the suite dir, searched recursively) of which at least
                   one file must exist, e.g. `**/src/test/**`
    supersedes     presets dropped when this one is detected in the same directory
    test_paths, runnable, setup, test_command, module_root, env, timeout, setup_timeout
                   the suite fields; `runnable` omitted means the built-in rule
    variants       [{when: [globs], setup?, test_command?, module_root?}] first match wins
    notes          why the command is right and what to watch for

`{suite_dir}` in `test_paths`, `runnable`, `module_root` and `test_command` is replaced at
detection time by the suite's directory relative to the repo root (`.` for the root; a
following `/` disappears there, so `{suite_dir}/src/test` becomes `src/test`).
"""

import re
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
from pathlib import Path

import yaml

from crucible.mine import config, placeholders

SUITE_DIR = "{suite_dir}"
_TOP = {"name", "markers", "project_files", "evidence", "supersedes", "test_paths", "runnable",
        "setup", "test_command", "module_root", "env", "timeout", "setup_timeout", "variants",
        "notes"}
_MARKER = {"glob", "contains", "weak", "aggregator", "wins_over"}
_VARIANT = {"when", "setup", "test_command", "module_root"}
_SUBST_FIELDS = ("test_paths", "runnable", "module_root", "test_command")
_MAX_MARKER_BYTES = 2_000_000


class PresetError(ValueError):
    """Invalid preset file(s). `errors` has one message per problem, each `<file>: <field>: ...`."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("\n".join(errors))


@dataclass(frozen=True)
class Marker:
    glob: str
    contains: str | None = None
    weak: bool = False
    aggregator: bool = False
    wins_over: tuple[str, ...] = ()

    def describe(self) -> str:
        return self.glob if self.contains is None else f"{self.glob} matching /{self.contains}/"


@dataclass(frozen=True)
class Variant:
    when: tuple[str, ...]
    overrides: dict[str, str]


@dataclass(frozen=True)
class Preset:
    name: str
    markers: tuple[Marker, ...]
    test_paths: tuple[str, ...]
    test_command: str
    notes: str
    project_files: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    supersedes: tuple[str, ...] = ()
    runnable: tuple[str, ...] | None = None
    setup: str = ""
    module_root: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    timeout: float = config.DEFAULT_TEST_TIMEOUT_S
    setup_timeout: float = config.DEFAULT_SETUP_TIMEOUT_S
    variants: tuple[Variant, ...] = ()


# --- file matching helpers (shared with detection) ---

def glob_files(directory: Path, pattern: str) -> list[Path]:
    """Files directly under `directory` matching `pattern` (may name a sub-path like
    `test/test_helper.rb`; no `**`)."""
    return sorted(p for p in directory.glob(pattern) if p.is_file())


def read_text(path: Path) -> str:
    with path.open("rb") as f:
        return f.read(_MAX_MARKER_BYTES).decode("utf-8", errors="replace")


def marker_matches(marker: Marker, directory: Path) -> bool:
    for f in glob_files(directory, marker.glob):
        if marker.contains is None:
            return True
        try:
            if re.search(marker.contains, read_text(f), re.MULTILINE):
                return True
        except OSError:
            continue
    return False


# --- validation ---

def _bool(raw: dict, key: str, where: str, errors: list[str]) -> bool:
    value = raw.get(key, False)
    if not isinstance(value, bool):
        errors.append(f"{where}.{key}: must be true or false")
        return False
    return value


def _glob_ok(value: str, where: str, errors: list[str], *, shallow: bool) -> None:
    if value.startswith("/") or ".." in value.split("/"):
        errors.append(f"{where}: must be a relative glob without '..', got {value!r}")
    elif shallow and "**" in value:
        errors.append(f"{where}: must not contain '**' (it names files in the directory itself)")


def _parse_markers(raw, f: str, errors: list[str]) -> tuple[Marker, ...]:
    if raw is None:
        errors.append(f"{f}: markers: required (a list of {{glob, contains?}})")
        return ()
    if not isinstance(raw, list) or not raw:
        errors.append(f"{f}: markers: must not be empty (a list of {{glob, contains?}})")
        return ()
    out = []
    for i, m in enumerate(raw):
        where = f"{f}: markers[{i}]"
        if not isinstance(m, dict):
            errors.append(f"{where}: must be a mapping")
            continue
        for key in sorted(set(m) - _MARKER, key=str):
            errors.append(f"{where}.{key}: unknown field")
        glob = m.get("glob")
        if not isinstance(glob, str) or not glob:
            errors.append(f"{where}.glob: required (a file glob)")
            continue
        _glob_ok(glob, f"{where}.glob", errors, shallow=True)
        contains = m.get("contains")
        if contains is not None:
            try:
                re.compile(contains)
            except (re.error, TypeError) as e:
                errors.append(f"{where}.contains: not a valid regular expression: {e}")
                contains = None
        wins = _str_list(m.get("wins_over"), f"{where}.wins_over", errors)
        out.append(Marker(glob, contains, _bool(m, "weak", where, errors),
                          _bool(m, "aggregator", where, errors), tuple(wins)))
    return tuple(out)


def _str_list(value, name: str, errors: list[str]) -> list[str]:
    """Optional list of non-empty strings; the error reads `<name>: ...`."""
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        errors.append(f"{name}: must be a list of non-empty strings")
        return []
    return value


def _check_command(command: str, name: str, has_root: bool, errors: list[str]) -> None:
    try:
        parsed = placeholders.parse(command.replace(SUITE_DIR, "x"))
    except placeholders.PlaceholderError as e:
        errors.append(f"{name}: {e}")
        return
    if any(p.source == "modules" for p in parsed.placeholders) and not has_root:
        errors.append(f"{name}: {{modules}} needs module_root")


def _str_field(raw: dict, key: str, f: str, errors: list[str], *, required: bool = False) -> str:
    value = raw.get(key)
    if value is None:
        if required:
            errors.append(f"{f}: {key}: required")
        return ""
    if not isinstance(value, str) or (required and not value.strip()):
        errors.append(f"{f}: {key}: must be a non-empty string" if required
                      else f"{f}: {key}: must be a string")
        return ""
    return value


def parse_preset(raw, filename: str) -> Preset | None:
    """Validate one preset mapping. Raises PresetError listing every problem."""
    f = filename
    errors: list[str] = []
    if not isinstance(raw, dict):
        raise PresetError([f"{f}: must be a mapping"])
    for key in sorted(set(raw) - _TOP, key=str):
        errors.append(f"{f}: {key}: unknown field")
    stem = filename.rsplit(".", 1)[0]
    name = raw.get("name")
    if name != stem:
        errors.append(f"{f}: name: must equal the file name {stem!r}, got {name!r}")
    markers = _parse_markers(raw.get("markers"), f, errors)
    lists = {k: _str_list(raw.get(k), f"{f}: {k}", errors)
             for k in ("project_files", "evidence", "supersedes")}
    for key in ("project_files", "evidence"):
        for i, g in enumerate(lists[key]):
            _glob_ok(g, f"{f}: {key}[{i}]", errors, shallow=key == "project_files")
    test_paths = _str_list(raw.get("test_paths"), f"{f}: test_paths", errors)
    if not test_paths and not any(e.startswith(f"{f}: test_paths") for e in errors):
        errors.append(f"{f}: test_paths: required (a non-empty list of globs)")
    runnable = raw.get("runnable")
    if runnable is not None:
        runnable = _str_list(runnable, f"{f}: runnable", errors)
        if not runnable and not any(e.startswith(f"{f}: runnable") for e in errors):
            errors.append(f"{f}: runnable: must not be empty (omit it for the built-in rule)")
    setup = _str_field(raw, "setup", f, errors)
    command = _str_field(raw, "test_command", f, errors, required=True)
    module_root = raw.get("module_root")
    if module_root is not None and (not isinstance(module_root, str) or not module_root):
        errors.append(f"{f}: module_root: must be a non-empty path string")
        module_root = None
    notes = _str_field(raw, "notes", f, errors, required=True).strip()
    if SUITE_DIR in setup:
        errors.append(f"{f}: setup: {SUITE_DIR} is not available here (steps run in the suite "
                      f"directory already)")
    if command:
        _check_command(command, f"{f}: test_command", module_root is not None, errors)
    timeout = _positive(raw, "timeout", config.DEFAULT_TEST_TIMEOUT_S, f, errors)
    setup_timeout = _positive(raw, "setup_timeout", config.DEFAULT_SETUP_TIMEOUT_S, f, errors)
    env = _parse_env(raw.get("env"), f, errors)
    variants = _parse_variants(raw.get("variants"), f, module_root is not None, errors)
    if errors:
        raise PresetError(errors)
    return Preset(
        name, markers, tuple(test_paths), command, notes,
        project_files=tuple(lists["project_files"]), evidence=tuple(lists["evidence"]),
        supersedes=tuple(lists["supersedes"]),
        runnable=tuple(runnable) if runnable else None, setup=setup, module_root=module_root,
        env=env, timeout=timeout, setup_timeout=setup_timeout, variants=variants)


def _positive(raw: dict, key: str, default: float, f: str, errors: list[str]) -> float:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not value > 0:
        errors.append(f"{f}: {key}: must be a positive number of seconds, got {value!r}")
        return default
    return float(value)


def _parse_env(raw, f: str, errors: list[str]) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        errors.append(f"{f}: env: must be a mapping of NAME: value")
        return {}
    out = {}
    for k, v in raw.items():
        if not isinstance(v, (str, int, float)) or isinstance(v, bool):
            errors.append(f"{f}: env.{k}: value must be a string")
        elif config._secret_like(str(k)) and len(str(v)) > 7 and not str(v).startswith("dummy"):
            errors.append(f"{f}: env.{k}: presets hold dummy values only (start with 'dummy')")
        else:
            out[str(k)] = str(v)
    return out


def _parse_variants(raw, f: str, has_root: bool, errors: list[str]) -> tuple[Variant, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        errors.append(f"{f}: variants: must be a list")
        return ()
    out = []
    for i, v in enumerate(raw):
        where = f"{f}: variants[{i}]"
        if not isinstance(v, dict):
            errors.append(f"{where}: must be a mapping")
            continue
        for key in sorted(set(v) - _VARIANT, key=str):
            errors.append(f"{where}.{key}: unknown field")
        when = _str_list(v.get("when"), f"{where}.when", errors)
        if not when and not any(e.startswith(f"{where}.when") for e in errors):
            errors.append(f"{where}.when: required (file globs; the variant applies if any exists)")
        for g in when:
            _glob_ok(g, f"{where}.when", errors, shallow=True)
        overrides = {k: v[k] for k in ("setup", "test_command", "module_root") if k in v}
        if not overrides:
            errors.append(f"{where}: must override at least one of setup, test_command, module_root")
        for k, val in overrides.items():
            if not isinstance(val, str) or (k != "setup" and not val):
                errors.append(f"{where}.{k}: must be a string")
            elif k == "test_command":
                _check_command(val, f"{where}.test_command", has_root or "module_root" in overrides,
                               errors)
            elif k == "setup" and SUITE_DIR in val:
                errors.append(f"{where}.setup: {SUITE_DIR} is not available here")
        out.append(Variant(tuple(when), overrides))
    return tuple(out)


# --- loading ---

def load_dir(directory) -> dict[str, Preset]:
    """Load every `*.yaml` preset in `directory` (a Path or importlib.resources Traversable).
    Raises PresetError with the problems of all files."""
    errors: list[str] = []
    loaded: dict[str, Preset] = {}
    for entry in sorted(directory.iterdir(), key=lambda e: e.name):
        if not entry.name.endswith(".yaml"):
            continue
        try:
            raw = yaml.safe_load(entry.read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
            errors.append(f"{entry.name}: not valid YAML: {e}")
            continue
        except OSError as e:
            errors.append(f"{entry.name}: cannot read: {e}")
            continue
        try:
            preset = parse_preset(raw, entry.name)
        except PresetError as e:
            errors.extend(e.errors)
            continue
        loaded[preset.name] = preset
    for p in loaded.values():
        for other in p.supersedes:
            if other not in loaded:
                errors.append(f"{p.name}.yaml: supersedes: unknown preset {other!r}")
        for m in p.markers:
            for other in m.wins_over:
                if other not in loaded:
                    errors.append(f"{p.name}.yaml: markers.wins_over: unknown preset {other!r}")
    if errors:
        raise PresetError(errors)
    return loaded


@lru_cache(maxsize=1)
def shipped() -> dict[str, Preset]:
    """The presets that ship with Crucible (`crucible/data/presets/`), found through
    importlib.resources so they work from a wheel or an installed plugin."""
    return load_dir(resources.files("crucible").joinpath("data", "presets"))


# --- instantiation ---

def substitute(text: str, suite_dir: str, *, escape: bool = False) -> str:
    """Replace `{suite_dir}`: `.` for the repo root (and `{suite_dir}/` vanishes there). With
    `escape`, the directory is made safe inside a placeholder argument (`\\`, `|`, `}`)."""
    d = suite_dir
    if escape:
        d = d.replace("\\", "\\\\").replace("|", "\\|").replace("}", "\\}")
    if suite_dir == ".":
        text = text.replace(SUITE_DIR + "/", "")
    return text.replace(SUITE_DIR, d)


def choose_variant(preset: Preset, directory: Path) -> Variant | None:
    for v in preset.variants:
        if any(glob_files(directory, g) for g in v.when):
            return v
    return None


def build_fields(preset: Preset, suite_dir: str, directory: Path) -> dict:
    """The `Suite` fields (without name/cwd) for `preset` in `directory` (`suite_dir` is its
    repo-relative path): the first matching variant applied, `{suite_dir}` substituted."""
    setup, command, module_root = preset.setup, preset.test_command, preset.module_root
    variant = choose_variant(preset, directory)
    if variant:
        setup = variant.overrides.get("setup", setup)
        command = variant.overrides.get("test_command", command)
        module_root = variant.overrides.get("module_root", module_root)
    return {
        "test_paths": [substitute(p, suite_dir) for p in preset.test_paths],
        "test_command": substitute(command, suite_dir, escape=True),
        "setup": setup,
        "module_root": substitute(module_root, suite_dir) if module_root else None,
        "env": dict(preset.env),
        "runnable": [substitute(p, suite_dir) for p in preset.runnable] if preset.runnable else None,
        "timeout": preset.timeout,
        "setup_timeout": preset.setup_timeout,
    }
