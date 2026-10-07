import json
import os
import subprocess
from pathlib import Path

import pytest

from crucible.cli import main
from crucible.mine import candidates as cd
from crucible.mine import config as mc

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
}

MINE_YAML = """\
suites:
  - name: py
    test_paths: ["tests/**"]
    test_command: python -m pytest {tests}
  - name: web
    test_paths: ["web/**/*.test.js"]
    cwd: web
    test_command: npx vitest run {tests}
"""


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True, env=GIT_ENV
    ).stdout.strip()


def write(repo: Path, rel: str, text: str) -> None:
    f = repo / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text, encoding="utf-8")


def commit(repo: Path, msg: str, files: dict[str, str | None]) -> str:
    for rel, text in files.items():
        if text is None:
            (repo / rel).unlink()
        else:
            write(repo, rel, text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg)
    return git(repo, "rev-parse", "HEAD")


def lines(n: int) -> str:
    return "".join(f"line {i}\n" for i in range(n))


@pytest.fixture
def repo(tmp_path, monkeypatch, crucible_home):
    r = tmp_path / "proj"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    commit(r, "init", {"README.md": "hi\n", "src/a.py": "a = 0\n", "tests/test_a.py": "x = 0\n"})
    (crucible_home / "mine.yaml").write_text(MINE_YAML, encoding="utf-8")
    r = r.resolve()
    monkeypatch.chdir(r)
    return r


@pytest.fixture
def story(repo):
    """The seven-commit history used by these tests. Returns name -> merge sha."""
    sha = {}
    sha["squash"] = commit(repo, "Add a (#12)", {"src/a.py": "a = 1\n", "tests/test_a.py": "x = 1\n"})
    git(repo, "checkout", "-q", "-b", "feature/b")
    commit(repo, "work on b", {"src/b.py": "b = 1\n", "tests/test_b.py": "y = 1\n"})
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "--no-ff", "-q", "feature/b", "-m",
        "Merge pull request #13 from me/feature/b\n\nAdd b feature")
    sha["merge"] = git(repo, "rev-parse", "HEAD")
    sha["docs"] = commit(repo, "docs only", {"docs/guide.md": "g\n", "README.md": "hello\n"})
    sha["lock"] = commit(repo, "bump lock", {"yarn.lock": "l\n", "tests/test_a.py": "x = 2\n"})
    sha["testonly"] = commit(repo, "more tests", {"tests/test_c.py": "z\n"})
    sha["codeonly"] = commit(repo, "code only", {"src/c.py": "c\n"})
    sha["large"] = commit(repo, "big one", {"src/big.py": lines(2000), "tests/test_big.py": "t\n"})
    return sha


def run_json(capsys, *argv):
    assert main(["mine", "--json", *argv]) == 0
    return json.loads(capsys.readouterr().out)


def test_two_good_candidates_newest_first(repo, story, capsys):
    out = run_json(capsys)
    assert out["excluded"] == []
    got = out["candidates"]
    assert [c["id"] for c in got] == ["13", "12"]
    merge, squash = got
    assert merge["merge"] == story["merge"]
    assert merge["base"] == git(repo, "rev-parse", f"{story['merge']}^1")
    assert merge["title"] == "Add b feature"
    assert merge["branch"] == "me/feature/b"
    assert merge["source"] == f"commit:{story['merge']}"
    assert merge["suite"] == "py"
    assert merge["test_files"] == [{"path": "tests/test_b.py", "status": "added", "added": 1, "deleted": 0}]
    assert merge["other_files"] == [{"path": "src/b.py", "status": "added", "added": 1, "deleted": 0}]
    assert merge["lines_changed"] == 2
    assert squash["base"] == git(repo, "rev-parse", f"{story['squash']}^")
    assert squash["title"] == "Add a (#12)"
    assert [f["path"] for f in squash["test_files"]] == ["tests/test_a.py"]
    assert squash["test_files"][0]["status"] == "modified"
    assert squash["lines_changed"] == 4
    assert squash["suggested_kind"] == "feature"
    for key in ("url", "labels", "branch", "issues", "body"):
        assert key in squash


def test_size_cap_ignores_excluded_paths(repo, capsys):
    sha = commit(repo, "Add d (#30)", {
        "src/d.py": lines(30), "tests/test_d.py": lines(20),
        "package-lock.json": lines(5000), "docs/plan.md": lines(300)})
    out = run_json(capsys, "--limit", "1")
    c = out["candidates"][0]
    assert c["merge"] == sha
    assert c["lines_changed"] == 5350
    assert c["counted_lines"] == 50


def test_size_cap_still_counts_real_code_and_tests(repo, capsys):
    sha = commit(repo, "Big (#31)", {
        "src/big.py": lines(1500), "tests/test_big.py": lines(500), "yarn.lock": lines(10)})
    out = run_json(capsys, "--limit", "1", "--show-excluded")
    (e,) = out["excluded"]
    assert e["merge"] == sha
    assert e["reason"] == "2000 lines changed (max 800)"
    assert e["lines_changed"] == 2010 and e["counted_lines"] == 2000


def test_show_excluded_lists_reasons(repo, story, capsys):
    out = run_json(capsys, "--show-excluded")
    reasons = {e["merge"]: e["reason"] for e in out["excluded"]}
    assert reasons[story["docs"]] == "no test file changed"
    assert reasons[story["lock"]] == "only excluded paths changed besides tests"
    assert reasons[story["testonly"]] == "no non-test file changed"
    assert reasons[story["codeonly"]] == "no test file changed"
    assert reasons[story["large"]] == "2001 lines changed (max 800)"
    root = [e for e in out["excluded"] if e["reason"] == "root commit"]
    assert len(root) == 1 and len(out["excluded"]) == 6
    assert [c["id"] for c in out["candidates"]] == ["13", "12"]


def test_table_output(repo, story, capsys):
    assert main(["mine"]) == 0
    out = capsys.readouterr().out
    assert "Add b feature" in out and "2 candidate(s), 6 excluded" in out
    assert "big one" not in out
    assert main(["mine", "--show-excluded"]) == 0
    out = capsys.readouterr().out
    assert "big one" in out and "no test file changed" in out


def test_max_lines_brings_large_back(repo, story, capsys):
    out = run_json(capsys, "--max-lines", "5000")
    assert out["candidates"][0]["merge"] == story["large"]
    assert out["candidates"][0]["lines_changed"] == 2001


def test_limit(repo, story, capsys):
    out = run_json(capsys, "--limit", "4", "--show-excluded")
    assert len(out["candidates"]) + len(out["excluded"]) == 4


def test_branch_option(repo, story, capsys):
    out = run_json(capsys, "--branch", story["merge"])
    assert [c["id"] for c in out["candidates"]] == ["13", "12"]


def test_pr_number_parsing():
    assert cd.parse_pr_number("Merge pull request #7 from a/b") == 7
    assert cd.parse_pr_number("Fix the thing (#42)") == 42
    assert cd.parse_pr_number("Fix issue #5 properly") is None
    assert cd.parse_pr_number("Fix (#3) now") is None


def test_id_falls_back_to_short_sha(repo, capsys):
    sha = commit(repo, "plain", {"src/a.py": "a = 5\n", "tests/test_a.py": "x = 5\n"})
    [c] = run_json(capsys)["candidates"]
    assert c["id"] == sha[:8]


@pytest.mark.parametrize("labels,branch,title,kind", [
    ([], "fix/login", "Update login", "bugfix"),
    ([], "hotfix/x", "Update", "bugfix"),
    ([], "refactor/cart", "Update", "refactor"),
    ([], "feat/x", "Fix typo", "feature"),
    ([], None, "Refactor cart", "refactor"),
    ([], None, "Fix crash on login", "bugfix"),
    ([], None, "Cleanup helpers", "refactor"),
    ([], None, "Add dark mode", "feature"),
    ([], None, "Prefix handling", "feature"),
    (["bug"], "feat/x", "Add", "bugfix"),
    (["Refactor"], None, "Fix", "refactor"),
    (["enhancement"], "fix/x", "Fix", "feature"),
])
def test_suggested_kind(labels, branch, title, kind):
    assert cd.suggest_kind(labels, branch, title) == kind


def test_kind_from_merged_branch_name(repo, capsys):
    git(repo, "checkout", "-q", "-b", "fix/login")
    commit(repo, "tweak", {"src/a.py": "a = 9\n", "tests/test_a.py": "x = 9\n"})
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "--no-ff", "-q", "fix/login", "-m", "Merge branch 'fix/login'")
    [c] = run_json(capsys)["candidates"]
    assert c["branch"] == "fix/login" and c["suggested_kind"] == "bugfix"


def test_title_kind_via_cli(repo, capsys):
    commit(repo, "Refactor cart", {"src/a.py": "a = 9\n", "tests/test_a.py": "x = 9\n"})
    [c] = run_json(capsys)["candidates"]
    assert c["suggested_kind"] == "refactor"


def test_already_mined(repo, story, crucible_home, capsys):
    tdir = crucible_home / "tasks" / "mined-a"
    tdir.mkdir()
    (tdir / "task.md").write_text(
        f"---\nid: mined-a\nsource: commit:{story['squash']}\n---\n", encoding="utf-8"
    )
    out = run_json(capsys, "--show-excluded")
    assert [c["id"] for c in out["candidates"]] == ["13"]
    reasons = {e["merge"]: e["reason"] for e in out["excluded"]}
    assert reasons[story["squash"]] == "already mined"


def test_already_mined_by_ref_and_merge(repo, story, crucible_home, capsys):
    base = git(repo, "rev-parse", f"{story['merge']}^1")
    tdir = crucible_home / "tasks" / "m2"
    tdir.mkdir()
    (tdir / "task.md").write_text(
        f"---\nid: m2\nref: {base}\nsource: https://x/pr/13 {story['merge']}\n---\n", encoding="utf-8"
    )
    (crucible_home / "tasks" / "broken").mkdir()
    (crucible_home / "tasks" / "broken" / "task.md").write_text("---\n: [\n---\n")
    out = run_json(capsys)
    assert [c["id"] for c in out["candidates"]] == ["12"]


def test_suite_spanning_is_a_candidate_with_all_suites(repo, capsys):
    both = commit(repo, "touch both", {
        "src/a.py": "a = 7\n", "tests/test_a.py": "x = 7\n", "web/a.test.js": "t\n",
    })
    web = commit(repo, "web only", {"src/a.py": "a = 8\n", "web/a.test.js": "t2\n"})
    out = run_json(capsys, "--show-excluded")
    by = {c["merge"]: c for c in out["candidates"]}
    assert by[both]["suites"] == ["py", "web"]
    assert by[both]["suite"] == "py"
    assert by[web]["suites"] == ["web"] and by[web]["suite"] == "web"
    assert not [e for e in out["excluded"] if e["merge"] == both]


def test_manifest_only_excluded(repo, capsys):
    sha = commit(repo, "deps", {"package.json": "{}\n", "tests/test_a.py": "x = 3\n"})
    out = run_json(capsys, "--show-excluded")
    [e] = [e for e in out["excluded"] if e["merge"] == sha]
    assert e["reason"] == "dependency manifest only besides tests"


def test_file_status_added_modified_deleted_renamed_binary(repo, capsys):
    (repo / "bin.dat").write_bytes(b"\x00\x01\x02")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "binary")
    git(repo, "mv", "src/a.py", "src/renamed.py")
    (repo / "bin.dat").write_bytes(b"\x00\x09\x09\x09")
    write(repo, "tests/new file.py", "n\n")
    (repo / "tests/test_a.py").unlink()
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "mixed")
    [c] = run_json(capsys)["candidates"]
    tests = {f["path"]: f["status"] for f in c["test_files"]}
    other = {f["path"]: (f["status"], f["added"], f["deleted"]) for f in c["other_files"]}
    assert tests == {"tests/new file.py": "added", "tests/test_a.py": "deleted"}
    assert other["src/a.py"] == ("deleted", 0, 1)
    assert other["src/renamed.py"] == ("added", 1, 0)
    assert other["bin.dat"] == ("modified", 0, 0)
    assert c["lines_changed"] == 1 + 1 + 1 + 1


def test_git_state_untouched(repo, story):
    def state():
        return (git(repo, "status", "--porcelain"), git(repo, "rev-parse", "HEAD"),
                git(repo, "for-each-ref"), git(repo, "branch", "--show-current"))
    before = state()
    for argv in (["mine"], ["mine", "--json", "--show-excluded"], ["mine", "--max-lines", "9999"]):
        assert main(argv) == 0
        assert state() == before


def test_bad_branch_exits_1(repo, capsys):
    assert main(["mine", "--branch", "nope"]) == 1
    assert "nope" in capsys.readouterr().err


def test_local_candidates_api(repo, story):
    cfg = mc.load_config(Path(os.environ["CRUCIBLE_HOME"]) / "mine.yaml")
    found = cd.local_candidates(repo, cfg)
    assert found[0].merge == story["large"]
    assert isinstance(found[0], cd.Candidate)
    assert found[-1].base == ""  # root commit


def _mined_dir(tasks, slug, source, merge=None, meta_source=None):
    d = tasks / slug
    d.mkdir(parents=True)
    (d / "task.md").write_text(f"---\nid: {slug}\nref: {'b' * 40}\nsource: {source}\n---\nx\n")
    if merge:
        (d / ".mine.json").write_text(json.dumps({"merge": merge, "source": meta_source or source}))


def _cand(source, merge):
    return cd.Candidate(id="1", title="t", base="b" * 40, merge=merge, test_files=[],
                        other_files=[], lines_changed=1, suites=["py"], suggested_kind="bugfix",
                        source=source)


def test_already_mined_matches_across_sources(tmp_path):
    merge = "a" * 40
    url = "https://github.com/o/r/pull/1"
    _mined_dir(tmp_path, "from-gh", url, merge)
    _mined_dir(tmp_path, "from-local", f"commit:{'c' * 40}")
    mined = cd.mined_tasks(tmp_path)
    assert cd._already_mined(_cand(f"commit:{merge}", merge), mined)  # gh task, local mining
    assert cd._already_mined(_cand(url, "c" * 40), mined)  # local task, gh mining
    assert not cd._already_mined(_cand("commit:" + "e" * 40, "e" * 40), mined)


def test_mine_meta_for_another_source_is_ignored(tmp_path):
    _mined_dir(tmp_path, "t", "https://github.com/o/r/pull/1", "a" * 40, meta_source="other")
    assert cd.mined_tasks(tmp_path)[0].merge is None


def test_max_lines_zero_rejected(repo, story, capsys):
    with pytest.raises(SystemExit) as e:
        main(["mine", "--max-lines", "0"])
    assert e.value.code == 2
    assert "--max-lines" in capsys.readouterr().err
