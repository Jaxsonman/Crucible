import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from crucible import prompt, records
from crucible.cli import main
from crucible.task import load_task

REPO_ROOT = Path(__file__).resolve().parent.parent
FAKE = Path(__file__).parent / "fake_claude.py"


@pytest.fixture(autouse=True)
def setup(crucible_home, monkeypatch):
    shutil.copytree(REPO_ROOT / "tasks" / "example-fizzbuzz", crucible_home / "tasks" / "example-fizzbuzz")
    monkeypatch.setenv("CRUCIBLE_CLAUDE_BIN", str(FAKE))
    monkeypatch.delenv("CRUCIBLE_ISOLATION", raising=False)
    monkeypatch.setenv(
        "FAKE_CLAUDE_WRITE", json.dumps({"fizzbuzz.py": "def fizzbuzz(n):\n    return []\n"})
    )


def task_dir(home):
    return str(home / "tasks" / "example-fizzbuzz")


def batches(home):
    return sorted((home / "results" / "example-fizzbuzz").iterdir())


def tree_hash(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def run_matrix(home, *extra):
    return main(["run", task_dir(home), "--configs", "opus:high,sonnet:medium", *extra])


def test_matrix_writes_four_attempts(crucible_home, capsys):
    assert run_matrix(crucible_home, "--repeat", "2") == 0
    (batch,) = batches(crucible_home)
    dirs = sorted(p.name for p in batch.iterdir())
    assert dirs == ["opus-high-1", "opus-high-2", "sonnet-medium-1", "sonnet-medium-2"]
    for d in dirs:
        for f in ("record.json", "output.json", "diff.patch"):
            assert (batch / d / f).is_file()
    assert "fizzbuzz.py" in (batch / "opus-high-1" / "diff.patch").read_text()
    out = capsys.readouterr().out.strip().splitlines()
    attempt_lines = [line for line in out if "completed" in line]
    assert len(attempt_lines) == 4
    assert "opus:high" in attempt_lines[0] and "$" in attempt_lines[0]


def test_record_schema_and_order(crucible_home):
    assert run_matrix(crucible_home) == 0
    (batch,) = batches(crucible_home)
    r = json.loads((batch / "opus-high-1" / "record.json").read_text())
    assert list(r) == [
        "schema_version", "task_id", "task_kind", "batch_id", "attempt", "config",
        "resolved_model", "prompt_sha256", "baseline_sha", "started_at", "wall_seconds",
        "status", "error", "num_turns", "session_id", "subtype", "reported_duration_ms",
        "tokens", "tokens_by_model", "cost_usd", "check", "judge", "success",
    ]
    assert r["schema_version"] == 1 and r["task_id"] == "example-fizzbuzz"
    assert r["task_kind"] == "feature" and r["batch_id"] == batch.name
    assert r["attempt"] == 1 and r["config"] == {"model": "opus", "effort": "high"}
    assert r["resolved_model"] == "claude-opus-5-5"
    assert r["status"] == "completed" and r["error"] is None
    assert r["success"] is False  # stub fizzbuzz fails the example check (see test_check.py)
    assert list(r["check"]) == ["ran", "passed", "exit_code", "seconds"]
    assert r["check"]["ran"] is True and r["check"]["passed"] is False
    assert r["judge"]["ran"] is False and r["judge"]["passed"] is None
    assert r["tokens"] == {"input": 1200, "output": 600, "cache_creation": 2400, "cache_read": 3600}
    assert r["cost_usd"]["reported"] == 0.0168 and r["cost_usd"]["computed"] > 0
    assert r["wall_seconds"] > 0 and r["started_at"].endswith("Z")
    assert r["num_turns"] == 5 and r["session_id"] == "fake-session-0001"
    assert r["reported_duration_ms"] == 4200 and r["subtype"] == "success"
    assert len(r["baseline_sha"]) == 40
    assert json.loads((batch / "opus-high-1" / "output.json").read_text())["type"] == "result"


def test_timeouts_and_turns_come_from_task(crucible_home, tmp_path, monkeypatch):
    argv_file = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_CLAUDE_ARGV_FILE", str(argv_file))
    assert main(["run", task_dir(crucible_home), "--configs", "sonnet:medium",
                 "--max-budget-usd", "1.5"]) == 0
    argv = json.loads(argv_file.read_text())
    assert argv[argv.index("--max-turns") + 1] == "30"
    assert argv[argv.index("--max-budget-usd") + 1] == "1.5"


def test_secret_redacted_in_output(crucible_home, monkeypatch):
    monkeypatch.setenv("FAKE_API_KEY", "sk-test-1234567890")
    monkeypatch.setenv("FAKE_CLAUDE_RESULT", "leaked sk-test-1234567890 here")
    monkeypatch.setenv("FAKE_CLAUDE_WRITE", json.dumps({"n.txt": "sk-test-1234567890"}))
    assert main(["run", task_dir(crucible_home), "--configs", "sonnet:medium"]) == 0
    (batch,) = batches(crucible_home)
    for f in batch.rglob("*"):
        if f.is_file():
            assert "sk-test-1234567890" not in f.read_text(), f
    assert "[REDACTED:FAKE_API_KEY]" in (batch / "sonnet-medium-1" / "output.json").read_text()
    assert "[REDACTED:FAKE_API_KEY]" in (batch / "sonnet-medium-1" / "diff.patch").read_text()


def test_unknown_model_gives_null_cost_and_warning(crucible_home, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_CLAUDE_MODEL", "claude-mystery-9")
    assert main(["run", task_dir(crucible_home), "--configs", "sonnet:medium"]) == 0
    (batch,) = batches(crucible_home)
    r = json.loads((batch / "sonnet-medium-1" / "record.json").read_text())
    assert r["cost_usd"]["computed"] is None and r["cost_usd"]["reported"] == 0.0168
    assert r["status"] == "completed"
    assert "claude-mystery-9" in capsys.readouterr().err


def test_shared_prompt_and_baseline(crucible_home):
    assert run_matrix(crucible_home, "--repeat", "2") == 0
    recs = records.load_records("example-fizzbuzz")
    assert len(recs) == 4
    assert len({r["prompt_sha256"] for r in recs}) == 1
    assert len({r["baseline_sha"] for r in recs}) == 1
    expected = prompt.prompt_sha256(prompt.build_prompt(load_task(task_dir(crucible_home))))
    assert recs[0]["prompt_sha256"] == expected


def test_second_run_new_batch_first_untouched(crucible_home):
    assert run_matrix(crucible_home) == 0
    (first,) = batches(crucible_home)
    before = tree_hash(first)
    time.sleep(1.1)
    assert run_matrix(crucible_home) == 0
    assert len(batches(crucible_home)) == 2
    assert tree_hash(first) == before


def test_crash_on_one_config_is_recorded_and_batch_continues(crucible_home, tmp_path, monkeypatch):
    wrapper = tmp_path / "claude-wrapper"
    wrapper.write_text(
        f'#!/bin/sh\ncase "$*" in *"--model opus"*) echo boom >&2; exit 1;; esac\n'
        f'exec "{sys.executable}" "{FAKE}" "$@"\n'
    )
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("CRUCIBLE_CLAUDE_BIN", str(wrapper))
    assert run_matrix(crucible_home) == 0
    by = {(r["config"]["model"]): r for r in records.load_records()}
    assert by["opus"]["status"] == "error" and "boom" in by["opus"]["error"]
    assert by["opus"]["success"] is False
    assert by["sonnet"]["status"] == "completed"


def test_dry_run_calls_fake_zero_times(crucible_home, tmp_path, monkeypatch, capsys):
    argv_file = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_CLAUDE_ARGV_FILE", str(argv_file))
    assert run_matrix(crucible_home, "--repeat", "2", "--dry-run") == 0
    assert not argv_file.exists()
    assert not list((crucible_home / "results").iterdir())
    out = capsys.readouterr().out
    sha = prompt.prompt_sha256(prompt.build_prompt(load_task(task_dir(crucible_home))))
    assert sha in out and out.count("opus:high") == 2 and out.count("sonnet:medium") == 2


def test_invalid_config_fails_before_any_attempt(crucible_home, tmp_path, monkeypatch, capsys):
    argv_file = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_CLAUDE_ARGV_FILE", str(argv_file))
    assert main(["run", task_dir(crucible_home), "--configs", "sonnet:medium,opus:turbo"]) == 1
    assert "opus:turbo" in capsys.readouterr().err
    assert not argv_file.exists()
    assert not list((crucible_home / "results").iterdir())


def test_matrix_option(crucible_home):
    (crucible_home / "configs" / "default.yaml").write_text("configs: [sonnet:low]\n")
    assert main(["run", task_dir(crucible_home), "--matrix", "default"]) == 0
    assert [r["config"]["effort"] for r in records.load_records()] == ["low"]


def test_requires_configs_or_matrix(crucible_home, capsys):
    with pytest.raises(SystemExit) as e:
        main(["run", task_dir(crucible_home)])
    assert e.value.code == 2


def test_invalid_task_and_repeat(crucible_home, capsys):
    assert main(["run", str(crucible_home / "tasks" / "nope"), "--configs", "sonnet:low"]) == 1
    assert main(["run", task_dir(crucible_home), "--configs", "sonnet:low", "--repeat", "0"]) == 1


def test_keep_workspaces(crucible_home):
    work = Path(os.environ["CRUCIBLE_WORK_DIR"])
    assert main(["run", task_dir(crucible_home), "--configs", "sonnet:low"]) == 0
    assert not any(work.rglob("fizzbuzz.py"))
    assert main(["run", task_dir(crucible_home), "--configs", "sonnet:low", "--keep-workspaces"]) == 0
    assert len(list(work.rglob("fizzbuzz.py"))) == 1


def test_refuses_existing_attempt_dir(crucible_home, monkeypatch):
    from crucible import runner

    monkeypatch.setattr(runner, "new_batch_id", lambda: "20260101T000000Z-aaaa")
    assert main(["run", task_dir(crucible_home), "--configs", "sonnet:low"]) == 0
    before = tree_hash(crucible_home / "results")
    assert main(["run", task_dir(crucible_home), "--configs", "sonnet:low"]) == 1
    assert tree_hash(crucible_home / "results") == before


def test_ctrl_c_records_interrupted_and_stops(crucible_home, tmp_path):
    pid_file = tmp_path / "pids"
    env = {**os.environ, "FAKE_CLAUDE_SCENARIO": "hang", "FAKE_CLAUDE_PID_FILE": str(pid_file)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "crucible.cli", "run", task_dir(crucible_home),
         "--configs", "opus:high,sonnet:medium", "--repeat", "2"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    deadline = time.monotonic() + 30
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert pid_file.exists(), "fake never started"
    time.sleep(0.3)
    proc.send_signal(signal.SIGINT)
    proc.communicate(timeout=60)
    assert proc.returncode == 130
    recs = records.load_records("example-fizzbuzz")
    assert len(recs) == 1
    assert recs[0]["status"] == "error" and recs[0]["error"] == "interrupted"
    assert recs[0]["success"] is False
    assert recs[0]["config"] == {"model": "opus", "effort": "high"}


def test_non_utf8_workspace_file_is_recorded(crucible_home, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_WRITE_LATIN1", json.dumps({"caf.txt": "caf\xe9\n"}))
    assert run_matrix(crucible_home) == 0
    recs = records.load_records()
    assert [r["status"] for r in recs] == ["completed", "completed"]
    (batch,) = batches(crucible_home)
    assert "caf" in (batch / "opus-high-1" / "diff.patch").read_text()


def test_non_utf8_claude_stdout_does_not_crash(crucible_home, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_LATIN1_STDOUT", "1")
    assert run_matrix(crucible_home) == 0
    assert len(records.load_records()) == 2


@pytest.mark.parametrize("configs", ["opus:high,opus:high", "opus:high,sonnet:low,opus:high"])
def test_duplicate_configs_rejected_up_front(crucible_home, tmp_path, monkeypatch, capsys, configs):
    argv_file = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_CLAUDE_ARGV_FILE", str(argv_file))
    assert main(["run", task_dir(crucible_home), "--configs", configs]) == 1
    assert "duplicate" in capsys.readouterr().err
    assert not argv_file.exists()
    assert not list((crucible_home / "results").iterdir())


def test_duplicate_configs_in_matrix_rejected(crucible_home, capsys):
    (crucible_home / "configs" / "dup.yaml").write_text("configs: [opus:high, opus:high]\n")
    assert main(["run", task_dir(crucible_home), "--matrix", "dup"]) == 1
    assert "duplicate" in capsys.readouterr().err
    assert not list((crucible_home / "results").iterdir())


def test_unexpected_exception_is_recorded_and_batch_continues(crucible_home, monkeypatch):
    from crucible import workspace

    real = workspace.create
    calls = []

    def flaky(*a, **kw):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("disk exploded " + "x" * 800)
        return real(*a, **kw)

    monkeypatch.setattr(workspace, "create", flaky)
    assert run_matrix(crucible_home) == 0
    by = {r["config"]["model"]: r for r in records.load_records()}
    assert by["opus"]["status"] == "error" and "disk exploded" in by["opus"]["error"]
    assert len(by["opus"]["error"]) <= 500 and by["opus"]["success"] is False
    assert by["sonnet"]["status"] == "completed"
    (batch,) = batches(crucible_home)
    assert (batch / "opus-high-1" / "output.json").is_file()


def test_dry_run_says_whether_task_has_a_check(crucible_home, capsys):
    assert main(["run", task_dir(crucible_home), "--configs", "sonnet:low", "--dry-run"]) == 0
    assert "check: ./check.sh" in capsys.readouterr().out.splitlines()
    md = crucible_home / "tasks" / "example-fizzbuzz" / "task.md"
    md.write_text(md.read_text().replace("check: ./check.sh\n", ""))
    assert main(["run", task_dir(crucible_home), "--configs", "sonnet:low", "--dry-run"]) == 0
    assert "check: none" in capsys.readouterr().out.splitlines()


def set_front(home, extra: str) -> None:
    """Add front-matter lines to the copied example task (after `max_turns: 30`)."""
    md = home / "tasks" / "example-fizzbuzz" / "task.md"
    md.write_text(md.read_text().replace("max_turns: 30\n", f"max_turns: 30\n{extra}"))


def test_task_max_budget_used_unless_flag_given(crucible_home, tmp_path, monkeypatch):
    set_front(crucible_home, "max_budget_usd: 1.25\n")
    argv_file = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_CLAUDE_ARGV_FILE", str(argv_file))
    assert main(["run", task_dir(crucible_home), "--configs", "sonnet:medium"]) == 0
    argv = json.loads(argv_file.read_text())
    assert argv[argv.index("--max-budget-usd") + 1] == "1.25"
    # an explicit flag wins, even when it equals the CLI default
    assert main(["run", task_dir(crucible_home), "--configs", "sonnet:medium",
                 "--max-budget-usd", "5"]) == 0
    argv = json.loads(argv_file.read_text())
    assert argv[argv.index("--max-budget-usd") + 1] == "5.0"


def test_default_budget_without_task_value(crucible_home, tmp_path, monkeypatch):
    argv_file = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_CLAUDE_ARGV_FILE", str(argv_file))
    assert main(["run", task_dir(crucible_home), "--configs", "sonnet:medium"]) == 0
    argv = json.loads(argv_file.read_text())
    assert argv[argv.index("--max-budget-usd") + 1] == "5.0"


def test_dry_run_shows_effective_budget(crucible_home, capsys):
    def dry(*extra):
        assert main(["run", task_dir(crucible_home), "--configs", "sonnet:low", "--dry-run",
                     *extra]) == 0
        return capsys.readouterr().out.splitlines()

    assert "max budget per attempt: $5 (default)" in dry()
    set_front(crucible_home, "max_budget_usd: 1.25\n")
    assert "max budget per attempt: $1.25 (task)" in dry()
    assert "max budget per attempt: $0.5 (--max-budget-usd)" in dry("--max-budget-usd", "0.5")


def test_non_positive_budget_flag_rejected(crucible_home, capsys):
    assert main(["run", task_dir(crucible_home), "--configs", "sonnet:low",
                 "--max-budget-usd", "0"]) == 1
    assert "--max-budget-usd must be positive" in capsys.readouterr().err
