import json
import os
import stat
import sys
from pathlib import Path

import pytest
from test_mine_candidates import MINE_YAML, commit, git

from crucible.cli import main
from crucible.mine import candidates as cd
from crucible.mine import config as mc
from crucible.mine import gh

FAKE = Path(__file__).parent / "fake_gh.py"
URL = "https://github.com/acme/widgets/pull/{}"
MISSING = "d" * 40


def pr(number, title, sha, labels=(), branch="feature/x", files=(), issues=(), body="",
       base="main"):
    return {
        "baseRefName": base,
        "number": number, "title": title, "url": URL.format(number), "mergeCommit": {"oid": sha},
        "headRefName": branch, "labels": [{"name": n} for n in labels],
        "files": [{"path": p, "additions": 1, "deletions": 0} for p in files],
        "additions": 2, "deletions": 0, "mergedAt": "2026-01-01T00:00:00Z", "body": body,
        "closingIssuesReferences": [
            {"number": n, "url": f"https://github.com/acme/widgets/issues/{n}"} for n in issues
        ],
    }


@pytest.fixture
def world(tmp_path, monkeypatch, crucible_home):
    """A repo with squash (#1), merge-commit (#2), multi-commit rebase (#3) PRs and a PR (#4)
    whose merge commit is absent; a fake `gh` on PATH serving them."""
    r = tmp_path / "proj"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    commit(r, "init", {"README.md": "hi\n", "src/a.py": "a = 0\n", "tests/test_a.py": "x = 0\n"})
    git(r, "remote", "add", "main", "git@github.com:acme/widgets.git")
    sq = commit(r, "Update a (#1)", {"src/a.py": "a = 1\n", "tests/test_a.py": "x = 1\n"})
    git(r, "checkout", "-q", "-b", "feature/b")
    commit(r, "work b", {"src/b.py": "b = 1\n", "tests/test_b.py": "y = 1\n"})
    git(r, "checkout", "-q", "main")
    git(r, "merge", "--no-ff", "-q", "feature/b", "-m", "Merge pull request #2 from acme/feature/b")
    mg = git(r, "rev-parse", "HEAD")
    git(r, "checkout", "-q", "-b", "feature/c")
    c1 = commit(r, "c part 1", {"src/c.py": "c = 1\n"})
    c2 = commit(r, "c part 2", {"tests/test_c.py": "z = 1\n"})
    git(r, "checkout", "-q", "main")
    git(r, "cherry-pick", c1, c2)
    rb = git(r, "rev-parse", "HEAD")
    prs = [
        pr(4, "Gone", MISSING, files=["src/g.py", "tests/test_g.py"]),
        pr(3, "Add c", rb, files=["src/c.py", "tests/test_c.py"]),
        pr(2, "Add b", mg, labels=["enhancement"], files=["src/b.py", "tests/test_b.py"],
           issues=[7], body="Adds b.", branch="feature/b"),
        pr(1, "Update a", sq, labels=["bug"], files=["src/a.py", "tests/test_a.py"]),
    ]
    data = {"prs": prs, "issues": {"7": {"number": 7, "title": "b is missing",
                                         "body": "Please add b.", "url": "u"}}}
    (tmp_path / "gh.json").write_text(json.dumps(data), encoding="utf-8")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    shim = bindir / "gh"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n', encoding="utf-8")
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_GH_DATA", str(tmp_path / "gh.json"))
    monkeypatch.setenv("FAKE_GH_ARGV_FILE", str(tmp_path / "argv.jsonl"))
    (crucible_home / "mine.yaml").write_text(MINE_YAML, encoding="utf-8")
    r = r.resolve()
    monkeypatch.chdir(r)
    return {"repo": r, "sq": sq, "mg": mg, "rb": rb, "data": data, "bindir": bindir,
            "argv": tmp_path / "argv.jsonl", "gh_json": tmp_path / "gh.json"}


def run(capsys, *argv, code=0):
    assert main(["mine", *argv]) == code
    cap = capsys.readouterr()
    # the "not calibrated" note is covered in test_mine_calibrate.py
    err = "".join(ln for ln in cap.err.splitlines(keepends=True) if "calibrat" not in ln)
    return cap.out, err


def listing(capsys, *argv):
    out, err = run(capsys, "--json", "--show-excluded", *argv)
    return json.loads(out), err


def test_gh_source_lists_prs_and_excludes_rebase_and_missing(world, capsys):
    out, err = listing(capsys)
    assert err == ""
    got = {c["id"]: c for c in out["candidates"]}
    assert list(got) == ["2", "1"]
    assert got["2"]["url"] == URL.format(2) and got["2"]["source"] == URL.format(2)
    assert got["2"]["labels"] == ["enhancement"] and got["2"]["branch"] == "feature/b"
    assert got["2"]["merge"] == world["mg"] and got["2"]["body"] == "Adds b."
    assert got["1"]["labels"] == ["bug"]
    assert [f["path"] for f in got["2"]["test_files"]] == ["tests/test_b.py"]
    why = {e["id"]: e["reason"] for e in out["excluded"]}
    assert why["3"] == "rebase merge: diff doesn't match PR"
    assert why["4"] == "merge commit not fetched; run `git fetch`"
    calls = [json.loads(ln) for ln in world["argv"].read_text().splitlines()]
    assert all("acme/widgets" in c for c in calls)  # --repo derived from the `main` remote
    assert calls[0][:2] == ["pr", "list"] and "merged" in calls[0]


def test_bug_label_beats_neutral_title(world, capsys):
    out, _ = listing(capsys)
    c = next(c for c in out["candidates"] if c["id"] == "1")
    assert c["title"] == "Update a" and c["suggested_kind"] == "bugfix"
    assert next(c for c in out["candidates"] if c["id"] == "2")["suggested_kind"] == "feature"


def test_linked_issue_fetched_only_for_candidates_passing_filters(world, capsys):
    data = world["data"]
    data["prs"][0]["closingIssuesReferences"] = [{"number": 9, "url": "u"}]  # PR 4: excluded
    world["gh_json"].write_text(json.dumps(data), encoding="utf-8")
    out, _ = listing(capsys)
    assert next(c for c in out["candidates"] if c["id"] == "2")["issues"][0]["title"] == "b is missing"
    viewed = [json.loads(ln)[2] for ln in world["argv"].read_text().splitlines()
              if json.loads(ln)[:2] == ["issue", "view"]]
    assert viewed == ["7"]


def test_unsupported_field_is_dropped(world, capsys):
    data = world["data"]
    data["unsupported"] = ["closingIssuesReferences"]
    world["gh_json"].write_text(json.dumps(data), encoding="utf-8")
    out, err = listing(capsys)
    assert err == "" and [c["id"] for c in out["candidates"]] == ["2", "1"]
    assert out["candidates"][0]["issues"] == []


def assert_local(out, err, reason):
    lines = [ln for ln in err.splitlines() if ln.strip()]
    assert len(lines) == 1 and "using local git history" in lines[0] and reason in lines[0]
    ids = [c["id"] for c in json.loads(out)["candidates"]]
    assert ids and all(not i.isdigit() or i in {"1", "2"} for i in ids)
    assert json.loads(out)["candidates"][0]["source"].startswith("commit:")


def test_fallback_gh_missing(world, capsys, monkeypatch):
    clean = world["bindir"].parent / "nogh"
    clean.mkdir()
    for tool in ("git", "python3"):
        (clean / tool).symlink_to(__import__("shutil").which(tool))
    monkeypatch.setenv("PATH", str(clean))
    out, err = run(capsys, "--json")
    assert_local(out, err, "gh not found on PATH")


def test_fallback_gh_auth_failure(world, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_GH_EXIT", "1")
    out, err = run(capsys, "--json")
    assert_local(out, err, "exit 1")
    assert "gh auth login" in err


def test_fallback_non_github_remote(world, capsys):
    git(world["repo"], "remote", "set-url", "main", "https://gitlab.com/acme/widgets.git")
    out, err = run(capsys, "--json")
    assert_local(out, err, "no GitHub remote")
    assert not world["argv"].exists()  # gh never called


def test_local_flag_skips_gh(world, capsys):
    out, err = run(capsys, "--json", "--local")
    assert err == "" and not world["argv"].exists()
    assert json.loads(out)["candidates"][0]["source"].startswith("commit:")


def test_github_repo_url_forms(world):
    repo = world["repo"]
    for url in ("https://github.com/acme/widgets", "https://github.com/acme/widgets.git",
                "ssh://git@github.com/acme/widgets.git", "git@github.com:acme/widgets.git"):
        git(repo, "remote", "set-url", "main", url)
        assert gh.github_repo(repo) == ("main", "acme/widgets")


def test_show_includes_issue_and_split_diffs(world, capsys):
    out, _ = run(capsys, "show", "2", "--json")
    c = json.loads(out)
    assert c["id"] == "2" and c["labels"] == ["enhancement"] and c["branch"] == "feature/b"
    assert c["issues"][0]["number"] == 7
    assert c["issues"][0]["title"] == "b is missing" and c["issues"][0]["body"] == "Please add b."
    assert "tests/test_b.py" in c["test_diff"] and "src/b.py" not in c["test_diff"]
    assert "+y = 1" in c["test_diff"]
    assert "src/b.py" in c["non_test_diff"] and "tests/test_b.py" not in c["non_test_diff"]
    assert "+b = 1" in c["non_test_diff"]
    assert c["excluded"] is None


def test_show_excluded_and_unknown(world, capsys):
    out, _ = run(capsys, "show", "3", "--json")
    assert json.loads(out)["excluded"] == "rebase merge: diff doesn't match PR"
    out, err = run(capsys, "show", "99", "--json", code=1)
    assert "no candidate" in err


def test_show_local_source(world, capsys):
    out, _ = run(capsys, "--local", "show", "2", "--json")
    c = json.loads(out)
    assert c["source"].startswith("commit:") and c["issues"] == []
    assert "+y = 1" in c["test_diff"] and "+b = 1" in c["non_test_diff"]


def snapshot(repo: Path) -> dict[str, bytes]:
    snap = {}
    for p in sorted(repo.rglob("*")):
        rel = p.relative_to(repo)
        if p.is_file() and rel.parts[0] != ".crucible":
            snap[str(rel)] = p.read_bytes()
    return snap


@pytest.mark.parametrize("argv", [["--json"], ["--local", "--show-excluded"], ["show", "2", "--json"]])
def test_nothing_written_outside_crucible(world, capsys, argv):
    before = snapshot(world["repo"])
    run(capsys, *argv)
    assert snapshot(world["repo"]) == before


def test_pr_candidates_api(world):
    cfg = mc.load_config(Path(os.environ["CRUCIBLE_HOME"]) / "mine.yaml")
    found, pre, slug = gh.pr_candidates(world["repo"], cfg, 10)
    assert slug == "acme/widgets" and isinstance(found[0], cd.Candidate)
    assert {e.candidate.id for e in pre} == {"3", "4"}


def reload(world, **changes):
    world["gh_json"].write_text(json.dumps({**world["data"], **changes}), encoding="utf-8")


def test_prs_sorted_by_merged_at_newest_first(world, capsys):
    for n, when in {1: "2026-03-01T00:00:00Z", 2: "2026-02-01T00:00:00Z"}.items():
        next(p for p in world["data"]["prs"] if p["number"] == n)["mergedAt"] = when
    reload(world)
    out, _ = listing(capsys)
    assert [c["id"] for c in out["candidates"]] == ["1", "2"]
    prs = [{"number": 1}, {"number": 2, "mergedAt": "2026-01-01T00:00:00Z"},
           {"number": 3}, {"number": 4, "mergedAt": "2026-05-01T00:00:00Z"}]
    assert [p["number"] for p in gh.sort_newest_first(prs)] == [4, 2, 1, 3]


@pytest.mark.parametrize("url", ["https://mygithub.com/acme/widgets.git",
                                 "git@notgithub.com:acme/widgets",
                                 "https://evil.com/github.com/acme/widgets"])
def test_lookalike_hosts_are_not_github(world, url):
    git(world["repo"], "remote", "set-url", "main", url)
    with pytest.raises(gh.GhUnavailable, match="no GitHub remote"):
        gh.github_repo(world["repo"])


def test_failing_issue_view_keeps_the_gh_result(world, capsys):
    next(p for p in world["data"]["prs"] if p["number"] == 2)["closingIssuesReferences"] = [
        {"number": 7, "url": "u7"}, {"number": 9, "url": "u9"}]
    reload(world)
    out, err = listing(capsys)
    assert err == ""
    issues = next(c for c in out["candidates"] if c["id"] == "2")["issues"]
    assert issues[0]["title"] == "b is missing"
    assert issues[1]["number"] == 9 and "title" not in issues[1] and "error" in issues[1]


def test_branch_filters_gh_prs_by_base(world, capsys):
    next(p for p in world["data"]["prs"] if p["number"] == 2)["baseRefName"] = "release"
    reload(world)
    out, _ = listing(capsys, "--branch", "release")
    assert [c["id"] for c in out["candidates"]] == ["2"]
    out, _ = listing(capsys)  # no --branch: no filter
    assert [c["id"] for c in out["candidates"]] == ["2", "1"]


def test_truncated_pr_file_list_needs_subset_not_equality(world, capsys, monkeypatch):
    monkeypatch.setattr(gh, "FILES_CAP", 1)
    pr2 = next(p for p in world["data"]["prs"] if p["number"] == 2)
    pr2["files"] = [{"path": "src/b.py"}]  # truncated: local diff also has tests/test_b.py
    pr1 = next(p for p in world["data"]["prs"] if p["number"] == 1)
    pr1["files"] = [{"path": "src/elsewhere.py"}]  # not in the local diff: still a mismatch
    reload(world)
    out, _ = listing(capsys)
    assert [c["id"] for c in out["candidates"]] == ["2"]
    assert {e["id"]: e["reason"] for e in out["excluded"]}["1"] == gh.REBASE
