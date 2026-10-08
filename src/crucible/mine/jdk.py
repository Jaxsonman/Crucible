"""Which Java does a Maven / Gradle project need, and is a matching JDK installed here?

Detection reads the build files (pom properties and compiler plugin, Gradle toolchain /
`jvmToolchain` / `sourceCompatibility`, the version catalog) and, as a fallback, the CI
`actions/setup-java` step. The local lookup (macOS `java_home`, `/usr/lib/jvm`,
`JAVA_HOME_<N>_*` variables) is injectable so tests never depend on the machine.
"""

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


def _prop(text: str, name: str) -> int | None:
    m = re.search(rf"<{re.escape(name)}>\s*([^<]+?)\s*</{re.escape(name)}>", text)
    if not m:
        return None
    value = m.group(1)
    ref = re.fullmatch(r"\$\{([\w.\-]+)\}", value)
    if ref and ref.group(1) != name:
        return _prop(text, ref.group(1))
    return _major(value)


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
        for f in _gradle_files(suite_dir):
            if (v := parse_gradle(_read(f))):
                found.append((v, rel(f)))
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
LINUX_JVM_DIR = Path("/usr/lib/jvm")


def find_local_jdk(version: int, *, env: dict | None = None, java_home_tool: str | None = MAC_JAVA_HOME,
                   jvm_dir: Path | None = LINUX_JVM_DIR) -> str | None:
    """A JAVA_HOME for Java `version` on this machine, or None. Looked up in order:
    `JAVA_HOME_<N>_<ARCH>` variables, macOS `java_home -v N`, `/usr/lib/jvm/*N*`."""
    if version < 5:
        return None
    env = os.environ if env is None else env
    for name in sorted(env):
        if re.fullmatch(rf"JAVA_HOME_{version}(?:_[A-Z0-9]+)?", name) and env[name]:
            return env[name]
    if java_home_tool and Path(java_home_tool).is_file():
        try:
            r = subprocess.run([java_home_tool, "-v", str(version)], capture_output=True,
                               text=True, timeout=10, check=False)
        except (OSError, subprocess.SubprocessError):
            r = None
        if r and r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip().splitlines()[0]
    if jvm_dir and Path(jvm_dir).is_dir():
        token = re.compile(rf"(?<!\d)(?:1\.)?{version}(?!\d)")
        for d in sorted(Path(jvm_dir).iterdir()):
            if d.is_dir() and token.search(d.name):
                return str(d)
    return None


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
