"""Attempt records: writing, loading, redaction, success rule. See docs/design.md."""

import json
import os
import sys
from pathlib import Path
from typing import Any

from crucible import paths

Record = dict[str, Any]

SECRET_NAME_PARTS = ("KEY", "TOKEN", "SECRET", "PASSWORD")
MIN_SECRET_LEN = 8


def redact(text: str) -> str:
    """Replace values of secret-looking env vars with [REDACTED:<NAME>]."""
    secrets: list[tuple[str, str]] = []
    for name, value in os.environ.items():
        if len(value) >= MIN_SECRET_LEN and any(p in name.upper() for p in SECRET_NAME_PARTS):
            secrets.append((name, value))
    # Longest first so a secret containing another secret is fully replaced.
    for name, value in sorted(secrets, key=lambda nv: len(nv[1]), reverse=True):
        marker = f"[REDACTED:{name}]"
        text = text.replace(value, marker)
        escaped = json.dumps(value)[1:-1]  # the form it takes inside a JSON string
        if escaped != value:
            text = text.replace(escaped, marker)
    return text


def write_text(path: Path, text: str) -> None:
    """Write a text file under results/ through redact(). Never overwrites."""
    with open(path, "x", encoding="utf-8") as f:
        f.write(redact(text))


def write_record(attempt_dir: Path, record: Record) -> None:
    """Create the attempt directory (must not exist) and write record.json."""
    attempt_dir.mkdir(parents=True, exist_ok=False)
    write_text(attempt_dir / "record.json", json.dumps(record, indent=2) + "\n")


def update_record(attempt_dir: Path, record: Record) -> None:
    """Rewrite an existing record.json (redacted, atomic). Only `crucible judge` may call this."""
    path = attempt_dir / "record.json"
    tmp = attempt_dir / "record.json.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(redact(json.dumps(record, indent=2) + "\n"))
    os.replace(tmp, path)


def load_records(task_id: str | None = None) -> list[Record]:
    """All attempt records under results/, optionally for one task, in path order."""
    root = paths.results_dir()
    pattern = f"{task_id}/*/*/record.json" if task_id else "*/*/*/record.json"
    out: list[Record] = []
    for f in sorted(root.glob(pattern)):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            print(f"warning: skipping unreadable record {f}: {e}", file=sys.stderr)
            continue
        if isinstance(data, dict):
            out.append(data)
    return out


def compute_success(record: Record) -> bool | None:
    """docs/design.md success rule: false if not completed; else the check's verdict when the
    check produced one; else the judge's verdict; else null (unknown)."""
    if record.get("status") != "completed":
        return False
    check = record.get("check") or {}
    if check.get("ran") and check.get("passed") is not None:
        return bool(check["passed"])
    judge = record.get("judge") or {}
    if judge.get("ran") and judge.get("passed") is not None:
        return bool(judge["passed"])
    return None


def judge_disagrees(record: Record) -> bool:
    """True when the check and the judge both produced a verdict and the verdicts differ."""
    check = record.get("check") or {}
    judge = record.get("judge") or {}
    if not (check.get("ran") and check.get("passed") is not None):
        return False
    if not (judge.get("ran") and judge.get("passed") is not None):
        return False
    return bool(check["passed"]) != bool(judge["passed"])
