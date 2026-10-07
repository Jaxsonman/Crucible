"""Run one prompt through `claude -p`. See docs/claude-headless.md."""

import json
import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from crucible.configs import Config

TERM_GRACE_S = 10
TOKEN_FIELDS = {
    "inputTokens": "input",
    "outputTokens": "output",
    "cacheCreationInputTokens": "cache_creation",
    "cacheReadInputTokens": "cache_read",
}


@dataclass
class ExecResult:
    status: str  # completed | error | timeout
    error: str | None = None
    exit_code: int | None = None
    wall_seconds: float = 0.0
    subtype: str | None = None
    num_turns: int | None = None
    session_id: str | None = None
    reported_duration_ms: int | None = None
    reported_cost_usd: float | None = None
    tokens: dict[str, int] = field(default_factory=dict)
    tokens_by_model: dict[str, dict[str, int]] = field(default_factory=dict)
    resolved_model: str | None = None
    result_text: str | None = None
    raw_output: str = ""


def build_command(
    prompt: str, config: Config, max_turns: int, max_budget_usd: float
) -> list[str]:
    isolation = os.environ.get("CRUCIBLE_ISOLATION", "safe")
    flag = "--bare" if isolation == "bare" else "--safe-mode"
    return [
        os.environ.get("CRUCIBLE_CLAUDE_BIN", "claude"),
        "-p",
        prompt,
        "--model",
        config.model,
        "--effort",
        config.effort,
        "--output-format",
        "json",
        "--permission-mode",
        "bypassPermissions",
        "--max-turns",
        str(max_turns),
        "--max-budget-usd",
        str(max_budget_usd),
        "--no-session-persistence",
        "--strict-mcp-config",
        flag,
    ]


def _killpg(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _terminate_group(proc: subprocess.Popen) -> tuple[str, str]:
    """SIGTERM the process group, wait up to 10 s, then SIGKILL. Returns drained (out, err)."""
    _killpg(proc.pid, signal.SIGTERM)
    try:
        out, err = proc.communicate(timeout=TERM_GRACE_S)
    except subprocess.TimeoutExpired:
        out, err = "", ""
    # Always SIGKILL the group: the leader may have exited while a grandchild ignored SIGTERM.
    _killpg(proc.pid, signal.SIGKILL)
    try:
        out2, err2 = proc.communicate(timeout=TERM_GRACE_S)
        out, err = out or out2, err or err2
    except subprocess.TimeoutExpired:
        pass
    return out or "", err or ""


def _sum_tokens(model_usage: dict) -> tuple[dict[str, int], dict[str, dict[str, int]]]:
    by_model: dict[str, dict[str, int]] = {}
    total = dict.fromkeys(TOKEN_FIELDS.values(), 0)
    for model_id, usage in model_usage.items():
        if not isinstance(usage, dict):
            continue
        row = {}
        for src, dst in TOKEN_FIELDS.items():
            value = usage.get(src)
            row[dst] = value if isinstance(value, int) and not isinstance(value, bool) else 0
            total[dst] += row[dst]
        by_model[str(model_id)] = row
    return total, by_model


def parse_output(stdout: str) -> dict | None:
    try:
        data = json.loads(stdout)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _result_from(
    data: dict, exit_code: int | None, wall: float, stdout: str, stderr: str
) -> ExecResult:
    tokens, by_model = _sum_tokens(data.get("modelUsage") or {})
    resolved = None
    if by_model:
        resolved = max(by_model, key=lambda m: by_model[m]["output"])
    subtype = data.get("subtype")
    error = None
    if exit_code not in (0, None):
        error = f"claude exited with code {exit_code}"
    elif data.get("is_error") or (subtype is not None and subtype != "success"):
        error = f"claude reported error (subtype={subtype})"
    if error and exit_code not in (0, None) and stderr.strip():
        error += f": {stderr.strip()[:500]}"
    result = data.get("result")
    return ExecResult(
        status="error" if error else "completed",
        error=error,
        exit_code=exit_code,
        wall_seconds=wall,
        subtype=subtype,
        num_turns=data.get("num_turns"),
        session_id=data.get("session_id"),
        reported_duration_ms=data.get("duration_ms"),
        reported_cost_usd=data.get("total_cost_usd"),
        tokens=tokens,
        tokens_by_model=by_model,
        resolved_model=resolved,
        result_text=result if isinstance(result, str) else None,
        raw_output=stdout,
    )


def execute(
    prompt: str,
    config: Config,
    cwd: str | Path,
    max_turns: int,
    timeout_s: float,
    max_budget_usd: float = 5.0,
) -> ExecResult:
    """Run claude for one config in cwd. Never raises for a failed run."""
    cmd = build_command(prompt, config, max_turns, max_budget_usd)
    start = time.monotonic()
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        )
    except OSError as e:
        return ExecResult(
            status="error",
            error=f"could not start {cmd[0]!r}: {e}"[:500],
            wall_seconds=time.monotonic() - start,
        )
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        stdout, stderr = _terminate_group(proc)
        return ExecResult(
            status="timeout",
            error=f"timed out after {timeout_s}s",
            exit_code=proc.returncode,
            wall_seconds=time.monotonic() - start,
            raw_output=stdout,
        )
    except BaseException:  # KeyboardInterrupt etc.: don't leave the group running and spending
        _terminate_group(proc)
        raise
    wall = time.monotonic() - start
    # Clean up stray grandchildren left behind by a finished run.
    _killpg(proc.pid, signal.SIGKILL)
    data = parse_output(stdout)
    if data is None:
        detail = stderr.strip()[:500]
        return ExecResult(
            status="error",
            error=detail or f"unparseable output from claude (exit {proc.returncode})",
            exit_code=proc.returncode,
            wall_seconds=wall,
            raw_output=stdout,
        )
    return _result_from(data, proc.returncode, wall, stdout, stderr)
