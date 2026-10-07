import pytest

from crucible import __version__
from crucible.cli import main

SUBCOMMANDS = ["task", "run", "judge", "report", "configs"]


def test_help_lists_subcommands(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for name in SUBCOMMANDS:
        assert name in out


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == "0.1.0" == __version__


def test_task_without_subcommand_exits_2():
    assert main(["task"]) == 2
