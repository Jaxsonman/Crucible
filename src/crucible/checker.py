"""Run a task's `check` script against an attempt workspace."""

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from crucible import executor
from crucible.task import Task

DEFAULT_TIMEOUT_S = 300.0


@dataclass
class CheckResult:
    ran: bool = False
    passed: bool | None = None
    exit_code: int | None = None
    seconds: float | None = None
    log: str = ""

    def as_record(self) -> dict:
        return {
            "ran": self.ran,
            "passed": self.passed,
            "exit_code": self.exit_code,
            "seconds": None if self.seconds is None else round(self.seconds, 3),
        }


def run_check(task: Task, workspace: Path, timeout_s: float = DEFAULT_TIMEOUT_S) -> CheckResult:
    """Run the task's check with the workspace as cwd. Never raises for a failing check."""
    script = task.check_path
    if script is None:
        return CheckResult()
    env = {**os.environ, "CRUCIBLE_TASK_DIR": str(task.task_dir)}
    start = time.monotonic()
    try:
        proc = subprocess.Popen(
            [str(script)],
            cwd=workspace,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        )
    except OSError as e:
        return CheckResult(
            ran=True, passed=False, seconds=time.monotonic() - start,
            log=f"could not start check {str(script)!r}: {e}\n",
        )
    try:
        out, _ = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        out, _ = executor._terminate_group(proc)
        return CheckResult(
            ran=True, passed=False, seconds=time.monotonic() - start,
            log=f"{out}\ncheck timed out after {timeout_s}s and was killed\n",
        )
    except BaseException:
        executor._terminate_group(proc)
        raise
    executor._killpg(proc.pid, signal.SIGKILL)  # stray grandchildren
    return CheckResult(
        ran=True, passed=proc.returncode == 0, exit_code=proc.returncode,
        seconds=time.monotonic() - start, log=out or "",
    )
