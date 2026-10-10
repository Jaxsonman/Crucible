"""Which Java does a Maven / Gradle project need, and is a matching JDK installed here?

Detection reads the build files (pom properties and compiler plugin, Gradle toolchain /
`jvmToolchain` / `sourceCompatibility`, the version catalog) and, as a fallback, the CI
`actions/setup-java` step. The local lookup enumerates the JDKs on this machine (macOS `java_home -V`,
`JAVA_HOME_<N>_*` variables, the usual install directories), reads each one's real major from
its `release` file, and picks the exact major or else the lowest newer one. It is injectable so
tests never depend on the machine.
"""

import glob
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

_MAX_BYTES = 1_000_000
_SKIP = {"node_modules", "build", "target", "out", ".git", ".gradle", "vendor", "dist"}

JdkLookup = Callable[[int], "str | None"]


@dataclass(frozen=True)
class Requirement:
    version: int
    source: str  # repo-relative file the version came from


def _major(text: str) -> int | None:
    """`17` -> 17, `1.8` -> 8, anything else -> None."""
    m = re.fullmatch(r"\s*(?:1\.)?(\d{1,2})(?:\.\d+)*\s*", text)
    if not m:
        return None
    n = int(m.group(1))
    return n if n >= 5 else None


def _prop(text: str, name: str, seen: frozenset[str] = frozenset()) -> int | None:
    """The numeric Java version of the first `<name>` tag with one (a `${prop}` value is
    followed; reference cycles, `true`, paths and other non-numeric values are skipped)."""
    if name in seen:
        return None
    seen = seen | {name}
    for m in re.finditer(rf"<{re.escape(name)}>\s*([^<]+?)\s*</{re.escape(name)}>", text):
        value = m.group(1)
        ref = re.fullmatch(r"\$\{([\w.\-]+)\}", value)
        v = _prop(text, ref.group(1), seen) if ref else _major(value)
        if v:
            return v
    return None


def parse_pom(text: str) -> int | None:
    """The Java version a pom asks for: the compiler plugin's `<release>`, then
    `maven.compiler.release`, `java.version`, `maven.compiler.source`, the plugin's
    `<source>` / `<target>` (`1.8` means 8; `${prop}` references are followed)."""
    for name in ("release", "maven.compiler.release", "java.version", "maven.compiler.source",
                 "source", "maven.compiler.target", "target"):
        v = _prop(text, name)
        if v:
            return v
    return None


_GRADLE = (
    re.compile(r"JavaLanguageVersion\s*\.\s*of\(\s*['\"]?(\d{1,2})"),
    re.compile(r"jvmToolchain\(\s*(\d{1,2})\s*\)"),
    re.compile(r"\blanguageVersion\s*=\s*['\"]?(\d{1,2})\b(?!\.)"),
    re.compile(r"JavaVersion\.VERSION_(?:1_)?(\d{1,2})\b"),
    re.compile(r"\b(?:source|target)Compatibility\s*=\s*['\"]?(1\.\d|\d{1,2})\b"),
)


def parse_gradle(text: str) -> int | None:
    """The highest Java version a Gradle build file names (toolchain, `jvmToolchain`,
    `languageVersion = N`, `JavaVersion.VERSION_N`, `sourceCompatibility`)."""
    found = [v for rx in _GRADLE for m in rx.finditer(text)
             if (v := _major(m.group(1)))]
    return max(found) if found else None


_CATALOG_KEYS = ("jdk", "java", "java-version", "javaVersion", "jvm", "jvmTarget", "jdk-version")


def parse_catalog(text: str) -> int | None:
    """`jdk = "17"` (and similar keys) in a Gradle version catalog."""
    for key in _CATALOG_KEYS:
        m = re.search(rf'(?m)^\s*{re.escape(key)}\s*=\s*"([^"]+)"', text)
        if m and (v := _major(m.group(1))):
            return v
    return None


def parse_ci(text: str) -> int | None:
    """The first literal `java-version:` of an `actions/setup-java` step."""
    for m in re.finditer(r"(?m)^\s*java-version:\s*['\"]?([\d.]+)['\"]?\s*$", text):
        if v := _major(m.group(1)):
            return v
    return None


def _read(path: Path) -> str:
    try:
        if path.stat().st_size > _MAX_BYTES:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _gradle_files(d: Path) -> list[Path]:
    out = [p for p in sorted(d.glob("*.gradle*")) if p.suffix in (".gradle", ".kts")]
    subs = [x for x in sorted(d.iterdir()) if x.is_dir() and x.name not in _SKIP
            and not x.name.startswith(".")] if d.is_dir() else []
    for sub in subs:
        out += [p for p in sorted(sub.glob("*.gradle*")) if p.suffix in (".gradle", ".kts")]
        if sub.name == "buildSrc":
            out += sorted(sub.rglob("*.gradle*"))[:20] + sorted(sub.rglob("*.kt"))[:20]
        for sub2 in sorted(x for x in sub.iterdir() if x.is_dir() and x.name not in _SKIP
                           and not x.name.startswith(".")):
            out += [p for p in sorted(sub2.glob("*.gradle*")) if p.suffix in (".gradle", ".kts")]
    return out


_AUX_DIRS = re.compile(r"^(?:buildSrc|build-logic|gradle-plugins?|.+-plugins?)$")


def _auxiliary_build(path: Path, suite_dir: Path) -> bool:
    try:
        parts = path.relative_to(suite_dir).parts[:-1]
    except ValueError:
        return False
    return any(_AUX_DIRS.match(p) for p in parts)


def _ci_files(root: Path) -> list[Path]:
    wf = root / ".github" / "workflows"
    return sorted(wf.glob("*.y*ml")) if wf.is_dir() else []


def required_java(suite_dir: Path, repo_root: Path, tool: str) -> Requirement | None:
    """The Java version the `maven` / `gradle` project in `suite_dir` needs: the highest the
    build files name, else the first CI `setup-java` version. None when nothing says."""
    suite_dir, repo_root = Path(suite_dir), Path(repo_root)

    def rel(p: Path) -> str:
        try:
            return p.relative_to(repo_root).as_posix()
        except ValueError:
            return p.name

    found: list[tuple[int, str]] = []
    if tool == "maven":
        for pom in [suite_dir / "pom.xml"]:
            if (v := parse_pom(_read(pom))):
                found.append((v, rel(pom)))
    else:
        cat = suite_dir / "gradle" / "libs.versions.toml"
        if (v := parse_catalog(_read(cat))):
            found.append((v, rel(cat)))
        files = _gradle_files(suite_dir)
        main = [f for f in files if not _auxiliary_build(f, suite_dir)]
        # buildSrc / build-logic / plugin builds say what the build tooling runs on, not what
        # the project targets: they count only when the main project names nothing.
        main_found = [(v, rel(f)) for f in main if (v := parse_gradle(_read(f)))]
        found += main_found or [(v, rel(f)) for f in files if f not in main
                                if (v := parse_gradle(_read(f)))]
    if found:
        best = max(found, key=lambda t: t[0])
        return Requirement(*best)
    for f in _ci_files(repo_root):
        text = _read(f)
        if "setup-java" in text and (v := parse_ci(text)):
            return Requirement(v, rel(f))
    return None


# --- local JDKs ---

MAC_JAVA_HOME = "/usr/libexec/java_home"
# Glob patterns of directories that are (or hold, under `Contents/Home` or Homebrew's
# `libexec/openjdk.jdk/Contents/Home`) a JDK home.
JDK_ROOTS = (
    "/Library/Java/JavaVirtualMachines/*",
    "/opt/homebrew/opt/openjdk*",
    "/usr/local/opt/openjdk*",
    "/usr/lib/jvm/*",
    "~/.sdkman/candidates/java/*",
)
_HOME_SUBDIRS = ("", "Contents/Home", "libexec/openjdk.jdk/Contents/Home")


@dataclass(frozen=True)
class LocalJdk:
    home: str
    major: int


def release_major(home: str | Path) -> int | None:
    """The Java major in `<home>/release` (`JAVA_VERSION="22.0.1"` -> 22, `"1.8.0_392"` -> 8),
    or None when there is no readable release file."""
    text = _read(Path(home) / "release")
    m = re.search(r'(?m)^JAVA_VERSION\s*=\s*"?([^"\s]+)', text)
    if not m:
        return None
    parts = re.match(r"(?:1\.)?(\d{1,3})", m.group(1))
    return int(parts.group(1)) if parts else None


def _is_jdk_home(home: Path) -> bool:
    """A full JDK: a `release` file and a compiler (a bare JRE has no `javac`). Never raises:
    unreadable or vanished candidates simply don't count."""
    try:
        return (os.path.isfile(home / "release")
                and any(os.path.isfile(home / "bin" / exe) for exe in ("javac", "javac.exe")))
    except OSError:
        return False


def _resolve_home(path: str | Path) -> str | None:
    for sub in _HOME_SUBDIRS:
        home = Path(path) / sub if sub else Path(path)
        if _is_jdk_home(home):
            return str(home)
    return None


def _java_home_listing(tool: str | None) -> list[str]:
    """JDK homes named by `java_home -V` (macOS; it prints the list on stderr)."""
    if not tool or not Path(tool).is_file():
        return []
    try:
        r = subprocess.run([tool, "-V"], capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    homes = []
    for line in (r.stderr + "\n" + r.stdout).splitlines():
        m = re.match(r'^\s+\d[\w.+\-]*\s.*?"\s+(/.+?)\s*$', line)
        if m:
            homes.append(m.group(1))
    return homes


def local_jdks(*, env: dict | None = None, java_home_tool: str | None = MAC_JAVA_HOME,
               roots: tuple[str, ...] | list[str] | None = None) -> list[LocalJdk]:
    """The JDKs on this machine with their real majors (from each `release` file; a directory
    without one is not a JDK we can place, whatever its name says). Sources, in order:
    `JAVA_HOME_<N>_*` variables, `java_home -V`, then the `roots` globs. Duplicates (symlinks)
    are listed once."""
    env = os.environ if env is None else env
    candidates: list[str] = [env[n] for n in sorted(env)
                             if re.fullmatch(r"JAVA_HOME_\d+(?:_[A-Z0-9]+)?", n) and env[n]]
    candidates += _java_home_listing(java_home_tool)
    for pattern in JDK_ROOTS if roots is None else roots:
        candidates += sorted(glob.glob(os.path.expanduser(pattern)))
    out, seen = [], set()
    for c in candidates:
        home = _resolve_home(c)
        if not home:
            continue
        key = os.path.realpath(home)
        major = release_major(home)
        if key in seen or major is None:
            continue
        seen.add(key)
        out.append(LocalJdk(home, major))
    return out


def pick_jdk(version: int, jdks: list[LocalJdk]) -> str | None:
    """The JDK of exactly `version`, else the one with the lowest major above it, else None."""
    exact = [j for j in jdks if j.major == version]
    if exact:
        return exact[0].home
    newer = sorted((j for j in jdks if j.major > version), key=lambda j: j.major)
    return newer[0].home if newer else None


def find_local_jdk(version: int, *, env: dict | None = None,
                   java_home_tool: str | None = MAC_JAVA_HOME,
                   roots: tuple[str, ...] | list[str] | None = None,
                   jdks: list[LocalJdk] | None = None) -> str | None:
    """A JAVA_HOME for Java `version` on this machine, or None: the exact major if installed,
    else the lowest newer major (never the newest: `java_home -v N` returns that, and a build
    pinned to 21 breaks on 26). Pass `jdks` to skip enumeration."""
    if version < 5:
        return None
    found = local_jdks(env=env, java_home_tool=java_home_tool, roots=roots) if jdks is None else jdks
    return pick_jdk(version, found)


def describe(req: Requirement, home: str | None) -> list[str]:
    """Comment lines for mine.yaml about the required Java and what was done about it."""
    lines = [f"Needs Java {req.version} (from {req.source})."]
    if home:
        lines.append("A matching local JDK was found: `env: JAVA_HOME` is set to it below.")
    else:
        lines.append(
            f"No local JDK {req.version} was found. Install one and set `env: {{JAVA_HOME: "
            "<path>}}` for this suite, or run the build in a container through a wrapper script "
            "in `.crucible/scripts/` (see docs/mining-recipes.md).")
    return lines
