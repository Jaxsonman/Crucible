import json
import os
import threading
import time
from pathlib import Path

import pytest

from crucible import executor
from crucible.configs import Config

FAKE = Path(__file__).parent / "fake_claude.py"
FIXTURE = Path(__file__).parent / "fixtures" / "real_claude_output.json"
CFG = Config("sonnet", "medium")


@pytest.fixture(autouse=True)
def fake_bin(monkeypatch):
    monkeypatch.setenv("CRUCIBLE_CLAUDE_BIN", str(FAKE))
    monkeypatch.delenv("CRUCIBLE_ISOLATION", raising=False)


def run(tmp_path, scenario="success", timeout_s=30, **kw):
    os.environ["FAKE_CLAUDE_SCENARIO"] = scenario
    try:
        return executor.execute("do it", CFG, tmp_path, 7, timeout_s, **kw)
    finally:
        del os.environ["FAKE_CLAUDE_SCENARIO"]


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def wait_gone(*pids, limit=5):
    end = time.monotonic() + limit
    while time.monotonic() < end:
        if not any(alive(p) for p in pids):
            return True
        time.sleep(0.05)
    return False


def test_success_sums_tokens_across_models(tmp_path):
    r = run(tmp_path)
    assert r.status == "completed" and r.error is None and r.exit_code == 0
    assert r.tokens == {"input": 1200, "output": 600, "cache_creation": 2400, "cache_read": 3600}
    assert len(r.tokens_by_model) == 2
    assert r.resolved_model == "claude-opus-5-5"  # the model with more output tokens
    assert r.tokens_by_model[r.resolved_model]["output"] == 500
    assert r.subtype == "success" and r.num_turns == 5
    assert r.session_id == "fake-session-0001"
    assert r.reported_duration_ms == 4200 and r.reported_cost_usd == 0.0168
    assert r.wall_seconds > 0
    assert json.loads(r.raw_output)["type"] == "result"


def test_result_text_matches_fake(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_RESULT", "custom reply")
    assert run(tmp_path).result_text == "custom reply"


def test_fake_model_override(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODEL", "mystery-model-9")
    r = run(tmp_path)
    assert r.resolved_model == "mystery-model-9"
    assert "mystery-model-9" in r.tokens_by_model


def test_fake_writes_files_into_cwd(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_WRITE", json.dumps({"a/b.txt": "hi"}))
    run(tmp_path)
    assert (tmp_path / "a" / "b.txt").read_text() == "hi"


def argv_of(tmp_path, monkeypatch, isolation=None):
    f = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_CLAUDE_ARGV_FILE", str(f))
    if isolation:
        monkeypatch.setenv("CRUCIBLE_ISOLATION", isolation)
    run(tmp_path, max_budget_usd=2.5)
    return json.loads(f.read_text())


def test_argv_matches_docs(tmp_path, monkeypatch):
    argv = argv_of(tmp_path, monkeypatch)
    assert argv[:2] == ["-p", "do it"]

    def val(flag):
        return argv[argv.index(flag) + 1]

    assert val("--model") == "sonnet"
    assert val("--effort") == "medium"
    assert val("--output-format") == "json"
    assert val("--permission-mode") == "bypassPermissions"
    assert val("--max-turns") == "7"
    assert val("--max-budget-usd") == "2.5"
    for flag in ("--no-session-persistence", "--strict-mcp-config", "--safe-mode"):
        assert flag in argv
    assert "--bare" not in argv
    assert "--fallback-model" not in argv


def test_bare_isolation(tmp_path, monkeypatch):
    argv = argv_of(tmp_path, monkeypatch, isolation="bare")
    assert "--bare" in argv and "--safe-mode" not in argv
    assert "--fallback-model" not in argv


def test_max_turns_is_error(tmp_path):
    r = run(tmp_path, "max_turns")
    assert r.status == "error" and r.subtype == "error_max_turns" and r.error


def test_crash_is_error_with_stderr(tmp_path):
    r = run(tmp_path, "crash")
    assert r.status == "error" and "boom" in r.error and r.exit_code == 1


def test_garbage_is_error(tmp_path):
    r = run(tmp_path, "garbage")
    assert r.status == "error" and r.error and r.raw_output.startswith("this is not json")


def test_missing_binary_is_error_not_raise(tmp_path, monkeypatch):
    monkeypatch.setenv("CRUCIBLE_CLAUDE_BIN", str(tmp_path / "nope"))
    r = executor.execute("p", CFG, tmp_path, 1, 5)
    assert r.status == "error" and "could not start" in r.error


def test_parses_real_cli_output():
    data = executor.parse_output(FIXTURE.read_text())
    r = executor._result_from(data, 0, 1.0, FIXTURE.read_text(), "")
    assert r.status == "completed" and r.subtype == "success"
    assert r.result_text == "ok" and r.num_turns == 1
    assert r.resolved_model == "claude-sonnet-5-5"
    assert r.tokens == {"input": 2, "output": 4, "cache_creation": 4769, "cache_read": 10345}
    assert r.reported_cost_usd == 0.021189 and r.session_id


def test_hang_times_out_and_grandchild_dies(tmp_path, monkeypatch):
    pid_file = tmp_path / "pids"
    monkeypatch.setenv("FAKE_CLAUDE_PID_FILE", str(pid_file))
    t0 = time.monotonic()
    r = run(tmp_path, "hang", timeout_s=2)
    assert time.monotonic() - t0 < 15
    assert r.status == "timeout"
    fake_pid, grandchild = map(int, pid_file.read_text().split())
    assert wait_gone(fake_pid, grandchild)


def test_keyboard_interrupt_kills_group(tmp_path, monkeypatch):
    import signal

    pid_file = tmp_path / "pids"
    monkeypatch.setenv("FAKE_CLAUDE_PID_FILE", str(pid_file))

    def interrupter():
        end = time.monotonic() + 10
        while time.monotonic() < end:
            if pid_file.exists() and pid_file.read_text().count(" "):
                break
            time.sleep(0.05)
        time.sleep(0.2)
        # a real SIGINT (like Ctrl-C) wakes the blocked wait; _thread.interrupt_main() does not
        os.kill(os.getpid(), signal.SIGINT)

    threading.Thread(target=interrupter, daemon=True).start()
    with pytest.raises(KeyboardInterrupt):
        run(tmp_path, "hang", timeout_s=60)
    fake_pid, grandchild = map(int, pid_file.read_text().split())
    assert wait_gone(fake_pid, grandchild)
