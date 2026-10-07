from pathlib import Path

import pytest

from crucible import configs
from crucible.cli import main

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize(
    "text, msg",
    [
        ("opus", "model:effort"),
        ("opus:turbo", "effort"),
        (":high", "model"),
        ("nope:high", "unknown model"),
        ("haiku:high", "does not support effort"),
    ],
)
def test_parse_config_rejects(text, msg):
    with pytest.raises(configs.ConfigError, match=msg):
        configs.parse_config(text)


def test_parse_config_ok_and_slug():
    c = configs.parse_config("opus:high")
    assert (c.model, c.effort) == ("opus", "high")
    assert c.slug == "opus-high"


def test_parse_configs_list():
    cs = configs.parse_configs("opus:high, sonnet:medium")
    assert [c.slug for c in cs] == ["opus-high", "sonnet-medium"]
    with pytest.raises(configs.ConfigError):
        configs.parse_configs("opus:high,bad")
    with pytest.raises(configs.ConfigError):
        configs.parse_configs("")


def test_load_matrix(crucible_home):
    f = crucible_home / "configs" / "m.yaml"
    f.write_text("configs: [opus:high, sonnet:medium]\n")
    assert [c.slug for c in configs.load_matrix(f)] == ["opus-high", "sonnet-medium"]


@pytest.mark.parametrize("body", ["configs: []\n", "foo: 1\n", "configs: [opus:turbo]\n", "- a\n"])
def test_load_matrix_invalid(crucible_home, body):
    f = crucible_home / "configs" / "m.yaml"
    f.write_text(body)
    with pytest.raises(configs.ConfigError):
        configs.load_matrix(f)


def test_list_matrices_and_named_load(crucible_home):
    (crucible_home / "configs" / "b.yaml").write_text("configs: [opus:low]\n")
    (crucible_home / "configs" / "a.yaml").write_text("configs: [opus:low]\n")
    assert configs.list_matrices() == ["a", "b"]
    assert configs.load_named("a")[0].slug == "opus-low"
    with pytest.raises(configs.ConfigError, match="not found"):
        configs.load_named("zzz")


def test_repo_default_matrix(monkeypatch):
    monkeypatch.setenv("CRUCIBLE_HOME", str(REPO_ROOT))
    cs = configs.load_named("default")
    assert [c.slug for c in cs] == ["opus-high", "opus-medium", "sonnet-high", "sonnet-medium"]


def test_cli_configs_lists_matrices(crucible_home, capsys):
    (crucible_home / "configs" / "default.yaml").write_text("configs: [opus:high]\n")
    assert main(["configs"]) == 0
    assert "default" in capsys.readouterr().out


def test_cli_configs_named_prints_resolved_and_prices(crucible_home, capsys):
    (crucible_home / "configs" / "default.yaml").write_text(
        "configs: [opus:high, opus:medium, sonnet:high, sonnet:medium]\n"
    )
    assert main(["configs", "default"]) == 0
    out = capsys.readouterr().out
    assert out.count("claude-opus-5-5") == 2 and out.count("claude-sonnet-5-5") == 2
    assert "$4.00" in out and "$20.00" in out and "$2.00" in out


def test_cli_configs_unknown_matrix(capsys):
    assert main(["configs", "zzz"]) == 1
    assert "not found" in capsys.readouterr().err


def test_cli_configs_invalid_matrix(crucible_home, capsys):
    (crucible_home / "configs" / "bad.yaml").write_text("configs: [opus:turbo]\n")
    assert main(["configs", "bad"]) == 1
    assert "effort" in capsys.readouterr().err
