import json
import os
import subprocess
from pathlib import Path

import pytest
from test_mine_scaffold import FILLED, fill, scaffold, tasks  # noqa: F401
from test_mine_verify import PY, RepoInfo, cache, commit, git, verify

from crucible import workspace
from crucible.cli import main
from crucible.mine import candidates as cands
from crucible.mine import config as mc
from crucible.mine import leak

CMD = f"{PY} -m pytest -q -p no:cacheprovider {{tests}}"


def yaml_for(b_command: str = CMD, b_setup: str = "") -> str:
    lines = ["suites:"]
    for name, cmd, setup in (("a", CMD, ""), ("b", b_command, b_setup)):
        lines += [f"  - name: {name}", f'    test_paths: ["{name}/tests/**"]', f"    cwd: {name}",
                  "    env: {PYTHONPATH: .}"]
        if setup:
            lines.append(f"    setup: {setup}")
        lines.append(f"    test_command: {json.dumps(cmd)}")
    return "\n".join(lines) + "\n"


def src(n: int) -> str:
    return f"def f():\n    return {n}\n"


def tsrc(n: int, name="test_f") -> str:
    return f"from src.m import f\n\ndef {name}():\n    assert f() == {n}\n"


@pytest.fixture
def repo(tmp_path, monkeypatch, crucible_home):
    r = tmp_path / "proj"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    commit(r, "init", {"README.md": "hi\n", "a/src/__init__.py": "", "a/src/m.py": src(0),
                       "b/src/__init__.py": "", "b/src/m.py": src(0)})
    sha = {}
    sha["both"] = commit(r, "fix both", {
        "a/src/m.py": src(1), "b/src/m.py": src(1),
        "a/tests/test_a.py": tsrc(1), "b/tests/test_b.py": tsrc(1)})
    sha["onlya"] = commit(r, "fix a only", {
        "a/src/m.py": src(2), "a/tests/test_a2.py": tsrc(2),
        "b/tests/test_b2.py": "def test_ok():\n    assert True\n"})
    sha["bfails"] = commit(r, "b broken", {
        "a/src/m.py": src(3), "a/tests/test_a3.py": tsrc(3),
        "b/tests/test_b3.py": tsrc(99)})
    (crucible_home / "mine.yaml").write_text(yaml_for(), encoding="utf-8")
    r = r.resolve()
    monkeypatch.chdir(r)
    return RepoInfo(r, sha)


def ids(repo, *names):
    return [repo.sha[n][:8] for n in names]


def test_listing_shows_both_suites(repo, capsys):
    assert main(["mine", "--local", "--json"]) == 0
    by = {c["merge"]: c for c in json.loads(capsys.readouterr().out)["candidates"]}
    assert by[repo.sha["both"]]["suites"] == ["a", "b"]
    assert by[repo.sha["both"]]["suite"] == "a"
    assert main(["mine", "--local"]) == 0
    assert "a+b" in capsys.readouterr().out


def test_verified_variants(repo, crucible_home, capsys):
    both, onlya, bfails = ids(repo, "both", "onlya", "bfails")
    assert verify("--verify", both, onlya, bfails) == 0
    out = capsys.readouterr().out
    assert f"{both}: verified" in out
    assert f"{onlya}: verified" in out
    assert f"{bfails}: rejected: fails at merge (b)" in out
    c = cache(crucible_home, repo.sha["both"])
    assert c["suites"] == ["a", "b"]
    assert set(c["logs"]) == {"base-a-test", "base-b-test", "merge-a-test", "merge-b-test"}
    assert "failed" in Path(c["logs"]["base-a-test"]).read_text()
    assert "passed" in Path(c["logs"]["merge-b-test"]).read_text()


def test_setup_failure_of_second_suite(repo, crucible_home, capsys):
    (crucible_home / "mine.yaml").write_text(yaml_for(b_setup="exit 1"))
    cid = ids(repo, "both")[0]
    assert verify("--verify", cid) == 0
    assert f"{cid}: rejected: setup failed at base (b)" in capsys.readouterr().out
    assert not (crucible_home / "mine" / "verify" / f"{repo.sha['both']}.json").exists()


def test_each_suite_gets_only_its_files(repo, crucible_home, tmp_path):
    rec = tmp_path / "rec.txt"
    cmd = f"echo {{tests}} >> {rec}; false"
    (crucible_home / "mine.yaml").write_text(yaml_for(b_command=cmd).replace(
        json.dumps(CMD), json.dumps(f"echo {{tests}} >> {rec}; false")))
    verify("--verify", ids(repo, "both")[0])
    assert set(rec.read_text().split()) == {"tests/test_a.py", "tests/test_b.py"}  # never the other suite's files


def test_editing_second_suite_invalidates_cache(repo, crucible_home, capsys):
    cid = ids(repo, "both")[0]
    verify("--verify", cid)
    capsys.readouterr()
    verify("--verify", cid)
    assert "(cached)" in capsys.readouterr().out
    (crucible_home / "mine.yaml").write_text(yaml_for(b_command=CMD + " -x"))
    verify("--verify", cid)
    assert "(cached)" not in capsys.readouterr().out


def _check(repo, d, ref):
    ws = d.parent.parent / f"ws-{ref}"
    ws.mkdir()
    workspace.export_tree(repo.path, ref, ws)
    return subprocess.run(["bash", str(d / "check.sh")], cwd=ws, capture_output=True, text=True, check=False,
                          env={"PATH": os.environ["PATH"], "CRUCIBLE_TASK_DIR": str(d)})


def test_scaffold_prove_and_check_runs_both_suites(repo, crucible_home, capsys):
    cid = ids(repo, "both")[0]
    assert verify("--verify", cid) == 0
    assert scaffold(cid) == 0
    d = tasks(crucible_home) / f"commit-{cid}-fix-both"
    meta = json.loads((d / ".mine.json").read_text())
    assert meta["suites"] == ["a", "b"]
    fill(d)
    capsys.readouterr()
    assert main(["mine", "prove", str(d)]) == 0
    assert capsys.readouterr().out.startswith(f"proven: {d.name} (merge check ")

    def run_check(ref):
        return _check(repo, d, ref)

    base = run_check(repo.sha["both"] + "^")
    assert base.returncode != 0
    assert base.stdout.count("== suite") == 2 and "== suite b ==" in base.stdout
    assert base.stdout.count("failed") >= 2  # both suites ran and reported
    merge = run_check(repo.sha["both"])
    assert merge.returncode == 0 and merge.stdout.count("passed") == 2


def test_check_fails_when_only_one_suite_fails_and_setup_exits_97(repo, crucible_home):
    cid = ids(repo, "onlya")[0]
    verify("--verify", cid)
    assert scaffold(cid) == 0
    d = tasks(crucible_home) / f"commit-{cid}-fix-a-only"
    ws = d.parent.parent / "ws"
    ws.mkdir()
    workspace.export_tree(repo.path, repo.sha["onlya"] + "^", ws)
    env = {"PATH": os.environ["PATH"], "CRUCIBLE_TASK_DIR": str(d)}
    r = subprocess.run(["bash", str(d / "check.sh")], cwd=ws, capture_output=True, text=True, check=False, env=env)
    assert r.returncode != 0 and "== suite b ==" in r.stdout  # b still ran (and passed)
    text = (d / "check.sh").read_text()
    assert "exit 97" not in text  # no setup configured
    (d / "check.sh").write_text(text.replace(
        "echo '== suite b =='", "echo '== suite b =='; ( cd b; bash -c false ) || { exit 97; }"))
    r = subprocess.run(["bash", str(d / "check.sh")], cwd=ws, capture_output=True, text=True, check=False, env=env)
    assert r.returncode == 97


def test_leakcheck_uses_all_suites(repo, crucible_home):
    base = git(repo.path, "rev-parse", f"{repo.sha['both']}^")
    cfg = mc.load_config(crucible_home / "mine.yaml")
    changes = cands.file_changes(repo.path, base, repo.sha["both"])
    tests, other, owners = cands.split_files(changes, cfg.suites)
    assert owners == ["a", "b"]
    assert sorted(f.path for f in tests) == ["a/tests/test_a.py", "b/tests/test_b.py"]
    assert all("/tests/" not in f.path for f in other)
    flags = leak.check_text(repo.path, base, repo.sha["both"], cfg,
                            [("What", "Make f() return 1 in both a and b.")])
    assert flags == []
