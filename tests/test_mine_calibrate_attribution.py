# ruff: noqa: F811
"""Calibrate attributes a multi-suite candidate's failure to the failing suite only."""

import json

from test_mine_multi_suite import CMD, repo, yaml_for  # noqa: F401

from crucible.cli import main

BROKEN = "echo {tests}; false"


def run(capsys, *argv):
    code = main(["mine", "--local", "calibrate", "--json", *argv])
    data = json.loads(capsys.readouterr().out)
    return code, {s["name"]: s for s in data["suites"]}


def test_healthy_suite_is_not_blamed_for_the_broken_one(repo, crucible_home, capsys):
    (crucible_home / "mine.yaml").write_text(yaml_for(b_command=BROKEN))
    code, s = run(capsys)
    assert code == 1
    assert s["a"]["status"] == "ok", s["a"]
    assert {x["outcome"] for x in s["a"]["samples"]} == {"verified"}
    assert s["b"]["status"] == "failing"
    assert all(x["reason"] == "fails at merge (b)" for x in s["b"]["samples"])


def test_suite_whose_part_never_ran_is_excluded_not_blamed(repo, crucible_home, capsys):
    # a (first) is broken: at merge b's tests never run, so b has no usable sample
    text = yaml_for().replace(f"test_command: {json.dumps(CMD)}\n  - name: b",
                              f"test_command: {json.dumps(BROKEN)}\n  - name: b")
    (crucible_home / "mine.yaml").write_text(text)
    _code, s = run(capsys)
    assert s["a"]["status"] == "failing"
    assert s["b"]["samples"] == [] and s["b"]["status"] == "no-candidates"
