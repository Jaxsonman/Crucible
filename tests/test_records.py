import json

from crucible import records


def rec(**kw):
    base = {
        "status": "completed",
        "check": {"ran": False, "passed": None, "exit_code": None},
        "judge": {"ran": False, "passed": None},
    }
    base.update(kw)
    return base


def test_success_error_is_false_without_verdicts():
    assert records.compute_success(rec(status="error")) is False
    assert records.compute_success(rec(status="timeout")) is False


def test_success_error_is_false_even_if_check_passed():
    r = rec(status="error", check={"ran": True, "passed": True, "exit_code": 0})
    assert records.compute_success(r) is False


def test_success_null_when_no_verdicts():
    assert records.compute_success(rec()) is None
    r = rec(judge={"ran": True, "passed": None})
    assert records.compute_success(r) is None


def test_success_follows_verdicts():
    chk = lambda p: {"ran": True, "passed": p, "exit_code": 0 if p else 1}
    jdg = lambda p: {"ran": True, "passed": p}
    assert records.compute_success(rec(check=chk(True))) is True
    assert records.compute_success(rec(check=chk(False))) is False
    assert records.compute_success(rec(judge=jdg(True))) is True
    assert records.compute_success(rec(judge=jdg(False))) is False
    assert records.compute_success(rec(check=chk(True), judge=jdg(True))) is True
    assert records.compute_success(rec(check=chk(True), judge=jdg(False))) is True  # check wins
    assert records.compute_success(rec(check=chk(False), judge=jdg(True))) is False


def test_redact_replaces_secret_values(monkeypatch):
    monkeypatch.setenv("FAKE_API_KEY", "supersecret123")
    monkeypatch.setenv("MY_PASSWORD", "hunter2hunter2")
    monkeypatch.setenv("SOME_TOKEN", "short")  # < 8 chars: not redacted
    monkeypatch.setenv("PLAIN_VAR", "plainvalue99")  # name doesn't match
    out = records.redact("a supersecret123 b hunter2hunter2 c short d plainvalue99")
    assert "supersecret123" not in out and "[REDACTED:FAKE_API_KEY]" in out
    assert "[REDACTED:MY_PASSWORD]" in out
    assert "short" in out and "plainvalue99" in out


def test_redact_handles_json_escaped_values(monkeypatch):
    monkeypatch.setenv("FAKE_API_KEY", 'sec"ret\\value')
    text = json.dumps({"x": 'sec"ret\\value'})
    out = records.redact(text)
    assert "ret" not in out.replace("[REDACTED", "")
    assert json.loads(out) == {"x": "[REDACTED:FAKE_API_KEY]"}


def test_write_and_load_records(crucible_home):
    d = crucible_home / "results" / "t1" / "b1" / "sonnet-medium-1"
    r = {"task_id": "t1", "batch_id": "b1", "attempt": 1, "status": "completed"}
    records.write_record(d, r)
    d2 = crucible_home / "results" / "t2" / "b1" / "opus-high-1"
    records.write_record(d2, {"task_id": "t2", "batch_id": "b1", "attempt": 1})
    assert [x["task_id"] for x in records.load_records()] == ["t1", "t2"]
    assert [x["task_id"] for x in records.load_records("t1")] == ["t1"]
    assert records.load_records("nope") == []


def test_write_record_refuses_existing_dir(crucible_home):
    d = crucible_home / "results" / "t1" / "b1" / "x-1"
    records.write_record(d, {"a": 1})
    try:
        records.write_record(d, {"a": 2})
    except FileExistsError:
        pass
    else:
        raise AssertionError("expected FileExistsError")
    assert json.loads((d / "record.json").read_text()) == {"a": 1}


def test_check_ran_without_verdict_falls_back_to_judge():
    no_verdict = {"ran": True, "passed": None, "exit_code": None}
    assert records.compute_success(rec(check=no_verdict, judge={"ran": True, "passed": True})) is True
    assert records.compute_success(rec(check=no_verdict, judge={"ran": True, "passed": False})) is False
    assert records.compute_success(rec(check=no_verdict)) is None


def test_judge_disagrees_needs_two_verdicts():
    chk = lambda p: {"ran": True, "passed": p, "exit_code": 0 if p else 1}
    jdg = lambda p: {"ran": True, "passed": p}
    assert records.judge_disagrees(rec(check=chk(True), judge=jdg(False))) is True
    assert records.judge_disagrees(rec(check=chk(False), judge=jdg(True))) is True
    assert records.judge_disagrees(rec(check=chk(True), judge=jdg(True))) is False
    assert records.judge_disagrees(rec(check=chk(True))) is False  # judge did not run
    assert records.judge_disagrees(rec(check=chk(True), judge=jdg(None))) is False
    no_verdict = {"ran": True, "passed": None, "exit_code": None}
    assert records.judge_disagrees(rec(check=no_verdict, judge=jdg(False))) is False
    assert records.judge_disagrees({"status": "completed"}) is False  # old record, no blocks
