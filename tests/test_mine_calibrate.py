"""`crucible mine calibrate` with fake shell runners simulating each status."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from crucible.cli import main
from crucible.mine import calibrate as cal

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
}
FIXED = "def add(a, b):\n    return a + b\n"
BROKEN = "def add(a, b):\n    return a - b\n"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True,
                          env=GIT_ENV).stdout.strip()


def commit(repo: Path, msg: str, files: dict[str, str]) -> None:
    for rel, text in files.items():
        f = repo / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg)


def write_config(home: Path, command: str, *, setup: str = "", timeout: float | None = None,
                 setup_timeout: float | None = None,
                 extra_suites: str = "") -> None:
    lines = ["suites:", "  - name: app", '    test_paths: ["tests/**"]',
             f"    test_command: {json.dumps(command)}"]
    if setup:
        lines.append(f"    setup: {json.dumps(setup)}")
    if timeout is not None:
        lines.append(f"    timeout: {timeout}")
    if setup_timeout is not None:
        lines.append(f"    setup_timeout: {setup_timeout}")
    (home / "mine.yaml").write_text("\n".join(lines) + "\n" + extra_suites, encoding="utf-8")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = tmp_path / "proj"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    commit(r, "init", {"README.md": "hi\n", "src/calc.py": BROKEN})
    commit(r, "fix add", {"src/calc.py": FIXED, "tests/test_calc.py": "# t\n"})
    commit(r, "regress", {"src/calc.py": BROKEN})
    commit(r, "more", {"src/calc.py": FIXED, "tests/test_more.py": "# t\n"})
    r = r.resolve()
    monkeypatch.chdir(r)
    return r


# A fake runner: passes only when the fix is present.
GOOD = ': {tests}; grep -q "a + b" src/calc.py'


def calibrate(*argv: str) -> int:
    return main(["mine", "--local", "calibrate", *argv])


def run_json(capsys, *argv: str) -> tuple[int, dict]:
    code = calibrate("--json", *argv)
    return code, json.loads(capsys.readouterr().out)


def suite_of(data: dict, name: str = "app") -> dict:
    return next(s for s in data["suites"] if s["name"] == name)


def test_ok(repo, crucible_home, capsys):
    write_config(crucible_home, GOOD, setup="echo installing")
    code, data = run_json(capsys)
    s = suite_of(data)
    assert code == 0 and data["ok"] is True
    assert s["status"] == "ok" and s["hints"] == []
    assert s["setup"]["ok"] is True and Path(s["setup"]["log"]).exists()
    assert "installing" in s["setup"]["tail"]
    assert {x["outcome"] for x in s["samples"]} == {"verified"}
    assert len(s["samples"]) == 2
    assert set(s["samples"][0]) == {"id", "outcome", "reason", "cached", "logs", "tail"}
    assert set(s) >= {"name", "status", "setup", "samples", "hints"}
    assert set(s["setup"]) == {"ok", "seconds", "log", "tail"}


def test_candidates_limits_the_sample(repo, crucible_home, capsys):
    write_config(crucible_home, GOOD)
    _, data = run_json(capsys, "--candidates", "1")
    assert len(suite_of(data)["samples"]) == 1


def test_second_run_reuses_the_verify_cache(repo, crucible_home, capsys):
    write_config(crucible_home, GOOD)
    run_json(capsys)
    _, data = run_json(capsys)
    assert all(x["cached"] for x in suite_of(data)["samples"])
    _, data = run_json(capsys, "--force")
    assert not any(x["cached"] for x in suite_of(data)["samples"])


def test_setup_failed(repo, crucible_home, capsys):
    write_config(crucible_home, GOOD, setup="echo 'bash: nosuchtool: command not found'; exit 127")
    code, data = run_json(capsys)
    s = suite_of(data)
    assert code == 1 and s["status"] == "setup-failed"
    assert s["setup"]["ok"] is False and s["samples"] == []
    assert any(h["id"] == "command-not-found" for h in s["hints"])
    assert "command not found" in s["setup"]["tail"]


def test_no_tests_ran(repo, crucible_home, capsys):
    write_config(crucible_home, ': {tests}; echo "=== no tests ran in 0.01s ==="; exit 5')
    code, data = run_json(capsys)
    s = suite_of(data)
    assert code == 1 and s["status"] == "no-tests-ran"
    assert any(h["id"] == "pytest-no-tests" for h in s["hints"])
    assert any("runnable" in h["hint"] for h in s["hints"])
    assert all(x["reason"] == "fails at merge" for x in s["samples"])


def test_not_selective(repo, crucible_home, capsys):
    write_config(crucible_home, ": {tests}; true")
    code, data = run_json(capsys)
    s = suite_of(data)
    assert code == 1 and s["status"] == "not-selective"
    assert all(x["reason"] == "passes at base" for x in s["samples"])
    assert any(h["id"] == "status-not-selective" for h in s["hints"])


def test_passes_at_base_with_no_tests_message_is_no_tests_ran(repo, crucible_home, capsys):
    write_config(crucible_home, ': {tests}; echo "No tests found, exiting with code 0"')
    _, data = run_json(capsys)
    assert suite_of(data)["status"] == "no-tests-ran"


def test_failing(repo, crucible_home, capsys):
    write_config(crucible_home, ": {tests}; echo \"KeyError: 'API_URL'\" >&2; false")
    code, data = run_json(capsys)
    s = suite_of(data)
    assert code == 1 and s["status"] == "failing"
    assert any(h["id"] == "missing-env-keyerror" for h in s["hints"])
    assert "KeyError" in s["samples"][0]["tail"]


def test_timeout_is_reported_and_not_cached(repo, crucible_home, capsys):
    write_config(crucible_home, ": {tests}; sleep 5", timeout=0.5)
    code, data = run_json(capsys, "--candidates", "1")
    s = suite_of(data)
    assert code == 1 and s["status"] == "timeout"
    assert any(h["id"] == "timeout-test" for h in s["hints"])
    cache = crucible_home / "mine" / "verify"
    assert not list(cache.glob("*.json"))  # transient: never cached


def test_timeout_flag_overrides(repo, crucible_home, capsys):
    write_config(crucible_home, ": {tests}; sleep 5")
    _, data = run_json(capsys, "--candidates", "1", "--timeout", "0.5")
    assert suite_of(data)["status"] == "timeout"


def test_setup_timeout(repo, crucible_home, capsys):
    (crucible_home / "mine.yaml").write_text(
        "suites:\n  - name: app\n    test_paths: ['tests/**']\n    setup: sleep 5\n"
        "    setup_timeout: 0.5\n    test_command: 'true'\n", encoding="utf-8")
    _, data = run_json(capsys)
    s = suite_of(data)
    assert s["status"] == "timeout" and any(h["id"] == "timeout-setup" for h in s["hints"])


def test_no_candidates_is_not_a_failure(repo, crucible_home, capsys):
    write_config(crucible_home, GOOD)
    (crucible_home / "mine.yaml").write_text(
        "suites:\n  - name: app\n    test_paths: ['nothing/**']\n    test_command: 'true'\n",
        encoding="utf-8")
    code, data = run_json(capsys)
    s = suite_of(data)
    assert code == 0 and s["status"] == "no-candidates" and s["samples"] == []
    assert any(h["id"] == "status-no-candidates" for h in s["hints"]) or s["hints"] == []


def test_no_candidates_warns_in_the_table(repo, crucible_home, capsys):
    (crucible_home / "mine.yaml").write_text(
        "suites:\n  - name: app\n    test_paths: ['nothing/**']\n    test_command: 'true'\n",
        encoding="utf-8")
    assert calibrate() == 0
    out = capsys.readouterr().out
    assert "no-candidates" in out and "warning" in out


def test_mixed(repo, crucible_home, capsys):
    # test_calc.py passes at base (not selective), test_more.py fails at merge
    write_config(crucible_home,
                 'case "{tests}" in *test_calc*) true;; *) false;; esac')
    code, data = run_json(capsys)
    s = suite_of(data)
    assert code == 1 and s["status"] == "mixed"
    assert {x["reason"] for x in s["samples"]} == {"passes at base", "fails at merge"}
    assert "not-selective" in s["detail"] and "failing" in s["detail"]


def test_human_table(repo, crucible_home, capsys):
    write_config(crucible_home, ": {tests}; true")
    assert calibrate() == 1
    out = capsys.readouterr().out
    assert out.splitlines()[0].split()[:2] == ["suite", "status"]
    assert "not-selective" in out and "hint [status-not-selective]" in out


def test_suite_selection_and_unknown_suite(repo, crucible_home, capsys):
    write_config(crucible_home, GOOD, extra_suites=(
        "  - name: other\n    test_paths: ['other/**']\n    cwd: .\n    test_command: 'true'\n"))
    code, data = run_json(capsys, "--suite", "app")
    assert [s["name"] for s in data["suites"]] == ["app"] and code == 0
    assert calibrate("--suite", "nope") == 2
    assert "unknown suite" in capsys.readouterr().err


def test_wrapper_script_suite(repo, crucible_home, capsys):
    scripts = crucible_home / "scripts"
    scripts.mkdir()
    (scripts / "run.sh").write_text(
        'test -n "$CRUCIBLE_TEST_FILES" && grep -q "a + b" src/calc.py\n')
    write_config(crucible_home, 'bash "$CRUCIBLE_SCRIPTS/run.sh"')
    code, data = run_json(capsys)
    assert code == 0 and suite_of(data)["status"] == "ok"


# --- calibration.json and the warnings ---

def test_calibration_json_written_and_keyed(repo, crucible_home, capsys):
    write_config(crucible_home, GOOD)
    calibrate()
    data = json.loads((crucible_home / "mine" / "calibration.json").read_text())
    from crucible.mine.config import load_config
    suites = load_config(crucible_home / "mine.yaml").suites
    assert data["config_hash"] == cal.config_hash(suites)
    assert data["suites"]["app"]["status"] == "ok"
    assert cal.note(suites) is None


def test_hash_includes_the_scripts_digest(repo, crucible_home):
    from crucible.mine.config import load_config
    write_config(crucible_home, GOOD)
    suites = load_config(crucible_home / "mine.yaml").suites
    before = cal.config_hash(suites)
    (crucible_home / "scripts").mkdir()
    (crucible_home / "scripts" / "x.sh").write_text("true\n")
    assert cal.config_hash(suites) != before


def test_listing_warns_when_uncalibrated_then_clears(repo, crucible_home, capsys):
    write_config(crucible_home, GOOD)
    assert main(["mine", "--local"]) == 0
    err = capsys.readouterr().err
    assert "never calibrated" in err and "crucible mine calibrate" in err
    assert calibrate() == 0
    capsys.readouterr()
    assert main(["mine", "--local"]) == 0
    assert "calibrat" not in capsys.readouterr().err


def test_listing_warns_after_failed_calibration(repo, crucible_home, capsys):
    write_config(crucible_home, ": {tests}; true")
    calibrate()
    capsys.readouterr()
    main(["mine", "--local"])
    assert "last calibration was not ok: app (not-selective)" in capsys.readouterr().err


def test_editing_config_makes_it_uncalibrated(repo, crucible_home, capsys):
    write_config(crucible_home, GOOD)
    calibrate()
    write_config(crucible_home, GOOD + " ")
    capsys.readouterr()
    main(["mine", "--local"])
    err = capsys.readouterr().err
    assert "config changed since last calibration" in err and "never calibrated" not in err


def test_json_listing_stays_pure_json(repo, crucible_home, capsys):
    write_config(crucible_home, GOOD)
    main(["mine", "--local", "--json"])
    cap = capsys.readouterr()
    json.loads(cap.out)
    assert "never calibrated" in cap.err


def test_verify_hints_to_calibrate_first_but_runs(repo, crucible_home, capsys):
    write_config(crucible_home, GOOD)
    cid = git(repo, "rev-parse", "--short=8", "HEAD")
    assert main(["mine", "--local", "--verify", cid]) == 0
    cap = capsys.readouterr()
    assert "never calibrated" in cap.err and f"{cid}: verified" in cap.out


def test_partial_calibration_merges_into_the_file(repo, crucible_home):
    write_config(crucible_home, GOOD, extra_suites=(
        "  - name: other\n    test_paths: ['other/**']\n    test_command: 'true'\n"))
    calibrate("--suite", "app")
    calibrate("--suite", "other")
    data = json.loads((crucible_home / "mine" / "calibration.json").read_text())
    assert set(data["suites"]) == {"app", "other"}


def test_no_suites_is_an_error(repo, crucible_home, capsys):
    (crucible_home / "mine.yaml").write_text("suites: []\n", encoding="utf-8")
    assert calibrate() == 1
    assert "suites" in capsys.readouterr().err


def test_verify_logs_end_with_the_exit_code(repo, crucible_home, capsys):
    write_config(crucible_home, ": {tests}; exit 7")
    _, data = run_json(capsys, "--candidates", "1")
    log = list(suite_of(data)["samples"][0]["logs"].values())[-1]
    assert Path(log).read_text().rstrip().endswith("exit code: 7")


def test_raising_a_timeout_clears_the_stale_timeout_status(repo, crucible_home, capsys):
    from crucible.mine.config import load_config
    write_config(crucible_home, ": {tests}; sleep 5", timeout=0.5)
    assert calibrate("--candidates", "1") == 1
    suites = load_config(crucible_home / "mine.yaml").suites
    assert "last calibration was not ok: app (timeout)" in cal.note(suites)
    before = cal.config_hash(suites)
    write_config(crucible_home, ": {tests}; sleep 5", timeout=900)
    suites = load_config(crucible_home / "mine.yaml").suites
    assert cal.config_hash(suites) != before
    assert "config changed since last calibration" in cal.note(suites)
    write_config(crucible_home, ": {tests}; sleep 5", setup_timeout=7)
    suites = load_config(crucible_home / "mine.yaml").suites
    assert cal.config_hash(suites) not in (before,)


def test_timeouts_change_the_calibration_hash_but_not_the_verify_cache_key(crucible_home):
    from crucible.mine import config as mc
    a = mc.Suite("a", ["t/**"], "x")
    b = mc.Suite("a", ["t/**"], "x", timeout=5, setup_timeout=9)
    assert a.settings_hash() == b.settings_hash()
    assert cal.config_hash([a]) != cal.config_hash([b])
    assert cal.config_hash([mc.Suite("a", ["t/**"], "x", timeout=5)]) != cal.config_hash([b])
