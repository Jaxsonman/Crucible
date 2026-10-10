"""Check Crucible's version bookkeeping.

`pyproject.toml` holds the version. This verifies that `.claude-plugin/plugin.json`, `uv.lock` and
`CHANGELOG.md` agree with it and, given a base ref (CI passes the pull request's base branch), that
the version is higher than the base's, so every pull request releases something.

    python scripts/check_version.py [BASE_REF]

Standard library only, so CI can run it before the project is installed.
"""

import datetime
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEMVER = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")


def parse(text: str) -> tuple[int, int, int]:
    if not SEMVER.fullmatch(text):
        raise ValueError(f"{text!r} is not a MAJOR.MINOR.PATCH version")
    major, minor, patch = text.split(".")
    return int(major), int(minor), int(patch)


def read(root: Path, name: str) -> str:
    try:
        return (root / name).read_text()
    except OSError as e:
        raise ValueError(f"{name}: cannot read: {e.strerror}") from e


def load(root: Path, name: str, loads, *keys: str):
    """Parse `name` with `loads` and descend `keys`, turning any failure into a ValueError."""
    try:
        value = loads(read(root, name))
        for key in keys:
            value = value[key]
    except (ValueError, LookupError, TypeError) as e:  # decode errors are ValueErrors
        raise ValueError(f"{name}: unusable ({e!r})") from e
    return value


def project_version(root: Path) -> str:
    return load(root, "pyproject.toml", tomllib.loads, "project", "version")


def lock_version(root: Path) -> str | None:
    packages = load(root, "uv.lock", tomllib.loads).get("package", [])
    return next((p.get("version") for p in packages if p.get("name") == "crucible"), None)


def changelog_problem(text: str, version: str) -> str | None:
    """Why the newest changelog section is not a filled-in, dated one for `version`, if so."""
    wanted = f"## [{version}] - "
    in_fence = False
    section: list[str] | None = None
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        if in_fence:
            if section is not None:
                section.append(line)
            continue
        if line.startswith("## "):
            if section is not None:
                break
            heading = line.rstrip()
            date = heading.removeprefix(wanted)
            try:
                if heading == date or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
                    raise ValueError
                datetime.date.fromisoformat(date)
            except ValueError:
                return f"the newest section must be `{wanted}YYYY-MM-DD`, it is `{heading}`"
            section = []
        elif section is not None:
            section.append(line)
    if section is None:
        return f"it has no `{wanted}YYYY-MM-DD` section"
    if not any(line.strip() for line in section):
        return f"the `{version}` section is empty"
    return None


def problems(root: Path, base: str | None = None) -> list[str]:
    """Everything wrong with the version in `root`, as messages; empty when it is fine."""
    try:
        version = project_version(root)
        current = parse(version)
    except ValueError as e:
        return [str(e)]

    found = []
    plugin = load(root, ".claude-plugin/plugin.json", json.loads, "version")
    if plugin != version:
        found.append(f".claude-plugin/plugin.json is {plugin}, pyproject.toml is {version}")
    locked = lock_version(root)
    if locked != version:
        found.append(
            f"uv.lock records crucible {locked}, pyproject.toml is {version}; run `uv lock`"
        )
    why = changelog_problem(read(root, "CHANGELOG.md"), version)
    if why:
        found.append(f"CHANGELOG.md: {why}")
    if base is not None and current <= parse(base):
        found.append(f"version {version} must be higher than the base branch's {base}; bump it")
    return found


def base_version(root: Path, ref: str) -> str:
    shown = subprocess.run(
        ["git", "show", f"{ref}:pyproject.toml"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if shown.returncode:
        raise ValueError(f"cannot read pyproject.toml at {ref!r}: {shown.stderr.strip()}")
    try:
        return tomllib.loads(shown.stdout)["project"]["version"]
    except (ValueError, LookupError, TypeError) as e:
        raise ValueError(f"pyproject.toml at {ref!r}: unusable ({e!r})") from e


def main(argv: list[str], root: Path = ROOT) -> int:
    try:
        base = base_version(root, argv[0]) if argv else None
        found = problems(root, base)
    except ValueError as e:
        found = [str(e)]
    for message in found:
        print(f"version check: {message}", file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
