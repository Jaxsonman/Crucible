"""Verify UX: progress, rejection hints, stale-config scaffold message, weak-task warnings."""

import json

import pytest
from test_mine_scaffold import fill, scaffold, tasks
from test_mine_verify import commit, mine_yaml, verify

from crucible.mine import verify as vf

ASSERT_CMD = ': {tests}; grep -q "a + b" src/calc.py'


@pytest.fixture
def git_repo(tmp_path, monkeypatch, crucible_home):
    from test_mine_verify import git
    r = tmp_path / "proj"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    commit(r, "init", {"README.md": "hi\n", "src/calc.py": "def add(a, b):\n    return a - b\n"})
    r = r.resolve()
    monkeypatch.chdir(r)
    return r


def cand(repo, test_text="def test_a():\n    pass\n", name="tests/test_calc.py"):
    sha = commit(repo, "fix add", {"src/calc.py": "def add(a, b):\n    return a + b\n",
                                   name: test_text})
    return sha[:8], sha


def cfg(home, command):
    (home / "mine.yaml").write_text(mine_yaml(command), encoding="utf-8")


def test_progress_lines_go_to_stderr_as_each_finishes(git_repo, crucible_home, capsys):
    cfg(crucible_home, ASSERT_CMD)
    cid, _ = cand(git_repo)
    assert verify("--verify", cid) == 0
    cap = capsys.readouterr()
    assert f"[1/1] {cid}: verified" in cap.err
    assert f"{cid}: verified" in cap.out


def test_rejection_prints_log_path_and_matching_hint(git_repo, crucible_home, capsys):
    cfg(crucible_home, "echo 'LoadError: cannot load such file -- rails/all'; exit 1 # {tests}")
    cid, sha = cand(git_repo)
    verify("--verify", cid)
    cap = capsys.readouterr()
    assert f"{cid}: rejected: passes at base" not in cap.out  # base fails here, merge fails too
    assert "rejected: fails at merge" in cap.out
    assert f"{sha}.merge-test.log" in cap.out
    assert "errored before tests ran (LoadError" in cap.out
    verify("--verify", cid, "--json")
    data = json.loads(capsys.readouterr().out)["verify"][0]
    assert data["hints"] and data["hints"][0]["id"] == "ruby-load-error"


def test_scaffold_says_config_changed_since_verification(git_repo, crucible_home, capsys):
    cfg(crucible_home, ASSERT_CMD)
    cid, _ = cand(git_repo)
    assert verify("--verify", cid) == 0
    cfg(crucible_home, ASSERT_CMD + " # changed")
    capsys.readouterr()
    assert scaffold(cid) == 1
    err = capsys.readouterr().err
    assert "config changed since verification; re-run --verify" in err
    assert "not verified" not in err


def test_scaffold_never_verified_message_unchanged(git_repo, crucible_home, capsys):
    cfg(crucible_home, ASSERT_CMD)
    cid, _ = cand(git_repo)
    assert scaffold(cid) == 1
    assert "not verified; run `crucible mine --verify" in capsys.readouterr().err


@pytest.mark.parametrize("text,marker", [
    ("@Disabled\nvoid t() {}\n", "@Disabled"), ("@Ignore\n", "@Ignore"),
    ("xit('x', () => {})\n", "xit("),
    ("xdescribe('x', () => {})\n", "xdescribe("), ("@pytest.mark.skip\n", "pytest.mark.skip"),
    ("t.Skip(\"no\")\n", "t.Skip("), ("skip \"later\" do\nend\n", 'skip "'),
    ("pending\n", "pending"), ("[Fact(Skip=\"x\")]\n", "[Fact(Skip="),
])
def test_skip_markers_warn_but_do_not_reject(git_repo, crucible_home, capsys, text, marker):
    cfg(crucible_home, ASSERT_CMD)
    cid, sha = cand(git_repo, text, "tests/test_calc.py")
    assert verify("--verify", cid) == 0
    out = capsys.readouterr().out
    assert f"{cid}: verified" in out
    assert "warning:" in out and marker in out
    cache = json.loads((crucible_home / "mine" / "verify" / f"{sha}.json").read_text())
    assert cache["outcome"] == "verified" and cache["warnings"]
    # a cached result keeps its warnings
    verify("--verify", cid, "--json")
    assert json.loads(capsys.readouterr().out)["verify"][0]["warnings"] == cache["warnings"]


def test_no_warning_for_a_clean_candidate(git_repo, crucible_home, capsys):
    cfg(crucible_home, ASSERT_CMD)
    cid, sha = cand(git_repo)
    verify("--verify", cid)
    assert "warning" not in capsys.readouterr().out
    cache = json.loads((crucible_home / "mine" / "verify" / f"{sha}.json").read_text())
    assert cache["warnings"] == []


def test_compile_only_base_failure_warns(git_repo, crucible_home, capsys):
    cfg(crucible_home, 'if grep -q "a + b" src/calc.py; then echo "3 passed"; else '
                       'echo "[ERROR] COMPILATION ERROR :"; false; fi # {tests}')
    cid, _ = cand(git_repo)
    assert verify("--verify", cid) == 0
    out = capsys.readouterr().out
    assert f"{cid}: verified" in out
    assert "warning:" in out and "compile" in out


def test_assertion_failure_at_base_has_no_compile_warning(git_repo, crucible_home, capsys):
    cfg(crucible_home, 'if grep -q "a + b" src/calc.py; then echo "3 passed"; else '
                       'echo "1 failed"; echo "COMPILATION ERROR in a log line"; false; fi # {tests}')
    cid, _ = cand(git_repo)
    verify("--verify", cid)
    assert "warning" not in capsys.readouterr().out


def test_old_cache_entries_without_warnings_still_load(git_repo, crucible_home):
    cfg(crucible_home, ASSERT_CMD)
    cid, sha = cand(git_repo)
    verify("--verify", cid)
    f = crucible_home / "mine" / "verify" / f"{sha}.json"
    data = json.loads(f.read_text())
    del data["warnings"]
    f.write_text(json.dumps(data))
    assert verify("--verify", cid) == 0
    assert vf.VerifyResult("a", "b", "verified").warnings == []


def test_warnings_listed_when_scaffolding_is_still_allowed(git_repo, crucible_home, capsys):
    cfg(crucible_home, ASSERT_CMD)
    cid, _ = cand(git_repo, "@pytest.mark.skip\n")
    verify("--verify", cid)
    capsys.readouterr()
    assert scaffold(cid) == 0
    fill(next(tasks(crucible_home).iterdir()))
