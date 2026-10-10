# ruff: noqa: F811
"""Setup runs BEFORE the PR's test files are overlaid (a fake build tool that "compiles" the
tests during setup, like Maven test-compile / Gradle testClasses)."""

import json

import pytest
from test_mine_scaffold import fill, scaffold, tasks
from test_mine_verify import PY, cache, commit, counter, mine_yaml, repo, verify  # noqa: F401

from crucible.cli import main

# Fake compiler: fails when a test file imports a module that does not exist yet.
FAKE_BUILD = (
    "for f in $(ls tests/*.py 2>/dev/null); do "
    "m=$(sed -n 's/^from src\\.\\([a-z0-9]*\\) import.*/\\1/p' $f); "
    '[ -z "$m" ] || [ -e src/$m.py ] || { echo "cannot find symbol in $f"; exit 1; }; done')


@pytest.fixture
def newmod(repo, crucible_home):
    sha = commit(repo.path, "add mul", {
        "src/mul2.py": "def mul2(a, b):\n    return a * b\n",
        "tests/test_mul2.py": "from src.mul2 import mul2\n\ndef test_m():\n    assert mul2(2, 3) == 6\n",
    })
    (crucible_home / "mine.yaml").write_text(
        mine_yaml(f"{PY} -m pytest -q -p no:cacheprovider {{tests}}",
                  setup=json.dumps(FAKE_BUILD)), encoding="utf-8")
    return sha[:8], sha


def test_setup_sees_no_test_files_at_base(repo, newmod, crucible_home, capsys):
    cid, sha = newmod
    assert verify("--verify", cid) == 0
    assert f"{cid}: verified" in capsys.readouterr().out
    assert set(cache(crucible_home, sha)["logs"]) == {
        "base-setup", "base-test", "merge-setup", "merge-test"}


def test_check_script_runs_setup_before_installing_hidden_tests(repo, newmod, crucible_home, capsys):
    cid, _sha = newmod
    assert verify("--verify", cid) == 0
    assert scaffold(cid) == 0
    d = tasks(crucible_home) / f"commit-{cid}-add-mul"
    text = (d / "check.sh").read_text()
    assert text.index("cannot find symbol") < text.index("could not install hidden tests")
    fill(d)
    capsys.readouterr()
    assert main(["mine", "prove", str(d)]) == 0
    assert capsys.readouterr().out.startswith(f"proven: {d.name}")


def test_setup_still_gets_test_files_env(repo, newmod, crucible_home, tmp_path):
    rec = tmp_path / "rec.txt"
    cid, _ = newmod
    (crucible_home / "mine.yaml").write_text(
        mine_yaml(f"{PY} -m pytest -q -p no:cacheprovider {{tests}}",
                  setup=json.dumps(f'echo "$CRUCIBLE_TEST_FILES" > {rec}')), encoding="utf-8")
    verify("--verify", cid)
    assert rec.read_text().strip() == "tests/test_mul2.py"

