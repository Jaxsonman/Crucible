import json

from test_report import M, make_record, opus, sonnet

from crucible.cli import main


def run(capsys, *args):
    code = main(["report", *args])
    cap = capsys.readouterr()
    return code, cap.out, cap.err


def kind_section(out, kind):
    parts = out.split("## Kind: ")
    return next(p for p in parts if p.startswith(kind))


def build(home):
    # bugfix (b1, b2): sonnet:low cheap but 2/5 ; opus:high 5/5 $4 ; sonnet:high 3/3 $10 ;
    # sonnet:medium 2/2 nearly free but only 2 attempts
    for task in ("b1", "b2"):
        for n in range(1, 4 if task == "b1" else 3):
            ok = (task, n) in {("b1", 1), ("b1", 2)}
            make_record(home, task=task, kind="bugfix", config="sonnet:low", n=n, success=ok,
                        tokens_by_model=sonnet(M))
            make_record(home, task=task, kind="bugfix", config="opus:high", n=n, success=True,
                        tokens_by_model=opus(M), wall=20.0)
    for n in (1, 2):
        make_record(home, task="b1", kind="bugfix", config="sonnet:medium", n=n, success=True,
                    tokens_by_model=sonnet(1), wall=1.0)
    for n in (1, 2, 3):
        make_record(home, task="b1", kind="bugfix", config="sonnet:high", n=n,
                    success=True, tokens_by_model=sonnet(5 * M), wall=30.0)
    # feature (task f1): everything below the bar
    for n in range(1, 4):
        make_record(home, task="f1", kind="feature", config="sonnet:low", n=n, success=n == 1,
                    tokens_by_model=sonnet(M))
        make_record(home, task="f1", kind="feature", config="opus:high", n=n, success=n <= 2,
                    tokens_by_model=opus(M))


def test_two_kinds_each_get_recommendation_and_table(crucible_home, capsys):
    build(crucible_home)
    code, out, _ = run(capsys, "--min-attempts", "3")
    assert code == 0
    bug, feat = kind_section(out, "bugfix"), kind_section(out, "feature")
    assert "Recommendation: opus:high" in bug
    assert "Runner-up: sonnet:high" in bug
    assert "2 tasks" in bug and "5 attempts" in bug  # opus:high evidence
    assert "no config meets the bar" in feat
    assert "Highest success rate: opus:high" in feat
    for sec, cfgs in ((bug, ["sonnet:medium", "opus:high", "sonnet:high", "sonnet:low"]),
                      (feat, ["opus:high", "sonnet:low"])):
        table = [ln for ln in sec.splitlines() if ln.startswith("|")]
        assert len(table) == 2 + len(cfgs)
        assert [ln.split("|")[1].strip() for ln in table[2:]] == cfgs


def test_cheaper_below_threshold_not_recommended(crucible_home, capsys):
    build(crucible_home)
    _, out, _ = run(capsys)
    assert "Recommendation: sonnet:low" not in out


def test_under_min_attempts_never_recommended(crucible_home, capsys):
    build(crucible_home)
    _, out, _ = run(capsys)
    # sonnet:medium is cheapest and 100% but has only 2 attempts
    assert "Recommendation: sonnet:medium" not in out
    _, out, _ = run(capsys, "--min-attempts", "2")
    assert "Recommendation: sonnet:medium" in kind_section(out, "bugfix")


def test_threshold_flag(crucible_home, capsys):
    build(crucible_home)
    _, out, _ = run(capsys)
    assert "no config meets the bar" in kind_section(out, "feature")
    _, out, _ = run(capsys, "--threshold", "0.3")
    feat = kind_section(out, "feature")
    # sonnet:low (1/3) now qualifies too; it and opus:high (2/3) both cost $6.0000 per
    # success and take the same time, so the tie goes to the label
    assert "Recommendation: opus:high" in feat and "Runner-up: sonnet:low" in feat


def test_kind_filter(crucible_home, capsys):
    build(crucible_home)
    code, out, _ = run(capsys, "--kind", "bugfix")
    assert code == 0 and "## Kind: bugfix" in out and "feature" not in out
    code, out, err = run(capsys, "--kind", "ghost")
    assert code == 1 and "ghost" in err


def test_json_matches_text(crucible_home, capsys):
    build(crucible_home)
    _, out, _ = run(capsys)
    code, jout, _ = run(capsys, "--json")
    assert code == 0
    data = json.loads(jout)
    assert data["min_attempts"] == 3 and data["threshold"] == 0.8
    bug, feat = data["kinds"]
    assert bug["kind"] == "bugfix" and bug["tasks"] == 2 and bug["attempts"] == 15
    rec = bug["recommendation"]
    assert rec["status"] == "recommended" and rec["config"] == "opus:high"
    assert rec["evidence"]["success_rate"] == 1.0 and rec["evidence"]["tasks"] == 2
    assert rec["evidence"]["median_cost"] == 4.0 and rec["evidence"]["attempts"] == 5
    assert rec["runner_up"]["config"] == "sonnet:high"
    assert rec["runner_up"]["median_cost"] == 10.0
    assert f"${rec['evidence']['median_cost']:.4f}" in kind_section(out, "bugfix")
    assert feat["recommendation"]["status"] == "none_qualify"
    assert feat["recommendation"]["best_effort"]["config"] == "opus:high"
    assert [c["config"] for c in bug["configs"]] == [
        "sonnet:medium", "opus:high", "sonnet:high", "sonnet:low"]


def test_empty_results_error(capsys):
    code, _out, err = run(capsys)
    assert code == 1 and "no results" in err


def test_bad_flags(crucible_home, capsys):
    build(crucible_home)
    assert run(capsys, "--min-attempts", "0")[0] == 1
    assert run(capsys, "--threshold", "1.5")[0] == 1
    assert run(capsys, "--batch", "x")[0] == 1
    assert run(capsys, "b1", "--kind", "bugfix")[0] == 1


def test_evidence_shows_interval(crucible_home, capsys):
    for n in (1, 2, 3):
        make_record(crucible_home, task="r1", kind="refactor", config="sonnet:low", n=n,
                    success=True)
    _, out, _ = run(capsys)
    assert "Recommendation: sonnet:low" in out
    assert "success 100% (3/3 known, 95% CI 44–100%)" in out
    assert "| 100% (44–100%) |" in out


def test_recommendation_ranks_by_cost_per_success(crucible_home, capsys):
    # sonnet:low: $2.00 a run, 4/5 pass -> $2.50 per success (median $2.00)
    # opus:high:  $2.20 a run, 3/3 pass -> $2.20 per success (median $2.20)
    for n in range(1, 6):
        make_record(crucible_home, task="r1", kind="refactor", config="sonnet:low", n=n,
                    success=n != 5, tokens_by_model=sonnet(M))
    for n in (1, 2, 3):
        make_record(crucible_home, task="r1", kind="refactor", config="opus:high", n=n,
                    success=True, tokens_by_model=opus(550_000))
    _, out, _ = run(capsys)
    assert "Recommendation: opus:high" in out
    assert "Runner-up: sonnet:low" in out
    assert "cost per success $2.2000" in out
