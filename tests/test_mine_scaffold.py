# ruff: noqa: F811  (the `repo` fixture is imported from test_mine_verify)
import json
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml
from test_mine_verify import (  # noqa: F401
    PY,
    commit,
    counter,
    git,
    ids,
    mine_yaml,
    repo,
    verify,
)

from crucible import checker
from crucible.cli import main
from crucible.mine import scaffold as sc
from crucible.mine.config import Suite
from crucible.task import load_task

FAKE = Path(__file__).parent / "fake_claude.py"
FILLED = {
    "_(Describe the change the agent should make.)_": "Make add work.",
    "_(Explain the motivation.)_": "It is broken.",
    "_(One concrete, checkable criterion per bullet.)_": "add(1, 2) is 3.",
}


def tasks(crucible_home) -> Path:
    return crucible_home / "tasks"


def scaffold(*argv) -> int:
    return main(["mine", "--local", "scaffold", *argv])


def fill(task_dir: Path) -> None:
    text = (task_dir / "task.md").read_text()
    for k, v in FILLED.items():
        text = text.replace(k, v)
    (task_dir / "task.md").write_text(text)


def front(task_dir: Path) -> dict:
    return yaml.safe_load((task_dir / "task.md").read_text().split("---")[1])


@pytest.fixture
def mined(repo, crucible_home):
    """The verified `fix add` candidate, scaffolded."""
    cid = ids(repo, "fix")[0]
    assert verify("--verify", cid) == 0
    assert scaffold(cid) == 0
    return tasks(crucible_home) / f"commit-{cid}-fix-add"


def test_scaffold_writes_task_and_validate_fails_only_on_placeholders(
        repo, crucible_home, capsys):
    cid = ids(repo, "fix")[0]
    verify("--verify", cid)
    capsys.readouterr()
    assert scaffold(cid) == 0
    d = tasks(crucible_home) / f"commit-{cid}-fix-add"
    fm = front(d)
    assert fm["id"] == d.name
    assert fm["ref"] == subprocess.run(
        ["git", "rev-parse", f"{repo.sha['fix']}^"], cwd=repo.path, capture_output=True,
        text=True, check=True).stdout.strip()
    assert fm["source"] == f"commit:{repo.sha['fix']}"
    assert fm["check"] == "./check.sh" and fm["kind"] == "bugfix"
    assert fm["timeout_minutes"] == 30 and fm["max_turns"] == 100
    # counted lines of the `fix add` commit: src/calc.py 1+1, tests/test_calc.py 4,
    # tests/helpers.py 1
    assert fm["source_lines"] == 7
    assert (d / fm["repo"]).resolve() == repo.path
    assert (d / "hidden_tests/tests/test_calc.py").is_file()
    assert (d / "hidden_tests/tests/helpers.py").is_file()
    assert not (d / "hidden_tests" / "DELETED").exists()
    assert os.access(d / "check.sh", os.X_OK)
    meta = json.loads((d / ".mine.json").read_text())
    assert meta["merge"] == repo.sha["fix"] and meta["suite"] == "py"
    capsys.readouterr()
    assert main(["task", "validate", str(d)]) == 1
    err = capsys.readouterr().err
    assert "placeholder" in err and "`repo`" not in err and "`ref`" not in err
    fill(d)
    assert main(["task", "validate", str(d)]) == 0
    assert capsys.readouterr().out.strip() == f"ok: {d.name}"
    assert load_task(d).source == f"commit:{repo.sha['fix']}"
    assert load_task(d).source_lines == 7


def test_prove_passes_then_always_green_check_fails(mined, capsys):
    fill(mined)
    capsys.readouterr()
    assert main(["mine", "prove", str(mined)]) == 0
    cap = capsys.readouterr()
    assert re.fullmatch(rf"proven: {mined.name} \(merge check \d+\.\ds\)", cap.out.strip())
    assert "warning" not in cap.err  # well under the default --check-timeout
    (mined / "check.sh").write_text("#!/bin/sh\nexit 0\n")
    assert main(["mine", "prove", str(mined)]) == 1
    assert "passes at base" in capsys.readouterr().err


def test_prove_warns_when_merge_check_exceeds_default_check_timeout(mined, monkeypatch, capsys):
    fill(mined)
    monkeypatch.setattr(checker, "DEFAULT_TIMEOUT_S", 0.0)
    capsys.readouterr()
    assert main(["mine", "prove", str(mined)]) == 0
    cap = capsys.readouterr()
    assert re.fullmatch(rf"proven: {mined.name} \(merge check \d+\.\ds\)", cap.out.strip())
    assert re.search(r"warning: merge check took \d+\.\ds, over the default --check-timeout "
                     r"of 0s; raise --check-timeout when running this task", cap.err)


def test_prove_fails_at_merge_and_shows_log_tail(mined, capsys):
    fill(mined)
    (mined / "check.sh").write_text(
        "#!/bin/sh\nfor i in $(seq 1 60); do echo line$i; done\nexit 1\n")
    capsys.readouterr()
    assert main(["mine", "prove", str(mined)]) == 1
    err = capsys.readouterr().err
    assert "fails at merge" in err and "line60" in err and "line20" not in err


def test_prove_reports_setup_failure_at_base(repo, crucible_home, capsys):
    (crucible_home / "mine.yaml").write_text(mine_yaml(
        f"{PY} -m pytest -q -p no:cacheprovider {{tests}}", setup="echo ok"))
    cid = ids(repo, "fix")[0]
    verify("--verify", cid)
    scaffold(cid)
    d = tasks(crucible_home) / f"commit-{cid}-fix-add"
    fill(d)
    script = (d / "check.sh").read_text().replace("bash -c 'echo ok'", "bash -c false")
    (d / "check.sh").write_text(script)
    capsys.readouterr()
    assert main(["mine", "prove", str(d)]) == 1
    assert "setup failed at base" in capsys.readouterr().err


def test_prove_unfilled_task_exits_1(mined, capsys):
    assert main(["mine", "prove", str(mined)]) == 1
    assert "placeholder" in capsys.readouterr().err


def test_prove_pr_url_uses_verify_cache(mined, crucible_home, repo, capsys):
    fill(mined)
    (mined / ".mine.json").unlink()
    text = (mined / "task.md").read_text().replace(
        f'source: "commit:{repo.sha["fix"]}"', 'source: "https://github.com/o/r/pull/1"')
    (mined / "task.md").write_text(text)
    with pytest.raises(sc.ScaffoldError):
        sc.merge_sha(load_task(mined))
    cache = crucible_home / "mine" / "verify" / f"{repo.sha['fix']}.json"
    data = json.loads(cache.read_text())
    data["id"] = "1"
    cache.write_text(json.dumps(data))
    assert main(["mine", "prove", str(mined)]) == 0
    assert "proven" in capsys.readouterr().out


def test_scaffold_unverified_and_rejected_exit_1(repo, crucible_home, capsys):
    f, p = ids(repo, "fix", "passes")
    capsys.readouterr()
    assert scaffold(f) == 1
    assert "not verified" in capsys.readouterr().err
    verify("--verify", p)
    capsys.readouterr()
    assert scaffold(p) == 1
    assert "passes at base" in capsys.readouterr().err
    assert scaffold("deadbeef") == 1
    assert not list(tasks(crucible_home).iterdir())


def test_scaffold_existing_slug_untouched(repo, crucible_home, capsys):
    f = ids(repo, "fix")[0]
    verify("--verify", f)
    assert scaffold(f, "--task-id", "mine-1") == 0
    marker = tasks(crucible_home) / "mine-1" / "marker"
    marker.write_text("x")
    # a second verified candidate wanting the same slug
    git(repo.path, "checkout", "-q", "-b", "side", repo.sha["fix"])
    sha = commit(repo.path, "add neg", {
        "src/neg.py": "def neg(a):\n    return -a\n",
        "tests/test_neg.py": "from src.neg import neg\n\ndef test_n():\n    assert neg(2) == -2\n"})
    side = ["mine", "--local", "--branch", "side"]
    assert main([*side, "--verify", sha[:8]]) == 0
    capsys.readouterr()
    assert main([*side, "scaffold", sha[:8], "--task-id", "mine-1"]) == 1
    assert "already exists" in capsys.readouterr().err
    assert marker.read_text() == "x"
    assert [p.name for p in tasks(crucible_home).iterdir()] == ["mine-1"]
    assert main([*side, "scaffold", sha[:8], "--task-id", "Bad Slug"]) == 1
    assert "invalid task id" in capsys.readouterr().err


def test_scaffold_marks_candidate_already_mined(repo, crucible_home, capsys):
    cid = ids(repo, "fix")[0]
    verify("--verify", cid)
    scaffold(cid)
    capsys.readouterr()
    assert main(["mine", "--local", "--show-excluded"]) == 0
    out = capsys.readouterr().out
    assert re.search(rf"{cid}.*already mined", out)
    assert scaffold(cid) == 1  # mined candidates can't be scaffolded again


def test_deleted_test_file_and_quoting(repo, crucible_home, tmp_path):
    r = repo.path
    commit(r, "add old test", {"tests/test_old.py": "def test_old():\n    assert True\n"})
    git(r, "rm", "-q", "tests/test_old.py")
    sha = commit(r, "fix mul", {"src/mul.py": "def mul(a, b):\n    return a * b\n",
                                "tests/test_mul.py":
                                "from src.mul import mul\n\ndef test_mul():\n    assert mul(2, 3) == 6\n",
                                "tests/test_sp ace.py": "from src.mul import mul\n\ndef test_x():\n    assert mul(1, 1) == 1\n"})
    assert verify("--verify", sha[:8]) == 0
    assert scaffold(sha[:8]) == 0
    d = tasks(crucible_home) / f"commit-{sha[:8]}-fix-mul"
    script = (d / "check.sh").read_text()
    assert "'tests/test_sp ace.py'" in script
    assert (d / "hidden_tests/tests/test_sp ace.py").is_file()
    assert (d / "hidden_tests" / "DELETED").read_text() == "tests/test_old.py\n"
    (d / "hidden_tests" / "DELETED").write_text("tests/test_calc.py\n")
    ws = tmp_path / "ws"
    (ws / "tests").mkdir(parents=True)
    (ws / "tests" / "test_calc.py").write_text("x")
    proc = subprocess.run([str(d / "check.sh")], cwd=ws, capture_output=True, text=True,
                          env={**os.environ, "CRUCIBLE_TASK_DIR": str(d)}, check=False)
    assert proc.returncode != 0  # bare workspace has no src/, but the quoted path was found
    assert "test_sp ace.py" in proc.stdout
    assert not (ws / "tests" / "test_calc.py").exists()
    assert not (ws / "DELETED").exists()
    assert (ws / "tests" / "test_sp ace.py").is_file()


def test_run_with_fake_claude_has_clean_workspace(mined, crucible_home, monkeypatch, capsys):
    fill(mined)
    monkeypatch.setenv("CRUCIBLE_CLAUDE_BIN", str(FAKE))
    monkeypatch.delenv("CRUCIBLE_ISOLATION", raising=False)
    seen = {}
    real = checker.run_check

    def spy(task, ws, *a, **k):
        seen["files"] = sorted(str(p.relative_to(ws)) for p in ws.rglob("*") if ".git" not in p.parts)
        return real(task, ws, *a, **k)

    monkeypatch.setattr(checker, "run_check", spy)
    capsys.readouterr()
    assert main(["run", str(mined), "--configs", "sonnet:medium"]) == 0
    assert seen["files"]
    assert not any(f.startswith(".crucible") for f in seen["files"])
    assert not any(f.startswith("tests") for f in seen["files"])  # hidden tests not yet installed
    (batch,) = (crucible_home / "results" / mined.name).iterdir()
    rec = json.loads((batch / "sonnet-medium-1" / "record.json").read_text())
    assert rec["check"]["ran"] is True
    assert rec["check"]["passed"] is False  # fake claude did not fix add


def run_script(tmp_path, suites, files, **env):
    """Generate check.sh for `suites` and run it in a fresh workspace with no hidden tests."""
    task = tmp_path / "task"
    (task / "hidden_tests").mkdir(parents=True, exist_ok=True)
    (task / "check.sh").write_text(sc.check_script(suites, files))
    ws = tmp_path / "ws"
    for s in suites if isinstance(suites, list) else [suites]:
        (ws / s.cwd).mkdir(parents=True, exist_ok=True)
    return subprocess.run(["bash", str(task / "check.sh")], cwd=ws, capture_output=True,
                          text=True, check=False,
                          env={"PATH": os.environ["PATH"], "CRUCIBLE_TASK_DIR": str(task), **env})


REG_SUITE = Suite("py", ["tests/**"], "echo tests {tests}; exit $TRC",
                  regression_command="echo regression-ran; exit $RRC")


def test_single_suite_without_regression_is_unchanged():
    script = sc.check_script(Suite("py", ["tests/**"], "echo tests {tests}"), ["tests/test_a.py"])
    assert script.rstrip().splitlines()[-1] == "exec bash -c 'echo tests tests/test_a.py'"
    assert "regression" not in script


@pytest.mark.parametrize("trc,rrc,expected", [("0", "0", 0), ("0", "3", 3), ("1", "3", 1),
                                              ("1", "0", 1)])
def test_single_suite_runs_regression_after_tests(tmp_path, trc, rrc, expected):
    r = run_script(tmp_path, REG_SUITE, ["tests/test_a.py"], TRC=trc, RRC=rrc)
    assert r.returncode == expected
    assert "== regression py ==" in r.stdout and "regression-ran" in r.stdout
    assert r.stdout.index("tests tests/test_a.py") < r.stdout.index("regression-ran")


def test_setup_failure_exits_97_before_regression(tmp_path):
    s = Suite("py", ["tests/**"], "echo tests {tests}", setup="false",
              regression_command="echo regression-ran")
    r = run_script(tmp_path, s, ["tests/test_a.py"])
    assert r.returncode == 97 and "regression-ran" not in r.stdout
    assert sc.SETUP_FAILED in r.stderr


def test_multi_suite_regression_failures_are_collected(tmp_path):
    files = ["a/tests/test_a.py", "b/tests/test_b.py"]
    a = Suite("a", ["a/**"], "echo a-tests", cwd="a",
              regression_command="echo a-regression; exit 4")
    b = Suite("b", ["b/**"], "echo b-tests; exit $BRC", cwd="b")
    r = run_script(tmp_path, [a, b], files, BRC="0")
    assert r.returncode == 4
    assert "a-regression" in r.stdout and "b-tests" in r.stdout  # b still ran
    assert r.stdout.count("== suite") == 2 and "== regression a ==" in r.stdout
    assert run_script(tmp_path, [a, b], files, BRC="6").returncode == 4  # first failure wins
    b_bad_setup = Suite("b", ["b/**"], "echo b-tests", cwd="b", setup="false")
    assert run_script(tmp_path, [a, b_bad_setup], files).returncode == 97


def test_failing_regression_makes_prove_fail_at_merge(repo, crucible_home, capsys):
    (crucible_home / "mine.yaml").write_text(
        mine_yaml(f"{PY} -m pytest -q -p no:cacheprovider {{tests}}")
        + '    regression_command: "echo regression-ran; exit 5"\n')
    cid = ids(repo, "fix")[0]
    assert verify("--verify", cid) == 0
    assert scaffold(cid) == 0
    d = tasks(crucible_home) / f"commit-{cid}-fix-add"
    assert "regression-ran" in (d / "check.sh").read_text()
    fill(d)
    capsys.readouterr()
    assert main(["mine", "prove", str(d)]) == 1
    err = capsys.readouterr().err
    assert "fails at merge" in err and "regression-ran" in err
