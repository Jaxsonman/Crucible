import hashlib
import json
import shutil
from pathlib import Path

import pytest

from crucible import executor, judge, records
from crucible.cli import main
from crucible.configs import Config
from crucible.task import load_task

REPO_ROOT = Path(__file__).resolve().parent.parent
FAKE = Path(__file__).parent / "fake_claude.py"
TASK_ID = "example-fizzbuzz"

CORRECT_FIZZBUZZ = '''def fizzbuzz(n):
    if n < 0:
        raise ValueError("n must be non-negative")
    return ["FizzBuzz" if i % 15 == 0 else "Fizz" if i % 3 == 0 else "Buzz" if i % 5 == 0 else str(i)
            for i in range(1, n + 1)]
'''


@pytest.fixture(autouse=True)
def setup(crucible_home, monkeypatch):
    shutil.copytree(REPO_ROOT / "tasks" / TASK_ID, crucible_home / "tasks" / TASK_ID)
    monkeypatch.setenv("CRUCIBLE_CLAUDE_BIN", str(FAKE))
    monkeypatch.delenv("CRUCIBLE_ISOLATION", raising=False)
    monkeypatch.delenv("FAKE_CLAUDE_RESULT_SEQ_FILE", raising=False)
    monkeypatch.setenv("FAKE_CLAUDE_WRITE", json.dumps({"fizzbuzz.py": CORRECT_FIZZBUZZ}))


def tdir(home):
    return str(home / "tasks" / TASK_ID)


def criteria(home):
    return load_task(tdir(home)).criteria


def verdict(home, passed=None, fence=False):
    cs = criteria(home)
    passed = passed or [True] * len(cs)
    text = json.dumps({"criteria": [
        {"criterion": c, "passed": p, "reason": f"because {i}"}
        for i, (c, p) in enumerate(zip(cs, passed))
    ]})
    return f"```json\n{text}\n```" if fence else text


def run(home, *extra, configs="sonnet:medium"):
    return main(["run", tdir(home), "--configs", configs, *extra])


def only_record():
    (r,) = records.load_records(TASK_ID)
    return r


def attempt_dirs(home):
    (batch,) = (home / "results" / TASK_ID).iterdir()
    return sorted(p for p in batch.iterdir() if p.is_dir())


def tree_hash(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*")) if p.is_file()
    }


def seq(tmp_path, monkeypatch, results):
    f = tmp_path / "seq.json"
    f.write_text(json.dumps(results))
    monkeypatch.setenv("FAKE_CLAUDE_RESULT_SEQ_FILE", str(f))
    return f


# --- acceptance criteria -------------------------------------------------------------------


def test_run_judge_writes_judge_json_and_fields_cost_unchanged(crucible_home, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_RESULT", verdict(crucible_home))
    assert run(crucible_home) == 0
    r = only_record()
    n = len(criteria(crucible_home))
    assert r["judge"]["ran"] is False  # baseline: no --judge, nothing judged
    # now with --judge on a fresh batch
    import time

    time.sleep(1.1)
    assert run(crucible_home, "--judge") == 0
    judged = [x for x in records.load_records(TASK_ID) if x["judge"]["ran"]]
    plain = [x for x in records.load_records(TASK_ID) if not x["judge"]["ran"]]
    (j,) = judged
    assert j["judge"] == {
        "ran": True, "passed": True, "criteria_passed": n, "criteria_total": n,
        "judge_config": "sonnet:medium", "judge_cost_usd": pytest.approx(j["judge"]["judge_cost_usd"]),
    }
    assert j["judge"]["judge_cost_usd"] > 0
    assert j["success"] is True  # check passed and judge passed
    assert j["cost_usd"] == plain[0]["cost_usd"]  # judge cost not added to attempt cost
    d = [p for p in (crucible_home / "results" / TASK_ID).rglob("judge.json")]
    assert len(d) == 1
    data = json.loads(d[0].read_text())
    assert data["passed"] is True and len(data["criteria"]) == n
    assert data["criteria"][0]["criterion"] == criteria(crucible_home)[0]
    assert data["criteria"][0]["reason"] == "because 0"
    assert data["judge_config"] == "sonnet:medium" and data["error"] is None
    assert data["raw_outputs"] and verdict(crucible_home) in data["raw_outputs"][0]


def test_judge_cost_priced_from_judge_tokens(crucible_home, monkeypatch):
    from crucible import pricing

    monkeypatch.setenv("FAKE_CLAUDE_RESULT", verdict(crucible_home))
    assert run(crucible_home, "--judge") == 0
    r = only_record()
    expected, _ = pricing.cost_usd(r["tokens_by_model"])  # fake gives the same usage to judge
    assert r["judge"]["judge_cost_usd"] == pytest.approx(expected)


def test_judge_prompt_has_criteria_diff_checklog_and_untrusted_warning(
    crucible_home, tmp_path, monkeypatch
):
    argv_file = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_CLAUDE_ARGV_FILE", str(argv_file))
    monkeypatch.setenv("FAKE_CLAUDE_RESULT", verdict(crucible_home))
    assert run(crucible_home, "--judge") == 0
    argv = json.loads(argv_file.read_text())  # the last call is the judge
    prompt_text = argv[argv.index("-p") + 1]
    for c in criteria(crucible_home):
        assert c in prompt_text
    (d,) = attempt_dirs(crucible_home)
    diff = (d / "diff.patch").read_text()
    assert diff and diff in prompt_text
    assert (d / "check.log").read_text().strip() in prompt_text
    lowered = prompt_text.lower()
    assert "untrusted" in lowered and "not instructions" in lowered
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert argv[argv.index("--effort") + 1] == "medium"
    assert argv[argv.index("--max-turns") + 1] == "3"


def test_prompt_omits_checklog_when_absent_and_truncates_huge_diff():
    cs = ["alpha criterion", "beta criterion"]
    p = judge.build_prompt(cs, "small diff", None)
    assert "small diff" in p and "check.log" not in p.lower().replace("check log", "")
    big = "x" * (judge.MAX_DIFF_CHARS + 5000)
    p = judge.build_prompt(cs, big, "log")
    assert big not in p
    assert "x" * judge.MAX_DIFF_CHARS in p
    assert "truncated" in p.lower() and "5000" in p


def test_runs_in_empty_temp_dir_with_three_turns(crucible_home, monkeypatch):
    calls = []
    real = executor.execute

    def spy(prompt, config, cwd, max_turns, timeout_s, max_budget_usd=5.0):
        if "untrusted" in prompt.lower():
            calls.append((config, list(Path(cwd).iterdir()), max_turns, Path(cwd)))
        return real(prompt, config, cwd, max_turns, timeout_s, max_budget_usd)

    monkeypatch.setattr(executor, "execute", spy)
    monkeypatch.setenv("FAKE_CLAUDE_RESULT", verdict(crucible_home))
    assert run(crucible_home, "--judge") == 0
    ((cfg, contents, turns, cwd),) = calls
    assert cfg == Config("sonnet", "medium") and contents == [] and turns == 3
    assert not cwd.exists()  # temp dir cleaned up


def test_failing_criterion_gives_judge_false_but_check_decides_success(crucible_home, monkeypatch):
    n = len(criteria(crucible_home))
    monkeypatch.setenv("FAKE_CLAUDE_RESULT", verdict(crucible_home, [True] * (n - 1) + [False]))
    assert run(crucible_home, "--judge") == 0
    r = only_record()
    assert r["check"]["passed"] is True
    assert r["judge"]["passed"] is False
    assert r["judge"]["criteria_passed"] == n - 1 and r["judge"]["criteria_total"] == n
    assert r["success"] is True  # the check ran, so its verdict is success; the judge is advisory


def test_code_fenced_verdict_is_accepted(crucible_home, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_RESULT", verdict(crucible_home, fence=True))
    assert run(crucible_home, "--judge") == 0
    assert only_record()["judge"]["passed"] is True


def test_garbage_twice_gives_null_and_check_decides_success(crucible_home, tmp_path, monkeypatch):
    f = seq(tmp_path, monkeypatch, ["not json at all", "still { not json"])
    assert run(crucible_home, "--judge") == 0
    r = only_record()
    assert r["judge"]["ran"] is True and r["judge"]["passed"] is None
    assert r["judge"]["criteria_passed"] is None
    assert r["success"] is True  # check passed; judge gave no verdict
    assert int(Path(str(f) + ".n").read_text()) == 3  # attempt + judge + one retry
    (jpath,) = (crucible_home / "results").rglob("judge.json")
    data = json.loads(jpath.read_text())
    assert data["passed"] is None and data["error"] and len(data["raw_outputs"]) == 2
    assert r["judge"]["judge_cost_usd"] > 0  # both tries are charged to the judge


def test_garbage_judge_with_failing_check_still_false(crucible_home, tmp_path, monkeypatch):
    monkeypatch.delenv("FAKE_CLAUDE_WRITE")
    seq(tmp_path, monkeypatch, ["nope"])
    assert run(crucible_home, "--judge") == 0
    r = only_record()
    assert r["judge"]["passed"] is None and r["check"]["passed"] is False
    assert r["success"] is False


def test_retry_once_recovers(crucible_home, tmp_path, monkeypatch):
    f = seq(tmp_path, monkeypatch, ["ok", "garbage", verdict(crucible_home)])
    assert run(crucible_home, "--judge") == 0
    r = only_record()
    assert r["judge"]["passed"] is True
    assert int(Path(str(f) + ".n").read_text()) == 3
    (jpath,) = (crucible_home / "results").rglob("judge.json")
    assert len(json.loads(jpath.read_text())["raw_outputs"]) == 2


@pytest.mark.parametrize("bad", ["wrong_count", "not_bool", "no_list", "array"])
def test_invalid_verdict_shapes_rejected(crucible_home, tmp_path, monkeypatch, bad):
    cs = criteria(crucible_home)
    entry = lambda c, p: {"criterion": c, "passed": p, "reason": "r"}
    payload = {
        "wrong_count": {"criteria": [entry(c, True) for c in cs[:-1]]},
        "not_bool": {"criteria": [entry(c, "yes") for c in cs]},
        "no_list": {"criteria": "all good"},
        "array": [entry(c, True) for c in cs],
    }[bad]
    seq(tmp_path, monkeypatch, [json.dumps(payload)])
    assert run(crucible_home, "--judge") == 0
    assert only_record()["judge"]["passed"] is None


def test_judge_config_option_recorded_and_validated(crucible_home, tmp_path, monkeypatch, capsys):
    argv_file = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_CLAUDE_ARGV_FILE", str(argv_file))
    monkeypatch.setenv("FAKE_CLAUDE_RESULT", verdict(crucible_home))
    assert run(crucible_home, "--judge", "--judge-config", "opus:high") == 0
    assert only_record()["judge"]["judge_config"] == "opus:high"
    argv = json.loads(argv_file.read_text())
    assert argv[argv.index("--model") + 1] == "opus"
    # invalid judge config fails before any attempt
    shutil.rmtree(crucible_home / "results" / TASK_ID)
    argv_file.unlink()
    assert run(crucible_home, "--judge", "--judge-config", "opus:turbo") == 1
    assert "opus:turbo" in capsys.readouterr().err
    assert not argv_file.exists()
    assert run(crucible_home, "--judge-config", "opus:high") == 1  # needs --judge
    assert not argv_file.exists()


def test_judge_files_are_redacted(crucible_home, monkeypatch):
    monkeypatch.setenv("FAKE_API_KEY", "sk-test-1234567890")
    cs = criteria(crucible_home)
    leaky = {"criteria": [
        {"criterion": c, "passed": True, "reason": "saw sk-test-1234567890"} for c in cs
    ]}
    monkeypatch.setenv("FAKE_CLAUDE_RESULT", json.dumps(leaky))
    assert run(crucible_home, "--judge") == 0
    for f in (crucible_home / "results").rglob("*"):
        if f.is_file():
            assert "sk-test-1234567890" not in f.read_text(), f
    (jpath,) = (crucible_home / "results").rglob("judge.json")
    assert "[REDACTED:FAKE_API_KEY]" in jpath.read_text()


# --- crucible judge ------------------------------------------------------------------------


def test_crucible_judge_skips_judged_and_touches_only_allowed_bytes(
    crucible_home, tmp_path, monkeypatch
):
    assert run(crucible_home, configs="opus:high,sonnet:medium") == 0
    dirs = attempt_dirs(crucible_home)
    assert [d.name for d in dirs] == ["opus-high-1", "sonnet-medium-1"]
    preexisting = '{"already": "judged"}\n'
    (dirs[0] / "judge.json").write_text(preexisting)
    results = crucible_home / "results"
    before = tree_hash(results)
    before_rec = json.loads((dirs[1] / "record.json").read_text())
    f = seq(tmp_path, monkeypatch, [verdict(crucible_home)])
    assert main(["judge", TASK_ID]) == 0
    assert int(Path(str(f) + ".n").read_text()) == 1  # only the unjudged attempt was judged
    after = tree_hash(results)
    changed = {k for k in after if before.get(k) != after[k]}
    rel = f"{TASK_ID}/{dirs[1].parent.name}/{dirs[1].name}"
    assert changed == {f"{rel}/judge.json", f"{rel}/record.json"}
    assert (dirs[0] / "judge.json").read_text() == preexisting
    after_rec = json.loads((dirs[1] / "record.json").read_text())
    diff_keys = {k for k in after_rec if after_rec[k] != before_rec[k]}
    assert diff_keys <= {"judge", "success"} and "judge" in diff_keys
    assert list(after_rec) == list(before_rec)
    assert after_rec["judge"]["ran"] is True and after_rec["judge"]["passed"] is True
    assert after_rec["cost_usd"] == before_rec["cost_usd"]
    assert after_rec["success"] is True
    # second invocation judges nothing
    assert main(["judge", TASK_ID]) == 0
    assert int(Path(str(f) + ".n").read_text()) == 1
    assert tree_hash(results) == after


def test_crucible_judge_batch_filter_and_config(crucible_home, monkeypatch):
    import time

    assert run(crucible_home) == 0
    time.sleep(1.1)
    assert run(crucible_home) == 0
    b1, b2 = sorted((crucible_home / "results" / TASK_ID).iterdir())
    monkeypatch.setenv("FAKE_CLAUDE_RESULT", verdict(crucible_home))
    assert main(["judge", TASK_ID, "--batch", b2.name, "--judge-config", "opus:high"]) == 0
    assert not list(b1.rglob("judge.json"))
    assert len(list(b2.rglob("judge.json"))) == 1
    (rec,) = [r for r in records.load_records(TASK_ID) if r["judge"]["ran"]]
    assert rec["batch_id"] == b2.name and rec["judge"]["judge_config"] == "opus:high"


def test_crucible_judge_errors(crucible_home, capsys):
    assert main(["judge", "no-such-task"]) == 1
    assert run(crucible_home) == 0
    assert main(["judge", TASK_ID, "--batch", "nope"]) == 1
    assert main(["judge", TASK_ID, "--judge-config", "opus:turbo"]) == 1
    assert "opus:turbo" in capsys.readouterr().err


def test_crucible_judge_garbage_leaves_success_to_check(crucible_home, tmp_path, monkeypatch):
    assert run(crucible_home) == 0
    seq(tmp_path, monkeypatch, ["garbage"])
    assert main(["judge", TASK_ID]) == 0
    r = only_record()
    assert r["judge"]["ran"] is True and r["judge"]["passed"] is None
    assert r["success"] is True


# --- non-completed attempts are never judged -----------------------------------------------


def test_run_judge_skips_errored_attempt(crucible_home, monkeypatch):
    judge_calls = []
    real = executor.execute

    def spy(prompt, *a, **kw):
        if "untrusted" in prompt.lower():
            judge_calls.append(prompt)
        return real(prompt, *a, **kw)

    monkeypatch.setattr(executor, "execute", spy)
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "crash")
    monkeypatch.setenv("FAKE_CLAUDE_RESULT", verdict(crucible_home))
    assert run(crucible_home, "--judge") == 0
    r = only_record()
    assert r["status"] == "error" and r["success"] is False
    assert r["judge"]["ran"] is False and r["judge"]["passed"] is None
    assert judge_calls == []
    assert not list((crucible_home / "results").rglob("judge.json"))


def test_crucible_judge_skips_errored_attempt_with_skip_line(
    crucible_home, tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "crash")
    assert run(crucible_home) == 0
    monkeypatch.delenv("FAKE_CLAUDE_SCENARIO")
    argv_file = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_CLAUDE_ARGV_FILE", str(argv_file))
    before = tree_hash(crucible_home / "results")
    capsys.readouterr()
    assert main(["judge", TASK_ID]) == 0
    out = capsys.readouterr().out
    assert not argv_file.exists()  # the fake was never called
    assert tree_hash(crucible_home / "results") == before
    assert "skip" in out.lower() and "sonnet-medium-1" in out and "error" in out
    assert "judged 0 attempt(s)" in out
