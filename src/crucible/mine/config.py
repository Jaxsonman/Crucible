"""`.crucible/mine.yaml`: per-repo test suites for mining, plus stack detection."""

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import yaml

CONFIG_NAME = "mine.yaml"
PLACEHOLDERS = ("tests", "modules")
SECRET_WORDS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "PRIVATE", "CREDENTIAL")
SECRET_TOKENS = ("PAT",)  # whole `_`-separated words only, so `PATH` is not a secret
_URL_CREDENTIALS = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^/\s:@]*:([^/\s@]*)@")
DEFAULT_MAX_LINES = 800
DEFAULT_EXCLUDE = [
    "**/package-lock.json", "**/*.lock", "**/migrations/**", "docs/**", "*.md",
]
SUITE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_SUITE_KEYS = {"name", "test_paths", "cwd", "setup", "test_command", "module_root", "env"}
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

    def to_dict(self) -> dict:
        """Plain-data form (stable key order), e.g. for hashing a suite's settings."""
        d = {"name": self.name, "test_paths": self.test_paths, "cwd": self.cwd}
        if self.setup:
            d["setup"] = self.setup
        d["test_command"] = self.test_command
        if self.module_root is not None:
            d["module_root"] = self.module_root
        if self.env:
            d["env"] = self.env
        return d

    def settings_hash(self) -> str:
        blob = json.dumps(self.to_dict(), sort_keys=True).encode()
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
        used = re.findall(r"\{([^{}]*)\}", command)
        for ph in sorted(set(used) - set(PLACEHOLDERS)):
            errors.append(
                f"{where}.test_command: unknown placeholder {{{ph}}} "
                f"(allowed: {', '.join('{' + p + '}' for p in PLACEHOLDERS)})"
            )
        if not used:
            errors.append(f"{where}.test_command: must contain {{tests}} or {{modules}}")
        if "modules" in used and module_root is None:
            errors.append(f"{where}.test_command: {{modules}} needs {where}.module_root")
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
    return Suite(name, test_paths, command, cwd, setup, module_root, clean_env)


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

_SKIP_DIRS = {"node_modules", "venv", "env", "build", "dist", "site-packages"}


def _candidate_dirs(root: Path) -> list[Path]:
    subs = sorted(
        d for d in root.iterdir()
        if d.is_dir() and not d.name.startswith(".") and d.name not in _SKIP_DIRS
        and not d.name.endswith(".egg-info")
    )
    return [root, *subs]


def _has_pytest_config(d: Path) -> bool:
    if (d / "pytest.ini").is_file():
        return True
    pyproject = d / "pyproject.toml"
    if pyproject.is_file():
        text = pyproject.read_text(encoding="utf-8", errors="replace")
        return "[tool.pytest" in text
    return False


def _js_runner(d: Path) -> str | None:
    pkg = d / "package.json"
    if not pkg.is_file():
        return None
    try:
        data = json.loads(pkg.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
    for runner in ("vitest", "jest"):
        if runner in deps:
            return runner
    return None


def _python_setup(d: Path) -> str:
    if (d / "requirements.txt").is_file():
        return "pip install -q -r requirements.txt"
    if (d / "pyproject.toml").is_file() or (d / "setup.py").is_file():
        return "pip install -q -e ."
    return ""


_PYTHON_MARKERS = ("pytest.ini", "pyproject.toml", "setup.cfg", "setup.py", "manage.py")


def _has_python_marker(d: Path) -> bool:
    return any((d / m).is_file() for m in _PYTHON_MARKERS) or any(d.glob("requirements*.txt"))


def _root_pytest(root: Path):
    return (root, "pytest", "python -m pytest {tests}",
            ["**/test_*.py", "**/*_test.py", "**/conftest.py"], _python_setup(root), None)


def detect_suites(repo_root: Path) -> list[Suite]:
    """Guess test suites (pytest, Django, Vitest, Jest) in the repo root or one level down."""
    repo_root = Path(repo_root)
    found: list[tuple[Path, str, str, list[str], str, str | None]] = []
    root_marked = _has_python_marker(repo_root)
    for d in _candidate_dirs(repo_root):
        prefix = "" if d == repo_root else d.name + "/"
        explicit_pytest = _has_pytest_config(d)
        django = (d / "manage.py").is_file()
        conftest_only = (d / "conftest.py").is_file() and not django and not explicit_pytest
        if conftest_only and d != repo_root and root_marked and not _has_python_marker(d):
            # a conftest.py-only dir (typically `tests/`) belongs to the root's python project
            if not any(f[0] == repo_root and f[1] in ("pytest", "django") for f in found):
                found.insert(0, _root_pytest(repo_root))
        elif explicit_pytest or ((d / "conftest.py").is_file() and not django):
            found.append((d, "pytest", "python -m pytest {tests}",
                          [f"{prefix}**/test_*.py", f"{prefix}**/*_test.py", f"{prefix}**/conftest.py"],
                          _python_setup(d), None))
        elif django:
            found.append((d, "django", "python manage.py test {modules}",
                          [f"{prefix}**/tests/**", f"{prefix}**/test_*.py", f"{prefix}**/tests.py"],
                          _python_setup(d), "."))
        runner = _js_runner(d)
        if runner:
            cmd = "npx vitest run {tests}" if runner == "vitest" else "npx jest {tests}"
            setup = "npm ci --silent" if (d / "package-lock.json").is_file() else "npm install --silent"
            found.append((d, runner, cmd,
                          [f"{prefix}**/*.test.*", f"{prefix}**/*.spec.*", f"{prefix}**/__tests__/**"],
                          setup, None))
    suites: list[Suite] = []
    used: set[str] = set()
    for d, kind, cmd, test_paths, setup, module_root in found:
        name = d.name if d != repo_root else kind
        name = re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip(".-") or kind
        if name in used:
            name = f"{name}-{kind}"
        used.add(name)
        rel = "." if d == repo_root else d.name
        suites.append(Suite(name, test_paths, cmd, rel, setup, module_root and rel))
    return suites


_DETECTED_HEADER = """\
# AUTO-DETECTED by `crucible mine`. Check every line before relying on it:
# the test_paths globs, the setup and test commands, and env (names + dummy values only,
# never real secrets). Run `crucible mine` again once it looks right.
"""
_TEMPLATE_HEADER = """\
# NOTHING DETECTED. `crucible mine` could not find pytest, Django, Vitest or Jest in this repo,
# so `suites` is empty. Fill it in (one entry per test stack), then run `crucible mine` again.
#
# suites:
#   - name: backend
#     test_paths: ["backend/**/tests/**", "backend/**/test_*.py"]   # globs of test files
#     cwd: backend                       # where setup and test commands run (relative to repo root)
#     setup: pip install -q -r requirements.txt
#     test_command: python manage.py test {modules}   # {tests}: file paths relative to cwd
#     module_root: backend               # optional; enables {modules} (dotted paths, .py stripped)
#     env: {DJANGO_SECRET_KEY: dummy-not-secret}      # dummy values only
"""
_TAIL = """\
# Files that never count as the change itself (globs; a pattern without `/` matches any depth).
exclude_paths: {exclude}
# Candidates changing more lines than this are excluded.
max_changed_lines: {max_lines}
"""


def render_default(suites: list[Suite]) -> str:
    """The commented mine.yaml text `crucible mine` writes on first use."""
    exclude = json.dumps(DEFAULT_EXCLUDE)
    tail = _TAIL.format(exclude=exclude, max_lines=DEFAULT_MAX_LINES)
    if not suites:
        return _TEMPLATE_HEADER + "suites: []\n" + tail
    body = yaml.safe_dump({"suites": [s.to_dict() for s in suites]}, sort_keys=False,
                          default_flow_style=None, width=100)
    return _DETECTED_HEADER + body + tail


def write_default(path: Path, repo_root: Path) -> list[Suite]:
    """Detect suites and write a commented mine.yaml at `path`. Never overwrites."""
    path = Path(path)
    if path.exists():
        raise FileExistsError(path)
    suites = detect_suites(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_default(suites), encoding="utf-8")
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
