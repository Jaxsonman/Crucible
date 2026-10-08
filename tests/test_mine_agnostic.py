# ruff: noqa: F811  (the `repo` fixture is imported from test_mine_verify)
"""Configurable runnable globs, timeouts, wrapper scripts, env vars and comment syntax."""

import hashlib
import json
import stat
import time
from pathlib import Path

import pytest
from test_mine_scaffold import fill, scaffold, tasks
from test_mine_verify import (  # noqa: F401
    PY,
    cache,
    counter,
    git,
    ids,
    mine_yaml,
    repo,
    verify,
)

from crucible import checker, workspace
from crucible.cli import main
from crucible.mine import config as mc
from crucible.mine import leak
from crucible.mine import scaffold as sc
from crucible.mine import verify as vf
from crucible.task import load_task

PYTEST = f"{PY} -m pytest -q -p no:cacheprovider"


def write_cfg(home: Path, body: str) -> None:
    (home / "mine.yaml").write_text(body, encoding="utf-8")


def suite_yaml(command: str, extra: str = "") -> str:
    return (
        "suites:\n  - name: py\n    test_paths: [\"tests/**\"]\n    env: {PYTHONPATH: .}\n"
        f"    test_command: {json.dumps(command)}\n{extra}")


# --- config ---

def load(tmp_path, text):
    f = tmp_path / "m.yaml"
    f.write_text(text, encoding="utf-8")
    return mc.load_config(f)


def test_new_suite_fields_load_and_default(tmp_path):
    [s] = load(tmp_path, suite_yaml("make test")).suites
    assert (s.runnable, s.timeout, s.setup_timeout) == (None, 600.0, 1800.0)
    [s] = load(tmp_path, suite_yaml(
        "x", "    runnable: ['**/*Test.java']\n    timeout: 90\n    setup_timeout: 5.5\n")).suites
    assert (s.runnable, s.timeout, s.setup_timeout) == (["**/*Test.java"], 90.0, 5.5)
    assert s.is_runnable("src/FooTest.java") and not s.is_runnable("src/Foo.java")


@pytest.mark.parametrize("extra,needle", [
    ("    runnable: []\n", "suites[0].runnable: must not be empty"),
    ("    runnable: [1]\n", "suites[0].runnable: must be a list of non-empty strings"),
    ("    runnable: '*.py'\n", "suites[0].runnable: must be a list"),
    ("    timeout: 0\n", "suites[0].timeout: must be a positive number"),
    ("    timeout: soon\n", "suites[0].timeout: must be a positive number"),
    ("    setup_timeout: -3\n", "suites[0].setup_timeout: must be a positive number"),
    ("    timeout: true\n", "suites[0].timeout: must be a positive number"),
])
def test_new_field_errors_name_the_field(tmp_path, extra, needle):
    with pytest.raises(mc.MineConfigError) as e:
        load(tmp_path, suite_yaml("x", extra))
    assert any(needle in m for m in e.value.errors), e.value.errors


def test_placeholder_errors_name_the_field(tmp_path):
    with pytest.raises(mc.MineConfigError) as e:
        load(tmp_path, suite_yaml("run {tests|zip}"))
    assert any(m.startswith("suites[0].test_command: ") and "unknown filter 'zip'" in m
               for m in e.value.errors)


def test_dollar_brace_commands_load(tmp_path):
    [s] = load(tmp_path, suite_yaml('echo "${HOME}" && run {tests}')).suites
    assert "${HOME}" in s.test_command


def test_legacy_settings_hash_is_unchanged():
    s = mc.Suite("a", ["t/**"], "pytest {tests}", "sub", "pip i", None, {"A": "b"})
    legacy = {"name": "a", "test_paths": ["t/**"], "cwd": "sub", "setup": "pip i",
              "test_command": "pytest {tests}", "env": {"A": "b"}}
    assert s.settings_hash() == hashlib.sha256(
        json.dumps(legacy, sort_keys=True).encode()).hexdigest()


def test_hash_covers_runnable_but_not_timeouts():
    base = mc.Suite("a", ["t/**"], "x")
    assert mc.Suite("a", ["t/**"], "x", runnable=["*.t"]).settings_hash() != base.settings_hash()
    assert (mc.Suite("a", ["t/**"], "x", timeout=5, setup_timeout=7).settings_hash()
            == base.settings_hash())
    assert mc.Suite("a", ["t/**"], "x", timeout=5).to_dict()["timeout"] == 5


# --- verify ---

def test_runnable_globs_replace_the_builtin_rule(repo, crucible_home, tmp_path):
    rec = tmp_path / "rec.txt"
    write_cfg(crucible_home, suite_yaml(f"echo {{tests}} > {rec}; true",
                                         "    runnable: ['tests/helpers.py']\n"))
    verify("--verify", ids(repo, "fix")[0])
    assert rec.read_text().split() == ["tests/helpers.py"]


def test_runnable_globs_can_make_conftest_runnable(repo, crucible_home):
    write_cfg(crucible_home, suite_yaml(f"{PYTEST} {{tests}}"))
    verify("--verify", ids(repo, "conftest")[0])
    assert cache(crucible_home, repo.sha["conftest"])["reason"] == "no runnable tests"
    write_cfg(crucible_home, suite_yaml(f"{PYTEST} {{tests}}", "    runnable: ['tests/conftest.py']\n"))
    verify("--verify", ids(repo, "conftest")[0], "--force")
    assert cache(crucible_home, repo.sha["conftest"])["reason"] != "no runnable tests"


def test_whole_suite_command_without_placeholder(repo, crucible_home, capsys):
    write_cfg(crucible_home, suite_yaml(f"{PYTEST} tests"))
    f, p = ids(repo, "fix", "passes")
    assert verify("--verify", f, p) == 0
    out = capsys.readouterr().out
    assert f"{f}: verified" in out and f"{p}: rejected: passes at base" in out


def test_module_filter_outside_root_rejects(repo, crucible_home):
    write_cfg(crucible_home, suite_yaml("echo {tests|module:elsewhere}"))
    verify("--verify", ids(repo, "fix")[0])
    assert cache(crucible_home, repo.sha["fix"])["reason"] == "test outside module_root"


def test_steps_get_test_file_env(repo, crucible_home, tmp_path):
    rec = tmp_path / "env.txt"
    write_cfg(crucible_home, suite_yaml(
        f"printf '%s|%s' \"$CRUCIBLE_TEST_FILES\" \"$CRUCIBLE_TEST_FILES_CWD\" > {rec}; true",
        "    cwd: tests\n    runnable: ['tests/*.py']\n"))
    verify("--verify", ids(repo, "fix")[0])
    files, cwd_files = rec.read_text().split("|")
    assert sorted(files.split("\n")) == ["tests/helpers.py", "tests/test_calc.py"]
    assert sorted(cwd_files.split("\n")) == ["helpers.py", "test_calc.py"]


def test_setup_step_gets_env_and_no_scripts_var_without_dir(repo, crucible_home, tmp_path):
    rec = tmp_path / "setup.txt"
    write_cfg(crucible_home, suite_yaml(
        f"{PYTEST} {{tests}}",
        f"    setup: 'echo \"$CRUCIBLE_TEST_FILES:${{CRUCIBLE_SCRIPTS-unset}}\" > {rec}'\n"))
    verify("--verify", ids(repo, "fix")[0])
    assert rec.read_text().strip() == "tests/test_calc.py:unset"


def make_script(home: Path, name="run-tests.sh", body=None) -> Path:
    d = home / "scripts"
    d.mkdir(exist_ok=True)
    f = d / name
    f.write_text(body or f"#!/usr/bin/env bash\nexec {PYTEST} $CRUCIBLE_TEST_FILES\n")
    f.chmod(0o755)
    return f


def test_scripts_dir_is_exposed_to_verify(repo, crucible_home):
    make_script(crucible_home)
    write_cfg(crucible_home, suite_yaml('bash "$CRUCIBLE_SCRIPTS/run-tests.sh"'))
    assert verify("--verify", ids(repo, "fix")[0]) == 0
    assert cache(crucible_home, repo.sha["fix"])["outcome"] == "verified"


def test_editing_a_script_invalidates_the_cache(repo, crucible_home, counter):
    s = make_script(crucible_home, body=f"#!/usr/bin/env bash\necho run >> {counter}\n"
                                        f"exec {PYTEST} $CRUCIBLE_TEST_FILES\n")
    write_cfg(crucible_home, suite_yaml('bash "$CRUCIBLE_SCRIPTS/run-tests.sh"'))
    cid = ids(repo, "fix")[0]
    verify("--verify", cid)
    n = counter.read_text().count("run")
    verify("--verify", cid)
    assert counter.read_text().count("run") == n  # cached
    s.write_text(s.read_text() + "# edited\n")
    verify("--verify", cid)
    assert counter.read_text().count("run") > n


def test_runnable_change_invalidates_but_timeout_change_does_not(repo, crucible_home, counter):
    cmd = f"echo run >> {counter}; {PYTEST} {{tests}}"
    write_cfg(crucible_home, suite_yaml(cmd))
    cid = ids(repo, "fix")[0]
    verify("--verify", cid)
    n = counter.read_text().count("run")
    write_cfg(crucible_home, suite_yaml(cmd, "    timeout: 100\n    setup_timeout: 100\n"))
    verify("--verify", cid)
    assert counter.read_text().count("run") == n
    write_cfg(crucible_home, suite_yaml(cmd, "    runnable: ['tests/test_*.py']\n"))
    verify("--verify", cid)
    assert counter.read_text().count("run") > n


def test_suite_timeout_and_cli_override(repo, crucible_home, capsys):
    cmd = f"sleep 2; {PYTEST} {{tests}}"
    write_cfg(crucible_home, suite_yaml(cmd, "    timeout: 0.5\n"))
    cid = ids(repo, "fix")[0]
    assert verify("--verify", cid) == 0
    assert "rejected: timeout" in capsys.readouterr().out
    assert verify("--verify", cid, "--timeout", "60") == 0  # CLI overrides the suite's timeout
    assert f"{cid}: verified" in capsys.readouterr().out


def test_setup_timeout_applies_to_setup_only(repo, crucible_home, capsys):
    write_cfg(crucible_home, suite_yaml(f"{PYTEST} {{tests}}", "    setup: sleep 3\n    setup_timeout: 0.5\n"))
    start = time.monotonic()
    assert verify("--verify", ids(repo, "fix")[0], "--timeout", "60") == 0
    assert "rejected: timeout" in capsys.readouterr().out
    assert time.monotonic() - start < 30


# --- scaffold and prove ---

def scaffolded(repo, crucible_home, command, extra="", script=True):
    if script:
        make_script(crucible_home)
    write_cfg(crucible_home, suite_yaml(command, extra))
    cid = ids(repo, "fix")[0]
    assert verify("--verify", cid) == 0
    assert scaffold(cid) == 0
    d = tasks(crucible_home) / f"commit-{cid}-fix-add"
    fill(d)
    return d


def test_scaffold_copies_scripts_and_check_sh_uses_them(repo, crucible_home, capsys):
    d = scaffolded(repo, crucible_home, 'bash "$CRUCIBLE_SCRIPTS/run-tests.sh"')
    copied = d / "scripts" / "run-tests.sh"
    assert copied.read_text() == (crucible_home / "scripts/run-tests.sh").read_text()
    assert stat.S_IMODE(copied.stat().st_mode) == 0o755
    assert 'export CRUCIBLE_SCRIPTS="$TASK_DIR/scripts"' in (d / "check.sh").read_text()
    capsys.readouterr()
    assert main(["mine", "prove", str(d)]) == 0, capsys.readouterr().err
    # the same check.sh through the runner's checker, from a fresh export, with the home's
    # scripts gone: only the task's own copy can be used
    (crucible_home / "scripts" / "run-tests.sh").unlink()
    task = load_task(d)
    ws = d.parent.parent / "ws"
    ws.mkdir()
    workspace.export_tree(repo.path, repo.sha["fix"], ws)
    assert not (ws / ".crucible").exists() and not (ws / "scripts").exists()
    assert checker.run_check(task, ws).passed is True


def test_no_scripts_dir_means_no_scripts_in_task(repo, crucible_home):
    d = scaffolded(repo, crucible_home, f"{PYTEST} {{tests}}", script=False)
    assert not (d / "scripts").exists()
    assert "CRUCIBLE_SCRIPTS" not in (d / "check.sh").read_text()


def test_check_sh_sets_test_file_env(repo, crucible_home, tmp_path):
    rec = tmp_path / "rec.txt"
    d = scaffolded(repo, crucible_home,
                   f"printf '%s|%s' \"$CRUCIBLE_TEST_FILES\" \"$CRUCIBLE_TEST_FILES_CWD\" > {rec}; "
                   "grep -q 'a + b' ../src/calc.py",
                   "    cwd: tests\n    runnable: ['tests/test_*.py']\n", script=False)
    ws = tmp_path / "ws2"
    ws.mkdir()
    workspace.export_tree(repo.path, repo.sha["fix"], ws)
    checker.run_check(load_task(d), ws)
    assert rec.read_text() == "tests/test_calc.py|test_calc.py"


def test_scaffold_records_check_timeout_and_prove_uses_it(repo, crucible_home, monkeypatch):
    d = scaffolded(repo, crucible_home, f"{PYTEST} {{tests}}",
                   "    timeout: 100\n    setup_timeout: 200\n", script=False)
    meta = json.loads((d / ".mine.json").read_text())
    assert meta["check_timeout_s"] == 300
    task = load_task(d)
    assert sc.check_timeout(task) == 300
    seen = []
    real = checker.run_check
    monkeypatch.setattr(checker, "run_check", lambda t, w, timeout: (seen.append(timeout), real(t, w, timeout))[1])
    assert sc.prove_task(task).ok
    assert seen == [300.0, 300.0]
    seen.clear()
    assert sc.prove_task(task, 123).ok
    assert seen == [123, 123]
    (d / ".mine.json").unlink()
    assert sc.check_timeout(task) == vf.DEFAULT_TIMEOUT_S


def test_scaffold_with_runnable_globs_passes_only_those(repo, crucible_home):
    d = scaffolded(repo, crucible_home, "echo {tests}; grep -q 'a + b' src/calc.py",
                   "    runnable: ['tests/helpers.py']\n", script=False)
    script = (d / "check.sh").read_text()
    assert "echo tests/helpers.py;" in script and "test_calc" not in script


def test_copy_scripts_keeps_modes_and_nested_files(crucible_home):
    make_script(crucible_home)
    sub = crucible_home / "scripts" / "lib"
    sub.mkdir()
    (sub / "data.txt").write_text("x")
    dest = crucible_home / "out"
    assert sc._copy_scripts(dest)
    assert stat.S_IMODE((dest / "run-tests.sh").stat().st_mode) == 0o755
    assert stat.S_IMODE((dest / "lib/data.txt").stat().st_mode) == (
        stat.S_IMODE((sub / "data.txt").stat().st_mode))


# --- leak: comment syntax by extension ---

def code_of(lines, ext):
    return leak.strip_non_code(lines, ext)


def test_js_decrement_is_not_a_comment():
    out = code_of(["while (i-- > 0) { brandnew(i) }"], "js")
    assert "brandnew" in out
    # and `#` is not a comment in JS (private fields)
    assert "brandnew" in code_of(["x = 1 #brandnew"], "js")
    assert "brandnew" not in code_of(["x = 1 // brandnew"], "js")


def test_sql_dash_dash_is_a_comment():
    assert "hidden_name" not in code_of(["select 1 -- hidden_name", "from t"], "sql")
    assert "from t" in code_of(["select 1 -- hidden_name", "from t"], "sql")
    assert "hidden_name" not in code_of(["/* hidden_name */ select 1"], "sql")


def test_haskell_operator_is_not_a_comment():
    assert "kept_name" in code_of(["a --> kept_name"], "hs")
    assert "gone_name" not in code_of(["a -- gone_name"], "hs")
    assert "gone_name" not in code_of(["return 1 -- gone_name"], "lua")


@pytest.mark.parametrize("ext,text,gone", [
    ("py", "x = 1  # gone_name", "gone_name"),
    ("rb", "x = 1  # gone_name", "gone_name"),
    ("sh", "echo 1  # gone_name", "gone_name"),
    ("yaml", "a: 1  # gone_name", "gone_name"),
    ("toml", "a = 1  # gone_name", "gone_name"),
    ("java", "int a; // gone_name", "gone_name"),
    ("kt", "val a = 1 /* gone_name */", "gone_name"),
    ("go", "a := 1 // gone_name", "gone_name"),
    ("rs", "let a = 1; // gone_name", "gone_name"),
    ("ts", "const a = 1; /* gone_name */", "gone_name"),
    ("cs", "int a; // gone_name", "gone_name"),
    ("php", "$a = 1; # gone_name", "gone_name"),
    ("php", "$a = 1; // gone_name", "gone_name"),
    ("html", "<p>hi</p> <!-- gone_name -->", "gone_name"),
    ("vue", "<!-- gone_name --><b>x</b>", "gone_name"),
    ("sql", "select 1 -- gone_name", "gone_name"),
    ("PY", "x = 1  # gone_name", "gone_name"),
])
def test_comment_syntax_per_extension(ext, text, gone):
    assert gone not in code_of([text], ext)


@pytest.mark.parametrize("ext,text,kept", [
    ("py", "x = 1 // kept_name", "kept_name"),       # `//` is floor division in Python
    ("java", "x = 1; # kept_name", "kept_name"),
    ("sh", "echo 1 // kept_name", "kept_name"),
    ("html", "<p>hi</p> # kept_name", "kept_name"),
    ("sql", "select 1 // kept_name", "kept_name"),
])
def test_other_languages_comments_are_code(ext, text, kept):
    assert kept in code_of([text], ext)


def test_unknown_extension_keeps_the_permissive_behaviour():
    for ext in (None, "", "zzz"):
        out = code_of(["a # gone1", "b // gone2", "/* gone3 */ c"], ext)
        assert "gone" not in out and "a" in out and "c" in out
    assert leak.comment_syntax("zzz") is leak.DEFAULT_SYNTAX


def test_unclosed_html_comment_stays_code():
    assert "kept_name" in code_of(["<!-- never closed", "kept_name()"], "html")


def test_check_text_uses_each_files_comment_syntax(tmp_path):
    r = tmp_path / "lk"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / "a.sql").write_text("select 1;\n")
    (r / "b.js").write_text("let i = 3;\n")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "init")
    (r / "a.sql").write_text("select 1;\n-- zorgle\nselect 2;\n")
    (r / "b.js").write_text("let i = 3;\nwhile (i--) { blorp(); }\n")
    (r / "tests").mkdir()
    (r / "tests/test_x.py").write_text("def test_ok():\n    assert True\n")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "change")
    cfg = mc.MineConfig([mc.Suite("py", ["tests/**"], "x {tests}")], [], 800)
    flags = leak.check_text(r, git(r, "rev-parse", "HEAD^"), git(r, "rev-parse", "HEAD"), cfg,
                            [("What", "Look at `zorgle` and `blorp`.")])
    spans = {f.span for f in flags if f.kind == "new-identifier"}
    assert spans == {"blorp"}  # the SQL comment word is not code; the JS call is
