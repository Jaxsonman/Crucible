import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from crucible import paths, workspace
from crucible.cli import main
from crucible.mine import config as mc
from crucible.mine import verify as vf

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
}
PY = sys.executable


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True,
                          env=GIT_ENV).stdout.strip()


def commit(repo: Path, msg: str, files: dict[str, str]) -> str:
    for rel, text in files.items():
        f = repo / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg)
    return git(repo, "rev-parse", "HEAD")


def mine_yaml(command: str, setup: str = "") -> str:
    lines = ["suites:", "  - name: py", '    test_paths: ["tests/**"]', "    env: {PYTHONPATH: .}"]
    if setup:
        lines.append(f"    setup: {setup}")
    lines.append(f"    test_command: {json.dumps(command)}")
    return "\n".join(lines) + "\n"


@pytest.fixture
def counter(tmp_path):
    return tmp_path / "counter.txt"


def runs(counter: Path) -> int:
    return len(counter.read_text().split()) if counter.exists() else 0


class RepoInfo(os.PathLike):
    def __init__(self, path, sha):
        self.path, self.sha = path, sha

    def __fspath__(self):
        return str(self.path)


@pytest.fixture
def repo(tmp_path, monkeypatch, crucible_home, counter):
    r = tmp_path / "proj"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    commit(r, "init", {
        "README.md": "hi\n", "src/__init__.py": "", "src/calc.py": "def add(a, b):\n    return a - b\n",
        "src/other.py": "x = 1\n", ".crucible/secret.txt": "hidden\n",
    })
    sha = {}
    sha["fix"] = commit(r, "fix add", {
        "src/calc.py": "def add(a, b):\n    return a + b\n",
        "tests/test_calc.py": "from src.calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        "tests/helpers.py": "VALUE = 1\n",
    })
    sha["passes"] = commit(r, "touch other", {
        "src/other.py": "x = 2\n",
        "tests/test_other.py": "def test_ok():\n    assert True\n",
    })
    sha["broken"] = commit(r, "break add", {
        "src/mul.py": "def mul(a, b):\n    return a + b\n",
        "tests/test_mul.py": "from src.mul import mul\n\ndef test_mul():\n    assert mul(2, 3) == 6\n",
    })
    sha["conftest"] = commit(r, "only helper", {
        "src/other.py": "x = 3\n", "tests/conftest.py": "# helper\n",
    })
    (crucible_home / "mine.yaml").write_text(
        mine_yaml(f"echo run >> {counter}; {PY} -m pytest -q -p no:cacheprovider {{tests}}"),
        encoding="utf-8")
    r = r.resolve()
    monkeypatch.chdir(r)
    return RepoInfo(r, sha)


def ids(repo, *names):
    return [repo.sha[n][:8] for n in names]


def verify(*argv) -> int:
    return main(["mine", "--local", *argv])


def cache(crucible_home, sha) -> dict:
    return json.loads((crucible_home / "mine" / "verify" / f"{sha}.json").read_text())


def test_verified_and_two_rejections(repo, crucible_home, capsys):
    f, p, b = ids(repo, "fix", "passes", "broken")
    assert verify("--verify", f, p, b) == 0
    out = capsys.readouterr().out
    assert f"{f}: verified" in out
    assert f"{p}: rejected: passes at base" in out
    assert f"{b}: rejected: fails at merge" in out
    assert cache(crucible_home, repo.sha["fix"])["outcome"] == "verified"
    assert cache(crucible_home, repo.sha["passes"])["reason"] == "passes at base"
    assert cache(crucible_home, repo.sha["broken"])["reason"] == "fails at merge"
    logs = cache(crucible_home, repo.sha["fix"])["logs"]
    assert set(logs) == {"base-test", "merge-test"}
    assert "failed" in Path(logs["base-test"]).read_text()
    assert "passed" in Path(logs["merge-test"]).read_text()


def test_no_runnable_tests(repo, crucible_home, capsys):
    assert verify("--verify", ids(repo, "conftest")[0]) == 0
    assert "rejected: no runnable tests" in capsys.readouterr().out
    assert cache(crucible_home, repo.sha["conftest"])["reason"] == "no runnable tests"


def test_command_gets_only_runnable_files(repo, crucible_home, tmp_path):
    rec = tmp_path / "rec.txt"
    (crucible_home / "mine.yaml").write_text(mine_yaml(f"echo {{tests}} > {rec}; true"))
    verify("--verify", ids(repo, "fix")[0])
    assert rec.read_text().split() == ["tests/test_calc.py"]  # helpers.py not passed


def test_setup_failure_at_base(repo, crucible_home, capsys):
    (crucible_home / "mine.yaml").write_text(mine_yaml(f"{PY} -m pytest {{tests}}", setup="exit 1"))
    assert verify("--verify", ids(repo, "fix")[0]) == 0
    assert "rejected: setup failed at base" in capsys.readouterr().out
    vdir = crucible_home / "mine" / "verify"
    assert (vdir / f"{repo.sha['fix']}.base-setup.log").exists()  # logs kept, result not cached
    assert not (vdir / f"{repo.sha['fix']}.base-test.log").exists()


def test_timeout(repo, crucible_home, capsys):
    (crucible_home / "mine.yaml").write_text(mine_yaml("sleep 30 # {tests}"))
    assert verify("--verify", ids(repo, "fix")[0], "--timeout", "1") == 0
    assert "rejected: timeout" in capsys.readouterr().out


def test_cache_force_and_invalidation(repo, crucible_home, counter, capsys):
    f = ids(repo, "fix")[0]
    verify("--verify", f)
    assert runs(counter) == 2  # base + merge
    capsys.readouterr()
    verify("--verify", f)
    assert runs(counter) == 2
    assert f"{f}: verified (cached)" in capsys.readouterr().out
    verify("--verify", f, "--force")
    assert runs(counter) == 4
    # changing the suite's settings invalidates the cache
    text = (crucible_home / "mine.yaml").read_text().replace("-q ", "-q -x ")
    (crucible_home / "mine.yaml").write_text(text)
    verify("--verify", f)
    assert runs(counter) == 6


def test_listing_shows_verified_column(repo, capsys):
    f, p = ids(repo, "fix", "passes")
    verify("--verify", f, p)
    capsys.readouterr()
    main(["mine", "--local"])
    out = capsys.readouterr().out
    assert "verified" in out.splitlines()[1]
    rows = {ln.split()[0]: ln for ln in out.splitlines() if ln[:1].isalnum()}
    assert rows[f].rstrip().endswith("yes")
    assert "no: passes at base" in rows[p]
    assert not rows[ids(repo, "broken")[0]].rstrip().endswith(("yes", "base"))
    main(["mine", "--local", "--json"])
    data = {c["id"]: c["verified"] for c in json.loads(capsys.readouterr().out)["candidates"]}
    assert data[f] == "yes" and data[p] == "no: passes at base"
    assert data[ids(repo, "broken")[0]] is None


def test_verify_errors(repo, capsys):
    assert verify("--verify") == 2
    assert "at least one" in capsys.readouterr().err
    assert verify("--verify", "zzzz", ids(repo, "fix")[0]) == 1
    cap = capsys.readouterr()
    assert "zzzz: skipped" in cap.err
    assert "verified" in cap.out  # the valid id still ran
    # an excluded candidate (the init commit has no base) says why
    root = git(repo.path, "rev-list", "--max-parents=0", "HEAD")[:8]
    assert verify("--show-excluded", "--verify", root) == 1
    assert "root commit" in capsys.readouterr().err


def test_workspaces_cleaned_or_kept_without_crucible_dir(repo, capsys):
    before = (git(repo.path, "status", "--porcelain"), git(repo.path, "rev-parse", "HEAD"),
              git(repo.path, "branch", "--list"), git(repo.path, "stash", "list"))
    f = ids(repo, "fix")[0]
    verify("--verify", f)
    assert not list((paths.work_dir() / "mine").iterdir())
    capsys.readouterr()
    verify("--verify", f, "--force", "--keep")
    kept = [ln.split("kept ")[1] for ln in capsys.readouterr().out.splitlines() if "kept " in ln]
    assert len(kept) == 2
    for d in map(Path, kept):
        assert (d / "src" / "calc.py").exists()
        assert not (d / ".crucible").exists()
    assert len(list((paths.work_dir() / "mine").iterdir())) == 2
    after = (git(repo.path, "status", "--porcelain"), git(repo.path, "rev-parse", "HEAD"),
             git(repo.path, "branch", "--list"), git(repo.path, "stash", "list"))
    assert before == after


def test_base_has_merge_tests_overlaid(repo, tmp_path, crucible_home):
    """At base the new test file exists (overlaid) while the source is still the old code."""
    rec = tmp_path / "rec.txt"
    cmd = f"cat src/calc.py tests/test_calc.py >> {rec}; false"
    (crucible_home / "mine.yaml").write_text(mine_yaml(f"{cmd} # {{tests}}"))
    verify("--verify", ids(repo, "fix")[0])
    first = rec.read_text().split("def add", 1)[1].split("def add", 1)[0]  # the base run
    assert "return a - b" in first and "def test_add" in first


def test_build_command_django_style():
    suite = mc.Suite(
        name="backend", test_paths=["backend/**"], cwd="backend", module_root="backend",
        test_command="python manage.py test {modules}",
    )
    cmd = vf.build_command(suite, ["backend/shop/tests/test_cart.py", "backend/shop/tests.py"])
    assert cmd == "python manage.py test shop.tests.test_cart shop.tests"
    s2 = mc.Suite(name="w", test_paths=["w/**"], cwd="w", test_command="npx vitest run {tests}")
    assert vf.build_command(s2, ["w/src/a b.test.js"]) == "npx vitest run 'src/a b.test.js'"


def test_is_runnable_test():
    for ok in ("tests/test_a.py", "a/b_test.py", "app/tests.py", "src/x.test.ts", "x.spec.js",
               "src/__tests__/thing.js"):
        assert vf.is_runnable_test(ok), ok
    for no in ("tests/conftest.py", "tests/helpers.py", "tests/fixtures/data.json", "tests/__init__.py"):
        assert not vf.is_runnable_test(no), no


def test_export_tree_excludes_crucible(repo, tmp_path):
    dest = tmp_path / "out"
    dest.mkdir()
    workspace.export_tree(repo.path, "HEAD", dest)
    assert (dest / "src" / "calc.py").exists()
    assert not (dest / ".crucible").exists()
