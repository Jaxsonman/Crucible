"""`.crucible/mine.yaml`: per-repo test suites for mining, plus stack detection."""

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import yaml

from crucible.mine import placeholders

CONFIG_NAME = "mine.yaml"
PLACEHOLDERS = placeholders.SOURCES
DEFAULT_TEST_TIMEOUT_S = 600.0
DEFAULT_SETUP_TIMEOUT_S = 1800.0
SECRET_WORDS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "PRIVATE", "CREDENTIAL")
SECRET_TOKENS = ("PAT",)  # whole `_`-separated words only, so `PATH` is not a secret
_URL_CREDENTIALS = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^/\s:@]*:([^/\s@]*)@")
DEFAULT_MAX_LINES = 800
DEFAULT_EXCLUDE = [
    "**/package-lock.json", "**/*.lock", "**/migrations/**", "docs/**", "*.md",
]
SUITE_NAME_MAX = 64
SUITE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_SUITE_KEYS = {"name", "test_paths", "cwd", "setup", "test_command", "module_root", "env",
               "runnable", "timeout", "setup_timeout"}
# A changed test file is *runnable* (passed to the test command) when its name matches this or
# it lives under a `__tests__` directory: the rule used when a suite has no `runnable` globs.
_RUNNABLE_NAME = re.compile(r"(?:test_.*\.py|.*_test\.py|tests\.py|.*\.(?:test|spec)\.[^.]+)\Z")


def default_is_runnable(path: str) -> bool:
    p = PurePosixPath(path)
    return bool(_RUNNABLE_NAME.match(p.name)) or "__tests__" in p.parts[:-1]
_TOP_KEYS = {"suites", "exclude_paths", "max_changed_lines"}


class MineConfigError(ValueError):
    """Invalid mine.yaml. `errors` has one message per problem, each naming its field."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("\n".join(errors))


@dataclass(frozen=True)
class Suite:
    name: str
    test_paths: list[str]
    test_command: str
    cwd: str = "."
    setup: str = ""
    module_root: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    runnable: list[str] | None = None  # None: the built-in rule (`default_is_runnable`)
    timeout: float = DEFAULT_TEST_TIMEOUT_S
    setup_timeout: float = DEFAULT_SETUP_TIMEOUT_S

    def is_runnable(self, path: str) -> bool:
        """Is this changed test file passed to the test command?"""
        if self.runnable is None:
            return default_is_runnable(path)
        return path_matches(path, self.runnable)

    def to_dict(self) -> dict:
        """Plain-data form (stable key order); optional fields appear only when set."""
        d = self.hashed_dict()
        if self.timeout != DEFAULT_TEST_TIMEOUT_S:
            d["timeout"] = int(self.timeout) if self.timeout.is_integer() else self.timeout
        if self.setup_timeout != DEFAULT_SETUP_TIMEOUT_S:
            d["setup_timeout"] = (int(self.setup_timeout) if self.setup_timeout.is_integer()
                                  else self.setup_timeout)
        return d

    def hashed_dict(self) -> dict:
        """The settings that decide a verification result. Timeouts are left out on purpose:
        a run that hit one is never cached, and a result produced within a limit stays valid
        under a longer one. Fields at their default are omitted so older caches stay valid."""
        d = {"name": self.name, "test_paths": self.test_paths, "cwd": self.cwd}
        if self.setup:
            d["setup"] = self.setup
        d["test_command"] = self.test_command
        if self.module_root is not None:
            d["module_root"] = self.module_root
        if self.env:
            d["env"] = self.env
        if self.runnable is not None:
            d["runnable"] = self.runnable
        return d

    def settings_hash(self) -> str:
        blob = json.dumps(self.hashed_dict(), sort_keys=True).encode()
        return hashlib.sha256(blob).hexdigest()


@dataclass(frozen=True)
class MineConfig:
    suites: list[Suite]
    exclude_paths: list[str]
    max_changed_lines: int


# --- path globs ---

def _glob_regex(pattern: str) -> re.Pattern:
    """`**` crosses directories (and may match none), `*` and `?` stay inside one segment."""
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def _match_one(path: str, pat: str) -> bool:
    target = path if "/" in pat else PurePosixPath(path).name
    return bool(_glob_regex(pat).match(target))


def path_matches(path: str, patterns: list[str]) -> bool:
    """True if repo-relative `path` matches any glob. A pattern with no `/` (like `*.md`)
    matches the file name at any depth. A directory pattern (trailing `/`, or no wildcard)
    also matches every file under that directory: `src/utils` and `src/utils/` are anchored at the
    repo root; a name without a `/` (`vendor`, `vendor/`) matches a directory of that name at any depth."""
    for pat in patterns:
        if _match_one(path, pat):
            return True
        base = pat.rstrip("/")
        if base and (pat.endswith("/") or not set(base) & set("*?")):
            if _match_one(path, base) or _match_one(path, base + "/**"):
                return True
            if "/" not in base and _match_one(path, f"**/{base}/**"):
                return True
    return False


# --- loading and validation ---

def _str_list(value, field_name: str, errors: list[str], *, required: bool) -> list[str]:
    if value is None:
        if required:
            errors.append(f"{field_name}: required (a list of globs)")
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        errors.append(f"{field_name}: must be a list of non-empty strings")
        return []
    if required and not value:
        errors.append(f"{field_name}: must not be empty")
    return value


def _check_relative(value: str, field_name: str, errors: list[str]) -> None:
    p = PurePosixPath(value)
    if p.is_absolute() or ".." in p.parts:
        errors.append(f"{field_name}: must be a path inside the repo, got {value!r}")


def _seconds(raw: dict, key: str, default: float, where: str, errors: list[str]) -> float:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not value > 0:
        errors.append(f"{where}.{key}: must be a positive number of seconds, got {value!r}")
        return default
    return float(value)


def _secret_like(name: str) -> bool:
    upper = name.upper()
    return any(w in upper for w in SECRET_WORDS) or any(
        t in SECRET_TOKENS for t in re.split(r"[^A-Z0-9]+", upper))


def _parse_suite(raw, where: str, errors: list[str]) -> Suite | None:
    if not isinstance(raw, dict):
        errors.append(f"{where}: must be a mapping")
        return None
    start = len(errors)
    for key in sorted(set(raw) - _SUITE_KEYS, key=str):
        errors.append(f"{where}.{key}: unknown field")
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        errors.append(f"{where}.name: required")
        name = ""
    elif not SUITE_NAME_RE.match(name):
        errors.append(f"{where}.name: must match {SUITE_NAME_RE.pattern} (used in file names and scripts)")
    test_paths = _str_list(raw.get("test_paths"), f"{where}.test_paths", errors, required=True)
    cwd = raw.get("cwd", ".")
    if not isinstance(cwd, str) or not cwd:
        errors.append(f"{where}.cwd: must be a non-empty path string")
        cwd = "."
    _check_relative(cwd, f"{where}.cwd", errors)
    setup = raw.get("setup") or ""
    if not isinstance(setup, str):
        errors.append(f"{where}.setup: must be a string")
        setup = ""
    module_root = raw.get("module_root")
    if module_root is not None:
        if not isinstance(module_root, str) or not module_root:
            errors.append(f"{where}.module_root: must be a non-empty path string")
            module_root = None
        else:
            _check_relative(module_root, f"{where}.module_root", errors)
    command = raw.get("test_command")
    if not isinstance(command, str) or not command.strip():
        errors.append(f"{where}.test_command: required")
        command = ""
    else:
        try:
            parsed = placeholders.parse(command)
        except placeholders.PlaceholderError as e:
            errors.append(f"{where}.test_command: {e}")
        else:
            if any(p.source == "modules" for p in parsed.placeholders) and module_root is None:
                errors.append(f"{where}.test_command: {{modules}} needs {where}.module_root")
    runnable = raw.get("runnable")
    if runnable is not None:
        runnable = _str_list(runnable, f"{where}.runnable", errors, required=False)
        if not runnable and not any(e.startswith(f"{where}.runnable") for e in errors):
            errors.append(f"{where}.runnable: must not be empty (omit it for the built-in rule)")
        runnable = runnable or None
    timeout = _seconds(raw, "timeout", DEFAULT_TEST_TIMEOUT_S, where, errors)
    setup_timeout = _seconds(raw, "setup_timeout", DEFAULT_SETUP_TIMEOUT_S, where, errors)
    env = raw.get("env") or {}
    if not isinstance(env, dict):
        errors.append(f"{where}.env: must be a mapping of NAME: value")
        env = {}
    clean_env: dict[str, str] = {}
    for k, v in env.items():
        if not isinstance(v, (str, int, float)) or isinstance(v, bool):
            errors.append(f"{where}.env.{k}: value must be a string")
            continue
        v = str(v)
        if _secret_like(str(k)) and len(v) > 7 and not v.startswith("dummy"):
            errors.append(
                f"{where}.env.{k}: looks like a real secret; env holds names with dummy values "
                f"only (start the value with 'dummy')"
            )
        m = _URL_CREDENTIALS.search(v)
        if m and not m.group(1).startswith("dummy"):
            errors.append(
                f"{where}.env.{k}: URL with embedded credentials; env holds dummy values only "
                f"(start the password with 'dummy')"
            )
        clean_env[str(k)] = v
    if len(errors) > start:
        return None
    return Suite(name, test_paths, command, cwd, setup, module_root, clean_env, runnable,
                 timeout, setup_timeout)


def load_config(path: Path) -> MineConfig:
    """Load and validate mine.yaml. Raises MineConfigError listing every problem."""
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise MineConfigError([f"{CONFIG_NAME}: not valid YAML: {e}"]) from e
    except OSError as e:
        raise MineConfigError([f"{CONFIG_NAME}: cannot read {path}: {e}"]) from e
    if not isinstance(raw, dict):
        raise MineConfigError([f"{CONFIG_NAME}: must be a mapping with a `suites` list"])
    errors: list[str] = []
    for key in sorted(set(raw) - _TOP_KEYS, key=str):
        errors.append(f"{key}: unknown field")
    suites: list[Suite] = []
    raw_suites = raw.get("suites")
    if not isinstance(raw_suites, list) or not raw_suites:
        errors.append("suites: required (a non-empty list of test suites)")
        raw_suites = []
    for i, s in enumerate(raw_suites):
        suite = _parse_suite(s, f"suites[{i}]", errors)
        if suite:
            suites.append(suite)
    names = [s.name for s in suites]
    for n in sorted({n for n in names if names.count(n) > 1}):
        errors.append(f"suites: duplicate name {n!r}")
    exclude = raw.get("exclude_paths")
    exclude = DEFAULT_EXCLUDE if exclude is None else _str_list(
        exclude, "exclude_paths", errors, required=False
    )
    max_lines = raw.get("max_changed_lines", DEFAULT_MAX_LINES)
    if not isinstance(max_lines, int) or isinstance(max_lines, bool) or max_lines < 1:
        errors.append("max_changed_lines: must be a positive integer")
    if errors:
        raise MineConfigError(errors)
    return MineConfig(suites, list(exclude), max_lines)


# --- stack detection ---

def detect_suites(repo_root: Path) -> list[Suite]:
    """Guess the repo's test suites from the framework presets (`mine/detect.py`)."""
    from crucible.mine import detect
    return [d.suite for d in detect.detect(repo_root)]


_DETECTED_HEADER = """\
# AUTO-DETECTED by `crucible mine` from its framework presets. Every suite below is a first
# guess: check each line (the test_paths globs, the setup and test commands, and env: names +
# dummy values only, never real secrets), then run `crucible mine calibrate` to prove the
# commands actually run the selected tests. Run `crucible mine` again once it looks right.
"""
_TEMPLATE_HEADER = """\
# NOTHING DETECTED. `crucible mine` found no marker of a framework preset ({presets}) in the
# repo root or up to two levels below it, so `suites` is empty. Fill it in (one entry per test
# stack, or copy the closest preset from src/crucible/data/presets/), then run
# `crucible mine calibrate`.
#
# suites:
#   - name: backend
#     test_paths: ["backend/**/tests/**", "backend/**/test_*.py"]   # globs of test files
#     cwd: backend                       # where setup and test commands run (relative to repo root)
#     setup: pip install -q -r requirements.txt
#     test_command: python manage.py test {{tests|module:backend}}
#         # {{tests}}: changed test files relative to cwd; filters after `|` reshape them:
#         # module:<root> stem dir unique prefix:<s> suffix:<s> join:<sep>. No placeholder at
#         # all runs the whole suite. See docs/design.md ("Placeholders").
#     module_root: backend               # optional; {{modules}} = {{tests|module:<module_root>}}
#     runnable: ["**/*Test.java"]        # optional; which changed test files are passed to the runner
#     timeout: 600                       # optional, seconds for the test step
#     setup_timeout: 1800                # optional, seconds for the setup step
#     env: {{DJANGO_SECRET_KEY: dummy-not-secret}}      # dummy values only
"""
_TAIL = """\
# Files that never count as the change itself (globs; a pattern without `/` matches any depth).
exclude_paths: {exclude}
# Candidates changing more lines than this are excluded.
max_changed_lines: {max_lines}
"""


def render_default(suites: list[Suite], comments: dict[str, list[str]] | None = None) -> str:
    """The commented mine.yaml text `crucible mine` writes on first use. `comments` maps a
    suite name to the comment lines written above it (where the guess came from)."""
    exclude = json.dumps(DEFAULT_EXCLUDE)
    tail = _TAIL.format(exclude=exclude, max_lines=DEFAULT_MAX_LINES)
    if not suites:
        from crucible.mine import presets
        names = ", ".join(presets.shipped())
        return _TEMPLATE_HEADER.format(presets=names) + "suites: []\n" + tail
    parts = [_DETECTED_HEADER, "suites:\n"]
    for s in suites:
        parts += [f"# {ln}\n".replace("# \n", "#\n") for ln in (comments or {}).get(s.name, [])]
        parts.append(yaml.safe_dump([s.to_dict()], sort_keys=False, default_flow_style=None,
                                    width=100))
    return "".join(parts) + tail


def write_default(path: Path, repo_root: Path) -> list[Suite]:
    """Detect suites and write a commented mine.yaml at `path`. Never overwrites."""
    from crucible.mine import detect
    path = Path(path)
    if path.exists():
        raise FileExistsError(path)
    found = detect.detect(repo_root)
    suites = [d.suite for d in found]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_default(suites, {d.suite.name: d.comment() for d in found}),
                    encoding="utf-8")
    return suites


def ensure_gitignored(home: Path) -> bool:
    """Make sure `<home>/.gitignore` ignores `mine/` (mining state). True if it changed."""
    f = Path(home) / ".gitignore"
    text = f.read_text(encoding="utf-8") if f.is_file() else ""
    ignored = {ln.strip() for ln in text.splitlines() if not ln.lstrip().startswith("#")}
    if ignored & {"mine/", "/mine/", "mine", "/mine"}:
        return False
    f.write_text(text + ("" if not text or text.endswith("\n") else "\n") + "mine/\n", encoding="utf-8")
    return True
