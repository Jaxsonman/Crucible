"""--limit hint, docker requirement recorded at scaffold, help text."""

import json

from test_mine_scaffold import fill, scaffold, tasks
from test_mine_verify import commit, git, mine_yaml, verify

from crucible.cli import build_parser, main

GOOD = ': {tests}; grep -q "a + b" src/calc.py'


def make_repo(tmp_path, monkeypatch, n_plain=8):
    r = tmp_path / "proj"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    commit(r, "init", {"README.md": "hi\n", "src/calc.py": "def add(a, b):\n    return a - b\n"})
    commit(r, "fix add", {"src/calc.py": "def add(a, b):\n    return a + b\n",
                          "tests/test_calc.py": "# t\n"})
    for i in range(n_plain):
        commit(r, f"plain {i}", {"docs.txt": f"{i}\n"})
    r = r.resolve()
    monkeypatch.chdir(r)
    return r


def test_limit_help_says_what_it_caps():
    parser = build_parser()
    sub = next(a for a in parser._actions if a.dest == "command").choices["mine"]
    limit = next(a for a in sub._actions if "--limit" in a.option_strings)
    assert "scanned" in limit.help and "PR" in limit.help


def test_limit_hint_when_the_walk_was_cut_short_and_few_candidates(tmp_path, monkeypatch,
                                                                    crucible_home, capsys):
    make_repo(tmp_path, monkeypatch)
    (crucible_home / "mine.yaml").write_text(mine_yaml(GOOD))
    assert main(["mine", "--local", "--limit", "5"]) == 0
    assert "raise --limit" in capsys.readouterr().err
    assert main(["mine", "--local", "--limit", "5", "--json"]) == 0
    cap = capsys.readouterr()
    json.loads(cap.out)
    assert "raise --limit" in cap.err


def test_no_limit_hint_when_the_whole_history_was_walked(tmp_path, monkeypatch, crucible_home,
                                                          capsys):
    make_repo(tmp_path, monkeypatch)
    (crucible_home / "mine.yaml").write_text(mine_yaml(GOOD))
    assert main(["mine", "--local", "--limit", "300"]) == 0
    assert "raise --limit" not in capsys.readouterr().err


def scripted(crucible_home, body):
    d = crucible_home / "scripts"
    d.mkdir(exist_ok=True)
    (d / "run.sh").write_text(body)


def test_scaffold_records_and_warns_docker_requirement(tmp_path, monkeypatch, crucible_home,
                                                       capsys):
    make_repo(tmp_path, monkeypatch, 0)
    scripted(crucible_home, "#!/bin/sh\n# normally: docker run --rm golang:1.22 go test ./...\ntrue\n")
    (crucible_home / "mine.yaml").write_text(
        mine_yaml('bash "$CRUCIBLE_SCRIPTS/run.sh" {tests}; grep -q "a + b" src/calc.py'))
    cid = git(tmp_path / "proj", "log", "--format=%h", "-n", "1", "--abbrev=8")
    assert verify("--verify", cid) == 0
    capsys.readouterr()
    assert scaffold(cid) == 0
    cap = capsys.readouterr()
    assert "docker" in cap.err and "requires" in cap.err.lower()
    d = next(tasks(crucible_home).iterdir())
    assert json.loads((d / ".mine.json").read_text())["requires"] == ["docker"]
    fill(d)


def test_scaffold_without_docker_records_nothing(tmp_path, monkeypatch, crucible_home, capsys):
    make_repo(tmp_path, monkeypatch, 0)
    scripted(crucible_home, "#!/bin/sh\necho hi\n")
    (crucible_home / "mine.yaml").write_text(mine_yaml(GOOD))
    cid = git(tmp_path / "proj", "log", "--format=%h", "-n", "1", "--abbrev=8")
    assert verify("--verify", cid) == 0
    capsys.readouterr()
    assert scaffold(cid) == 0
    assert "docker" not in capsys.readouterr().err
    d = next(tasks(crucible_home).iterdir())
    assert "requires" not in json.loads((d / ".mine.json").read_text())
