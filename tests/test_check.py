import json
import os
import shutil
import time
from pathlib import Path

import pytest

from crucible import records
from crucible.cli import main

REPO_ROOT = Path(__file__).resolve().parent.parent
FAKE = Path(__file__).parent / "fake_claude.py"

CORRECT_FIZZBUZZ = '''def fizzbuzz(n):
    if n < 0:
        raise ValueError("n must be non-negative")
    out = []
    for i in range(1, n + 1):
        out.append("FizzBuzz" if i % 15 == 0 else "Fizz" if i % 3 == 0 else "Buzz" if i % 5 == 0 else str(i))
    return out
'''


@pytest.fixture(autouse=True)
def setup(crucible_home, monkeypatch):
    shutil.copytree(REPO_ROOT / "tasks" / "example-fizzbuzz", crucible_home / "tasks" / "example-fizzbuzz")
    monkeypatch.setenv("CRUCIBLE_CLAUDE_BIN", str(FAKE))
    monkeypatch.delenv("CRUCIBLE_ISOLATION", raising=False)
    monkeypatch.delenv("FAKE_CLAUDE_WRITE", raising=False)


def tdir(home):
    return str(home / "tasks" / "example-fizzbuzz")


def run(home, *extra):
    return main(["run", tdir(home), "--configs", "sonnet:medium", *extra])


def only_record():
    (r,) = records.load_records("example-fizzbuzz")
    return r


def attempt_dir(home):
    (batch,) = (home / "results" / "example-fizzbuzz").iterdir()
    return batch / "sonnet-medium-1"


def test_untouched_fixture_fails_check(crucible_home):
    assert run(crucible_home) == 0
    r = only_record()
    assert r["check"]["ran"] is True and r["check"]["passed"] is False
    assert r["check"]["exit_code"] not in (0, None)
    assert r["success"] is False
    log = (attempt_dir(crucible_home) / "check.log").read_text()
    assert "NotImplementedError" in log  # stderr captured


def test_correct_fizzbuzz_passes_check(crucible_home, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_WRITE", json.dumps({"fizzbuzz.py": CORRECT_FIZZBUZZ}))
    assert run(crucible_home) == 0
    r = only_record()
    assert r["check"]["ran"] is True and r["check"]["passed"] is True
    assert r["check"]["exit_code"] == 0 and r["check"]["seconds"] > 0
    assert r["check"]["timed_out"] is False
    assert r["success"] is True
    assert "all assertions passed" in (attempt_dir(crucible_home) / "check.log").read_text()


def test_check_time_not_in_wall_seconds(crucible_home, monkeypatch):
    (crucible_home / "tasks" / "example-fizzbuzz" / "check.sh").write_text("#!/bin/sh\nsleep 1.5\n")
    assert run(crucible_home) == 0
    r = only_record()
    assert r["check"]["seconds"] >= 1.5 and r["wall_seconds"] < 1.5


def test_no_check_means_not_ran_and_success_null(crucible_home):
    tdir_ = crucible_home / "tasks" / "example-fizzbuzz"
    md = tdir_ / "task.md"
    md.write_text(md.read_text().replace("check: ./check.sh\n", ""))
    assert run(crucible_home) == 0
    r = only_record()
    assert r["check"] == {"ran": False, "passed": None, "exit_code": None, "seconds": None,
                          "timed_out": False}
    assert r["success"] is None
    assert not (attempt_dir(crucible_home) / "check.log").exists()


def test_hanging_check_is_killed_and_recorded_failed(crucible_home, tmp_path):
    pid_file = tmp_path / "grandchild.pid"
    script = crucible_home / "tasks" / "example-fizzbuzz" / "check.sh"
    script.write_text(f"#!/bin/sh\nsleep 600 &\necho $! > {pid_file}\nwait\n")
    t0 = time.monotonic()
    assert run(crucible_home, "--check-timeout", "2") == 0
    assert time.monotonic() - t0 < 30
    r = only_record()
    assert r["check"]["ran"] is True and r["check"]["passed"] is False
    assert r["check"]["exit_code"] is None and r["success"] is False
    assert r["check"]["timed_out"] is True
    assert 2 <= r["check"]["seconds"] < 20
    assert "timed out" in (attempt_dir(crucible_home) / "check.log").read_text()
    pid = int(pid_file.read_text())
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        os.kill(pid, 9)
        pytest.fail("check's grandchild survived the timeout")


def test_check_runs_for_error_attempt_but_success_false(crucible_home, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "crash")
    monkeypatch.setenv("FAKE_CLAUDE_WRITE", json.dumps({"fizzbuzz.py": CORRECT_FIZZBUZZ}))
    assert run(crucible_home) == 0
    r = only_record()
    assert r["status"] == "error"
    assert r["check"]["ran"] is True and r["check"]["passed"] is True
    assert r["success"] is False


def test_check_gets_task_dir_and_workspace_cwd(crucible_home):
    script = crucible_home / "tasks" / "example-fizzbuzz" / "check.sh"
    script.write_text('#!/bin/sh\necho "dir=$CRUCIBLE_TASK_DIR"\necho "cwd=$(pwd -P)"\nls\n')
    assert run(crucible_home) == 0
    log = (attempt_dir(crucible_home) / "check.log").read_text()
    assert f"dir={crucible_home / 'tasks' / 'example-fizzbuzz'}" in log
    assert "fizzbuzz.py" in log and "crucible-work" not in log.split("cwd=")[0]
    assert only_record()["check"]["passed"] is True


def test_check_log_is_redacted(crucible_home, monkeypatch):
    monkeypatch.setenv("FAKE_API_KEY", "sk-test-1234567890")
    script = crucible_home / "tasks" / "example-fizzbuzz" / "check.sh"
    script.write_text('#!/bin/sh\necho "key=$FAKE_API_KEY"\n')
    assert run(crucible_home) == 0
    log = (attempt_dir(crucible_home) / "check.log").read_text()
    assert "sk-test-1234567890" not in log and "[REDACTED:FAKE_API_KEY]" in log


def test_check_timeout_must_be_positive(crucible_home, capsys):
    assert run(crucible_home, "--check-timeout", "0") == 1
    assert "--check-timeout" in capsys.readouterr().err


def _spy_timeouts(monkeypatch):
    from crucible import checker
    seen = []
    real = checker.run_check
    monkeypatch.setattr(checker, "run_check",
                        lambda t, w, timeout=checker.DEFAULT_TIMEOUT_S:
                        (seen.append(timeout), real(t, w, timeout))[1])
    return seen


def _set_check_minutes(home, minutes):
    md = home / "tasks" / "example-fizzbuzz" / "task.md"
    text = md.read_text()
    md.write_text(text.replace("check: ./check.sh\n", f"check: ./check.sh\ncheck_timeout_minutes: {minutes}\n", 1))


def test_run_uses_task_check_timeout_minutes(crucible_home, monkeypatch):
    seen = _spy_timeouts(monkeypatch)
    _set_check_minutes(crucible_home, 45)
    assert run(crucible_home) == 0
    assert seen == [2700.0]


def test_run_cli_check_timeout_beats_the_task(crucible_home, monkeypatch):
    seen = _spy_timeouts(monkeypatch)
    _set_check_minutes(crucible_home, 45)
    assert run(crucible_home, "--check-timeout", "90") == 0
    assert seen == [90.0]


def test_run_defaults_to_300_without_either(crucible_home, monkeypatch):
    seen = _spy_timeouts(monkeypatch)
    assert run(crucible_home) == 0
    assert seen == [300.0]
