"""Portable tasks, diagnostics precision, calibrate bounds, bare task ids, skip markers."""

import json
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_mine_misc import GOOD, make_repo
from test_mine_scaffold import scaffold, tasks
from test_mine_verify import git, verify

from crucible import cli
from crucible.mine import calibrate
from crucible.mine import diagnostics as dg
from crucible.mine import scaffold as sc
from crucible.mine import verify as vf
from crucible.mine.config import Suite
from crucible.task import TaskError


def ids_of(text, code=1, stage="test"):
    return [h.id for h in dg.diagnose(text, code, stage)]


# --- 2: portable JAVA_HOME ---

def fake_jdk(root: Path, version="21.0.2") -> Path:
    root.mkdir(parents=True)
    (root / "release").write_text(f'JAVA_VERSION="{version}"\n')
    (root / "bin").mkdir()
    (root / "bin" / "javac").write_text("")
    return root


def test_check_script_guards_machine_paths_and_records_the_major(tmp_path):
    home = fake_jdk(tmp_path / "jdks" / "j21")
    s = Suite(name="m", test_paths=["t/**"], test_command="mvn test", env={
        "JAVA_HOME": str(home), "MODE": "ci", "TOOLS": str(tmp_path)})
    script = sc.check_script(s, ["t/FooTest.java"])
    assert f'if [ -d {home} ]; then export JAVA_HOME={home}; fi' in script
    assert f"if [ -e {tmp_path} ]; then export TOOLS={tmp_path}; fi" in script
    assert "export MODE=ci" in script
    assert not re.search(r"^export JAVA_HOME=", script, re.MULTILINE)
    assert "JDK 21" in script  # the major is kept in a comment


def test_check_script_with_guard_still_runs_without_the_path(tmp_path):
    s = Suite(name="m", test_paths=["t/**"], test_command='echo "JH=${JAVA_HOME:-unset}"',
              env={"JAVA_HOME": str(tmp_path / "does-not-exist")})
    script = sc.check_script(s, ["t/a"])
    assert "export JAVA_HOME" in script  # guarded, never unconditional
    ws = tmp_path / "ws"
    (tmp_path / "task" / "hidden_tests").mkdir(parents=True)
    ws.mkdir()
    (tmp_path / "task" / "check.sh").write_text(script)
    r = subprocess.run(["bash", str(tmp_path / "task" / "check.sh")], cwd=ws, capture_output=True, check=False,
                       text=True, env={"PATH": "/usr/bin:/bin"})
    assert "JH=unset" in r.stdout


def test_scaffold_records_and_warns_jdk_requirement(tmp_path, monkeypatch, crucible_home, capsys):
    make_repo(tmp_path, monkeypatch, 0)
    home = fake_jdk(tmp_path / "jdks" / "j21")
    (crucible_home / "mine.yaml").write_text(
        "suites:\n  - name: py\n    test_paths: [\"tests/**\"]\n"
        f"    env: {{JAVA_HOME: {json.dumps(str(home))}}}\n"
        f"    test_command: {json.dumps(GOOD)}\n")
    cid = git(tmp_path / "proj", "log", "--format=%h", "-n", "1", "--abbrev=8")
    assert verify("--verify", cid) == 0
    capsys.readouterr()
    assert scaffold(cid) == 0
    cap = capsys.readouterr()
    assert "jdk 21" in cap.err and "requires" in cap.err.lower()
    d = next(tasks(crucible_home).iterdir())
    assert json.loads((d / ".mine.json").read_text())["requires"] == ["jdk 21"]
    assert not re.search(r"^export JAVA_HOME=", (d / "check.sh").read_text(), re.MULTILINE)


# --- 3: ran_tests / unless ---

GO_BUILD_FAILED = ("# example.com/pkg [example.com/pkg.test]\n./a_test.go:5:2: undefined: Foo\n"
                   "FAIL\texample.com/pkg [build failed]\nFAIL\n")
TS_JEST_COMPILE = ("FAIL src/a.test.ts\n  ● Test suite failed to run\n"
                   "    src/a.test.ts:3:1 - error TS2305: Module has no exported member 'x'.\n"
                   "Test Suites: 1 failed, 1 total\nTests:       0 total\n")


@pytest.mark.parametrize("log", [GO_BUILD_FAILED, TS_JEST_COMPILE,
                                 "ok  \texample.com/pkg\t0.003s [no tests to run]\n",
                                 "Test Suites: 1 failed, 1 total\n"])
def test_build_failures_do_not_count_as_tests_that_ran(log):
    assert not dg.ran_tests(log)


@pytest.mark.parametrize("log", [
    "ok  \texample.com/pkg\t0.512s\n", "--- FAIL: TestX (0.00s)\n--- PASS: TestY (0.00s)\n",
    "Tests:       2 failed, 3 passed, 5 total\n", "ok  \texample.com/pkg\t(cached)\n",
    "3 passed in 0.1s\n"])
def test_real_test_runs_count(log):
    assert dg.ran_tests(log)


def test_go_build_failed_and_ts_compile_error_give_compile_hints():
    assert "compile-error-go" in ids_of(GO_BUILD_FAILED)
    assert "compile-error-ts" in ids_of(TS_JEST_COMPILE)


def test_go_no_tests_to_run_is_the_no_tests_status():
    hints = dg.diagnose("ok  \texample.com/pkg\t0.003s [no tests to run]\n", 0, "test")
    assert [h.id for h in hints if h.status == "no-tests-ran"] == ["go-no-tests-to-run"]


def test_no_test_files_still_fires_next_to_a_package_that_ran():
    both = "?   \texample.com/a\t[no test files]\nok  \texample.com/b\t0.2s\n"
    assert "go-no-test-files" not in ids_of(both, 0)
    assert "go-no-test-files" in ids_of("?   \texample.com/a\t[no test files]\n", 0)


def test_jest_no_tests_found_is_not_masked_by_a_suites_line():
    log = "No tests found, exiting with code 1\nTest Suites: 1 failed, 1 total\n"
    assert "jest-no-tests" in ids_of(log)


def test_weak_warning_fires_for_go_build_failure_and_ts_jest_compile_error(tmp_path):
    for i, text in enumerate((GO_BUILD_FAILED, TS_JEST_COMPILE)):
        f = tmp_path / f"{i}.log"
        f.write_text(text + "\nexit code: 1\n")
        res = SimpleNamespace(logs={"base-test": str(f)})
        assert vf.compile_only_warning(res, ["base-test"]), text


# --- 4: Lombok / new JDK symptom ---

SITEMAP = """[ERROR] COMPILATION ERROR :
[ERROR] /w/src/main/java/com/acme/SitemapDiff.java:[41,30] cannot find symbol
  symbol:   method getUrl()
  location: variable entry of type com.acme.Entry
[ERROR] /w/src/main/java/com/acme/SitemapDiff.java:[50,20] cannot find symbol
  symbol:   method builder()
  location: class com.acme.Diff
"""


def test_missing_lombok_accessors_hint_without_lombok_in_the_log():
    hints = dg.diagnose(SITEMAP, 1, "test")
    by = {h.id: h for h in hints}
    assert "lombok-accessors-missing" in by
    text = by["lombok-accessors-missing"].hint
    assert "Lombok" in text and "JAVA_HOME" in text and "mine.yaml" in text
    assert "java-class-missing" not in by  # "build the dependencies" would mislead here


def test_accessor_hint_needs_a_getter_setter_or_builder():
    other = "error: cannot find symbol\n  symbol:   method compute(int)\n"
    assert "lombok-accessors-missing" not in ids_of(other)


# --- 5: hint ordering / suppression ---

VICTOR = ("/usr/lib/ruby/rubygems/core_ext/kernel_require.rb:136:in `require': cannot load such "
          "file -- rails/all (LoadError)\nLoadError: cannot load such file -- rails/all\n"
          "No examples found.\n0 examples, 0 failures\n")


def test_root_cause_row_suppresses_the_generic_no_tests_row():
    got = ids_of(VICTOR, 0)
    assert got == ["ruby-load-error"]


def test_no_tests_row_still_fires_without_a_root_cause():
    assert ids_of("No examples found.\n0 examples, 0 failures\n", 0) == ["rspec-no-examples"]


def test_suppresses_must_name_existing_rows():
    bad = {"rules": [{"id": "a", "category": "c", "hint": "h", "pattern": "x",
                      "suppresses": ["nope"]}]}
    with pytest.raises(ValueError, match="suppresses"):
        dg.parse_table(bad)


# --- 7: calibrate ---

def test_own_part_logs_do_not_mix_suites_with_shared_prefixes(tmp_path):
    logs = {}
    for k in ("base-web-setup", "base-web-test", "merge-web-test", "base-web-ui-test",
              "merge-web-ui-test", "merge-web-ui-setup"):
        f = tmp_path / f"{k}.log"
        f.write_text("ok\n\nexit code: " + ("1" if k == "base-web-test" else "0") + "\n")
        logs[k] = str(f)
    res = vf.VerifyResult(id="1", merge="m", suite="web+web-ui", outcome="rejected",
                          reason="fails at merge (web-ui)", logs=logs)
    got = calibrate._own_part(res, "web", dg.load_table())
    assert got is not None
    assert set(got[0].logs) == {"base-web-setup", "base-web-test", "merge-web-test"}


def test_calibrate_scans_a_bounded_number_of_owned_candidates(monkeypatch):
    suite = Suite(name="a", test_paths=["t/**"], test_command="true")
    cands = [SimpleNamespace(id=str(i), merge=f"m{i}", suites=["a"]) for i in range(40)]
    monkeypatch.setattr(calibrate.vf, "involved_suites", lambda c, s: [suite])
    monkeypatch.setattr(calibrate.vf, "suite_plan", lambda c, s: [(suite, ["t/x"])])
    seen = []

    def fake_verify(repo, c, suites, **kw):
        seen.append(c.id)
        return SimpleNamespace(verified=False, reason="fails at merge (other)", suite="a+other",
                               logs={}, id=c.id, cached=False)

    monkeypatch.setattr(calibrate.vf, "verify_candidate", fake_verify)
    monkeypatch.setattr(calibrate, "check_setup", lambda *a, **k: SimpleNamespace(
        ok=True, timed_out=False, to_dict=dict))
    calibrate.calibrate_suite(Path("."), suite, [suite], cands, n=2, timeout=1, force=False,
                              memo={}, table=dg.load_table())
    assert len(seen) <= 6


# --- 8: bare task ids ---

def make_task(root: Path, name: str):
    d = root / name
    d.mkdir(parents=True)
    (d / "task.md").write_text("x")


def test_bare_id_falls_back_to_home_when_cwd_dir_is_not_a_task(tmp_path, monkeypatch, crucible_home):
    make_task(crucible_home / "tasks", "fix-it")
    work = tmp_path / "work"
    (work / "fix-it").mkdir(parents=True)  # same name, not a task
    monkeypatch.chdir(work)
    assert cli._task_path("fix-it") == crucible_home / "tasks" / "fix-it"


def test_bare_id_prefers_a_cwd_dir_that_is_a_task(tmp_path, monkeypatch, crucible_home):
    make_task(crucible_home / "tasks", "fix-it")
    work = tmp_path / "work"
    make_task(work, "fix-it")
    monkeypatch.chdir(work)
    assert cli._task_path("fix-it") == Path("fix-it")


def test_unknown_bare_id_lists_available_and_suggests(tmp_path, monkeypatch, crucible_home):
    for n in ("fix-parser", "add-cache", "zeta"):
        make_task(crucible_home / "tasks", n)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(TaskError) as e:
        cli._task_path("fix-parsr")
    msg = str(e.value)
    assert "no task named fix-parsr" in msg and "tasks" in msg
    assert "available: add-cache, fix-parser, zeta" in msg
    assert "did you mean fix-parser" in msg


def test_unknown_bare_id_caps_the_list_at_ten(tmp_path, monkeypatch, crucible_home):
    for i in range(14):
        make_task(crucible_home / "tasks", f"t{i:02d}")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(TaskError) as e:
        cli._task_path("qqqq")
    assert "t09" in str(e.value) and "t10" not in str(e.value) and "…" in str(e.value)


def test_paths_with_separators_are_untouched(tmp_path, monkeypatch, crucible_home):
    monkeypatch.chdir(tmp_path)
    assert cli._task_path("sub/dir") == Path("sub/dir")


# --- 9: skip-marker false positives ---

def warn(tmp_path, name, text):
    r = tmp_path / "r"
    r.mkdir(exist_ok=True)
    git(r, "init", "-q", "-b", "main") if not (r / ".git").exists() else None
    (r / name).parent.mkdir(parents=True, exist_ok=True)
    (r / name).write_text(text)
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "x")
    c = SimpleNamespace(merge=git(r, "rev-parse", "HEAD"))
    return vf.skip_marker_warnings(r, c, [name])


@pytest.mark.parametrize("name,text,flagged", [
    ("a.test.js", "it.skip('x', () => {})\n", True),
    ("a.test.ts", "describe.skip('x', () => {})\n", True),
    ("a.test.js", "test.skip('x', () => {})\n", True),
    ("a_test.py", "df = frame.skip(3)\n", False),
    ("a_test.go", "it := iter.skip(2)\n", False),
    ("a.test.js", "stream.skip(3)\n", False),
    ("a_spec.rb", "  pending 'later'\n", True),
    ("a_spec.rb", "  pending('later')\n", True),
    ("a_spec.rb", "  pending\n", True),
    ("a_spec.rb", "  pending_orders = []\n", False),
    ("a_spec.rb", "  pending_count += 1\n", False),
])
def test_skip_markers_are_precise(tmp_path, name, text, flagged):
    assert bool(warn(tmp_path, name, text)) is flagged


# --- final review fixes ---

def test_accessor_hint_ignores_missing_methods_in_test_code():
    # a hidden test calling a getter the fix adds is the normal fail-before case, not Lombok
    log = ("[ERROR] /w/src/test/java/com/acme/DiffTest.java:[12,9] cannot find symbol\n"
           "  symbol:   method getTotal()\n  location: class com.acme.Diff\n")
    assert "lombok-accessors-missing" not in ids_of(log)


def test_go_cached_no_tests_to_run_is_not_a_test_run():
    log = "ok  \texample.com/pkg\t(cached) [no tests to run]\n"
    assert not dg.ran_tests(log)
    assert "go-no-tests-to-run" in ids_of(log)
