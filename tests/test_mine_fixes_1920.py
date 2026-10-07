# ruff: noqa: F811  (the `repo` fixture is imported from test_mine_verify)
import dataclasses
import os
import stat
from pathlib import Path

from test_mine_scaffold import FILLED, fill, scaffold, tasks  # noqa: F401
from test_mine_verify import (  # noqa: F401
    PY,
    cache,
    commit,
    counter,
    git,
    ids,
    mine_yaml,
    repo,
    verify,
)

from crucible import workspace
from crucible.cli import main
from crucible.mine import candidates as cands
from crucible.mine import scaffold as sc
from crucible.mine.config import load_config


def _mode_repo(repo, crucible_home, command):
    (crucible_home / "mine.yaml").write_text(mine_yaml(command), encoding="utf-8")
    r = Path(repo.path)
    (r / "tests").mkdir(exist_ok=True)
    (r / "tests/fixture.sh").write_text("#!/bin/sh\nexit 0\n")
    (r / "tests/fixture.sh").chmod(0o755)
    (r / "tests/test_e.py").write_text(
        "import subprocess\nfrom src.e import E\n\ndef test_e():\n    assert E == 1\n"
        "    assert subprocess.run(['tests/fixture.sh']).returncode == 0\n")
    (r / "src").mkdir(exist_ok=True)
    (r / "src/e.py").write_text("E = 1\n")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "exec fixture")
    return git(r, "rev-parse", "HEAD")


def test_exec_bit_preserved_verify_scaffold_prove(repo, crucible_home, capsys):
    sha = _mode_repo(repo, crucible_home, f"{PY} -m pytest -q -p no:cacheprovider {{tests}}")
    cid = sha[:8]
    assert verify("--verify", cid) == 0
    assert f"{cid}: verified" in capsys.readouterr().out
    assert scaffold(cid) == 0
    td = tasks(crucible_home) / f"commit-{cid}-exec-fixture"
    hidden = td / "hidden_tests/tests/fixture.sh"
    assert hidden.stat().st_mode & 0o111
    fill(td)
    capsys.readouterr()
    assert main(["mine", "prove", str(td)]) == 0, capsys.readouterr().err


def test_overlay_gives_exec_mode_and_symlink(repo, crucible_home, tmp_path):
    from crucible.mine import verify as vf
    r = Path(repo.path)
    (r / "tests").mkdir(exist_ok=True)
    (r / "tests/fixture.sh").write_text("#!/bin/sh\nexit 0\n")
    (r / "tests/fixture.sh").chmod(0o755)
    (r / "tests/link.sh").symlink_to("fixture.sh")
    (r / "tests/test_x.py").write_text("def test_x():\n    pass\n")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "modes")
    merge = git(r, "rev-parse", "HEAD")
    cfg = load_config(crucible_home / "mine.yaml")
    c = cands.local_candidates(r, cfg)[0]
    assert c.merge == merge
    dest = tmp_path / "ovl"
    dest.mkdir()
    workspace.export_tree(r, c.base, dest)
    vf._overlay(r, c, dest)
    assert stat.S_IMODE((dest / "tests/fixture.sh").stat().st_mode) == 0o755
    assert (dest / "tests/link.sh").is_symlink()
    assert os.readlink(dest / "tests/link.sh") == "fixture.sh"
    hid = tmp_path / "hid"
    sc._write_hidden_tests(r, c, hid)
    assert stat.S_IMODE((hid / "tests/fixture.sh").stat().st_mode) == 0o755
    assert (hid / "tests/link.sh").is_symlink()


def test_timeout_not_cached(repo, crucible_home, capsys):
    (crucible_home / "mine.yaml").write_text(mine_yaml(
        f"sleep 2; {PY} -m pytest -q -p no:cacheprovider {{tests}}"), encoding="utf-8")
    cid = ids(repo, "fix")[0]
    assert verify("--verify", cid, "--timeout", "0.5") == 0
    assert "rejected: timeout" in capsys.readouterr().out
    assert not (crucible_home / "mine/verify" / f"{repo.sha['fix']}.json").exists()
    assert verify("--verify", cid, "--timeout", "60") == 0
    assert f"{cid}: verified" in capsys.readouterr().out
    assert cache(crucible_home, repo.sha["fix"])["outcome"] == "verified"


def test_setup_failure_not_cached(repo, crucible_home, capsys):
    (crucible_home / "mine.yaml").write_text(
        mine_yaml(f"{PY} -m pytest -q {{tests}}", setup="exit 1"), encoding="utf-8")
    cid = ids(repo, "fix")[0]
    assert verify("--verify", cid) == 0
    assert "setup failed at base" in capsys.readouterr().out
    assert not (crucible_home / "mine/verify" / f"{repo.sha['fix']}.json").exists()


def test_test_outside_module_root_rejected(repo, crucible_home, capsys, counter):
    (crucible_home / "mine.yaml").write_text(
        "suites:\n  - name: py\n    test_paths: [\"tests/**\"]\n    module_root: src\n"
        "    env: {PYTHONPATH: .}\n"
        f'    test_command: "echo run >> {counter}; {PY} -m unittest {{modules}}"\n',
        encoding="utf-8")
    cid = ids(repo, "fix")[0]
    assert verify("--verify", cid) == 0
    assert "rejected: test outside module_root" in capsys.readouterr().out
    assert not counter.exists()


def test_slug_prefix_by_pr_not_digits():
    c = cands.Candidate(
        id="12345678", title="Fix thing", base="a" * 40, merge="12345678" + "b" * 32,
        test_files=[], other_files=[], lines_changed=1, suites=["py"], suggested_kind="bugfix",
        source="commit:" + "12345678" + "b" * 32)
    assert sc.default_slug(c).startswith("commit-12345678-")
    c2 = dataclasses.replace(c, source="https://github.com/o/r/pull/12345678")
    assert sc.default_slug(c2).startswith("pr-12345678-")
    c3 = dataclasses.replace(c, id="42")  # local candidate whose subject had (#42)
    assert sc.default_slug(c3).startswith("pr-42-")


# --- write_blob never writes through a symlinked directory ---------------------------

def test_write_blob_rejects_symlinked_parent(repo, tmp_path):
    import pytest

    from crucible.mine import verify as vf
    r = Path(repo.path)
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "tests").symlink_to(outside, target_is_directory=True)
    sha = git(r, "rev-parse", "HEAD")
    with pytest.raises(vf.UnsafePathError):
        vf.write_blob(r, sha, "src/calc.py", "100644", root / "tests" / "calc.py", root)
    assert list(outside.iterdir()) == []
    vf.write_blob(r, sha, "src/calc.py", "100644", root / "ok" / "deep" / "calc.py", root)
    assert (root / "ok" / "deep" / "calc.py").is_file()


def test_verify_rejects_overlay_through_symlink(repo, crucible_home, tmp_path, monkeypatch):
    from crucible.mine import verify as vf
    r = Path(repo.path)
    cfg = load_config(crucible_home / "mine.yaml")
    outside = tmp_path / "outside"
    outside.mkdir()
    real_export = workspace.export_tree

    def export_with_link(repo_, ref, dest):
        real_export(repo_, ref, dest)
        if not (dest / "tests").exists():
            os.symlink(outside, dest / "tests")

    monkeypatch.setattr(vf.workspace, "export_tree", export_with_link)
    c = next(x for x in cands.local_candidates(r, cfg) if x.merge == repo.sha["fix"])
    res = vf.verify_candidate(r, c, cfg.suites[0], force=True)
    assert (res.outcome, res.reason) == ("rejected", "unsafe path (symlink)")
    assert list(outside.iterdir()) == []
