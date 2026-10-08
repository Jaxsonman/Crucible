import json

import pytest
import yaml

from crucible import aggregate
from crucible.cli import main

M = 1_000_000


def make_record(home, task="t1", batch="20261001T000000Z-aaaa", config="sonnet:medium", n=1,
                success=True, wall=10.0, tokens_by_model=None, sha="sha-one", kind="feature",
                computed=999.0, check=None, judge=None, resolved_model=None, status="completed"):
    model, effort = config.split(":")
    slug = f"{model}-{effort}-{n}"
    d = home / "results" / task / batch / slug
    d.mkdir(parents=True)
    tbm = tokens_by_model or {"claude-sonnet-5-5": {"input": M}}
    tbm = {m: {"input": 0, "output": 0, "cache_creation": 0, "cache_read": 0, **t}
           for m, t in tbm.items()}
    if check is None:  # reports recompute success from the check, so derive it from `success`
        check = {"ran": success is not None, "passed": success,
                 "exit_code": None if success is None else (0 if success else 1)}
    rec = {
        "schema_version": 1, "task_id": task, "task_kind": kind, "batch_id": batch, "attempt": n,
        "config": {"model": model, "effort": effort}, "prompt_sha256": sha,
        "resolved_model": resolved_model, "wall_seconds": wall, "status": status,
        "tokens": {"input": sum(t["input"] for t in tbm.values()), "output": 0,
                   "cache_creation": 0, "cache_read": 0},
        "tokens_by_model": tbm, "cost_usd": {"computed": computed, "reported": 0.0},
        "check": check, "judge": judge or {"ran": False, "passed": None},
        "success": success,
    }
    (d / "record.json").write_text(json.dumps(rec))
    return rec


def sonnet(inp):
    return {"claude-sonnet-5-5": {"input": inp}}


def opus(inp):
    return {"claude-opus-5-5": {"input": inp}}


def fixture_two_configs(home, batch="20261001T000000Z-aaaa"):
    # sonnet:medium: T,T,F ; walls 10,20,30 ; tokens 1M,2M,3M ; $2,$4,$6
    for n, (ok, w, t) in enumerate([(True, 10, 1), (True, 20, 2), (False, 30, 3)], 1):
        make_record(home, batch=batch, config="sonnet:medium", n=n, success=ok, wall=w,
                    tokens_by_model=sonnet(t * M))
    # opus:high: F,F,F ; walls 40,10,30 ; tokens 1M,2M,3M ; $4,$8,$12
    for n, (w, t) in enumerate([(40, 1), (10, 2), (30, 3)], 1):
        make_record(home, batch=batch, config="opus:high", n=n, success=False, wall=w,
                    tokens_by_model=opus(t * M))


def parse_table(out):
    rows = [ln for ln in out.splitlines() if ln.startswith("|")]
    cells = [[c.strip() for c in ln.strip("|").split("|")] for ln in rows]
    header = cells[0]
    return [dict(zip(header, r)) for r in cells[2:]]


def run_report(capsys, *args):
    code = main(["report", *args])
    cap = capsys.readouterr()
    return code, cap.out, cap.err


def test_two_configs_three_attempts(crucible_home, capsys):
    fixture_two_configs(crucible_home)
    code, out, _ = run_report(capsys, "t1")
    assert code == 0
    rows = parse_table(out)
    assert [r["Config"] for r in rows] == ["sonnet:medium", "opus:high"]
    s, o = rows
    assert (s["Attempts"], s["Successes"], s["Unknown"], s["Unpriced"]) == ("3", "2", "0", "0")
    assert s["Success rate"] == "67% (21–94%)"
    assert s["Median time (s)"] == "20.0" and s["Time range (s)"] == "10.0–30.0"
    assert s["Median tokens"] == "2,000,000"
    assert s["Median cost"] == "$4.0000"
    assert s["Cost/success"] == "$6.0000"  # (2+4+6) / 2 successes
    assert (o["Attempts"], o["Successes"]) == ("3", "0")
    assert o["Success rate"] == "0% (0–56%)"
    assert o["Median time (s)"] == "30.0" and o["Time range (s)"] == "10.0–40.0"
    assert o["Median tokens"] == "2,000,000"
    assert o["Median cost"] == "$8.0000"
    assert o["Cost/success"] == "—"  # zero successes


def test_header_line(crucible_home, capsys):
    fixture_two_configs(crucible_home)
    _, out, _ = run_report(capsys, "t1")
    head = out.split("|")[0]
    assert "t1" in head and "feature" in head and "sha-one" in head
    assert "batches: 1" in head
    assert "WARNING" not in out


def test_even_and_odd_medians(crucible_home):
    for n, (w, t) in enumerate([(10, 1), (20, 2), (30, 3), (40, 4)], 1):
        make_record(crucible_home, config="sonnet:medium", n=n, wall=w, tokens_by_model=sonnet(t * M))
    recs = [json.loads(p.read_text()) for p in crucible_home.glob("results/*/*/*/record.json")]
    recs.sort(key=lambda r: r["wall_seconds"])
    row = aggregate.aggregate_configs(recs)[0]
    assert row["median_wall_seconds"] == 25.0
    assert row["median_tokens"] == 2_500_000
    assert row["median_cost"] == 5.0  # 2,4,6,8
    odd = aggregate.aggregate_configs(recs[:3])[0]
    assert odd["median_wall_seconds"] == 20.0 and odd["median_cost"] == 4.0


def test_aggregate_pools_records_across_tasks(crucible_home):
    make_record(crucible_home, task="a", config="sonnet:medium", wall=10, tokens_by_model=sonnet(M))
    make_record(crucible_home, task="b", config="sonnet:medium", wall=30, tokens_by_model=sonnet(3 * M),
                success=False)
    recs = [json.loads(p.read_text()) for p in crucible_home.glob("results/*/*/*/record.json")]
    (row,) = aggregate.aggregate_configs(recs)
    assert row["attempts"] == 2 and row["successes"] == 1 and row["success_rate"] == 0.5
    assert row["median_wall_seconds"] == 20.0 and row["cost_per_success"] == 8.0


def test_unknown_success_rate(crucible_home, capsys):
    for n in (1, 2, 3):
        make_record(crucible_home, config="haiku:low", n=n, success=None)
    make_record(crucible_home, config="sonnet:low", n=1, success=True)
    make_record(crucible_home, config="sonnet:low", n=2, success=None)
    make_record(crucible_home, config="sonnet:low", n=3, success=False)
    _, out, _ = run_report(capsys, "t1")
    rows = {r["Config"]: r for r in parse_table(out)}
    h = rows["haiku:low"]
    assert h["Success rate"] == "—" and h["Unknown"] == "3" and h["Successes"] == "0"
    assert h["Cost/success"] == "—"
    s = rows["sonnet:low"]
    assert s["Success rate"] == "50% (9–91%)" and s["Unknown"] == "1" and s["Attempts"] == "3"


def test_unpriced_excluded_from_cost(crucible_home, capsys):
    make_record(crucible_home, n=1, tokens_by_model=sonnet(M), success=True)
    make_record(crucible_home, n=2, tokens_by_model=sonnet(3 * M), success=True)
    make_record(crucible_home, n=3, tokens_by_model={"mystery-9": {"input": M}}, success=True)
    make_record(crucible_home, config="opus:low", n=1, tokens_by_model={"mystery-9": {"input": M}})
    _, out, _ = run_report(capsys, "t1")
    rows = {r["Config"]: r for r in parse_table(out)}
    s = rows["sonnet:medium"]
    assert s["Unpriced"] == "1" and s["Attempts"] == "3"
    assert s["Median cost"] == "$4.0000"  # median of $2, $6 only
    assert s["Cost/success"] == "$4.0000"  # $8 priced total / 2 priced successes
    o = rows["opus:low"]
    assert o["Unpriced"] == "1" and o["Median cost"] == "—" and o["Cost/success"] == "—"


def test_reprice_ignores_stored_cost_and_follows_pricing_yaml(crucible_home, capsys):
    fixture_two_configs(crucible_home)
    before = {p: p.read_bytes() for p in (crucible_home / "results").rglob("*") if p.is_file()}
    _, out1, _ = run_report(capsys, "t1")
    s1 = {r["Config"]: r for r in parse_table(out1)}["sonnet:medium"]
    assert s1["Median cost"] == "$4.0000"  # not the stored 999.0

    pf = crucible_home / "pricing.yaml"
    data = yaml.safe_load(pf.read_text())
    for field in ("input", "output", "cache_write", "cache_read"):
        data["models"]["claude-sonnet-5-5"][field] *= 2
    pf.write_text(yaml.safe_dump(data))
    _, out2, _ = run_report(capsys, "t1")
    rows = {r["Config"]: r for r in parse_table(out2)}
    assert rows["sonnet:medium"]["Median cost"] == "$8.0000"
    assert rows["sonnet:medium"]["Cost/success"] == "$12.0000"
    assert rows["opus:high"]["Median cost"] == "$8.0000"  # opus untouched
    after = {p: p.read_bytes() for p in (crucible_home / "results").rglob("*") if p.is_file()}
    assert after == before


def test_batch_filter(crucible_home, capsys):
    fixture_two_configs(crucible_home, batch="20261001T000000Z-aaaa")
    for n in (1, 2):
        make_record(crucible_home, batch="20261002T000000Z-bbbb", config="sonnet:medium", n=n,
                    success=True, wall=50.0, tokens_by_model=sonnet(M))
    _, out, _ = run_report(capsys, "t1")
    s = {r["Config"]: r for r in parse_table(out)}["sonnet:medium"]
    assert s["Attempts"] == "5" and "batches: 2" in out
    _, out, _ = run_report(capsys, "t1", "--batch", "20261002T000000Z-bbbb")
    rows = parse_table(out)
    assert [r["Config"] for r in rows] == ["sonnet:medium"]
    assert rows[0]["Attempts"] == "2" and rows[0]["Median time (s)"] == "50.0"
    assert "batches: 1" in out


def test_unknown_batch_and_task_error(crucible_home, capsys):
    fixture_two_configs(crucible_home)
    code, out, err = run_report(capsys, "t1", "--batch", "nope")
    assert code == 1 and "nope" in err and out == ""
    code, out, err = run_report(capsys, "ghost")
    assert code == 1 and "ghost" in err


def test_prompt_sha_warning(crucible_home, capsys):
    make_record(crucible_home, batch="20261001T000000Z-aaaa", n=1, sha="sha-one")
    make_record(crucible_home, batch="20261002T000000Z-bbbb", n=1, sha="sha-two")
    _, out, _ = run_report(capsys, "t1")
    assert "WARNING" in out and "task changed between batches" in out
    assert "sha-one" in out and "sha-two" in out
    _, jout, _ = run_report(capsys, "t1", "--json")
    data = json.loads(jout)
    assert data["prompt_shas"] == ["sha-one", "sha-two"] and data["prompt_changed"] is True


def test_json_matches_table(crucible_home, capsys):
    fixture_two_configs(crucible_home)
    _, out, _ = run_report(capsys, "t1")
    code, jout, _ = run_report(capsys, "t1", "--json")
    assert code == 0
    data = json.loads(jout)
    assert data["task_id"] == "t1" and data["kind"] == "feature" and data["batches"] == 1
    assert data["prompt_shas"] == ["sha-one"] and data["prompt_changed"] is False
    s, o = data["configs"]
    assert s["config"] == "sonnet:medium" and o["config"] == "opus:high"
    assert s["attempts"] == 3 and s["successes"] == 2 and s["unknown"] == 0 and s["unpriced"] == 0
    assert s["success_rate"] == 2 / 3
    assert s["median_wall_seconds"] == 20.0 and s["min_wall_seconds"] == 10.0
    assert s["max_wall_seconds"] == 30.0
    assert s["median_tokens"] == 2_000_000
    assert s["median_cost"] == 4.0 and s["cost_per_success"] == 6.0
    assert o["success_rate"] == 0.0 and o["cost_per_success"] is None and o["median_cost"] == 8.0
    rows = parse_table(out)
    assert rows[0]["Median cost"] == f"${s['median_cost']:.4f}"
    assert rows[0]["Cost/success"] == f"${s['cost_per_success']:.4f}"
    assert rows[0]["Success rate"] == f"{s['success_rate']:.0%} (21–94%)"
    assert s["success_ci"] == pytest.approx([0.2076549551264879, 0.9385096847238394])
    assert o["success_ci"] == pytest.approx([0.0, 0.5615060804490177])
    assert rows[1]["Median cost"] == f"${o['median_cost']:.4f}"


def test_sort_by_rate_then_cost():
    def rec(cfg, ok, inp):
        m, e = cfg.split(":")
        return {"config": {"model": m, "effort": e}, "status": "completed",
                "check": {"ran": ok is not None, "passed": ok}, "success": ok,
                "wall_seconds": 1.0, "tokens_by_model": sonnet(inp)}

    recs = [rec("a:low", True, 3 * M), rec("b:low", True, M), rec("c:low", False, M),
            rec("d:low", None, M)]
    order = [r["config"] for r in aggregate.aggregate_configs(recs)]
    assert order == ["b:low", "a:low", "c:low", "d:low"]



def test_success_recomputed_check_decides_and_disagreement_counted(crucible_home, capsys):
    passed = {"ran": True, "passed": True, "exit_code": 0}
    # stored success=False is what the old AND rule wrote; the report must ignore it
    make_record(crucible_home, n=1, success=False, check=passed,
                judge={"ran": True, "passed": False})
    make_record(crucible_home, n=2, success=True, check=passed, judge={"ran": True, "passed": True})
    make_record(crucible_home, n=3, success=False,
                check={"ran": False, "passed": None, "exit_code": None},
                judge={"ran": True, "passed": False})
    _, out, _ = run_report(capsys, "t1")
    (row,) = parse_table(out)
    assert row["Successes"] == "2" and row["Unknown"] == "0" and row["Attempts"] == "3"
    assert row["Judge≠check"] == "1"
    _, jout, _ = run_report(capsys, "t1", "--json")
    (cfg,) = json.loads(jout)["configs"]
    assert cfg["judge_disagreements"] == 1 and cfg["successes"] == 2


@pytest.mark.parametrize("k,n,low,high", [
    (3, 3, 0.43849391955098227, 1.0),
    (2, 3, 0.2076549551264879, 0.9385096847238394),
    (1, 1, 0.20654329147389294, 1.0),
    (0, 1, 0.0, 0.7934567085261071),
    (4, 5, 0.3755282641185388, 0.9637768390302125),
])
def test_wilson_interval(k, n, low, high):
    lo, hi = aggregate.wilson_interval(k, n)
    assert lo == pytest.approx(low) and hi == pytest.approx(high)
    assert 0.0 <= lo <= k / n <= hi <= 1.0


def test_wilson_interval_empty_is_none():
    assert aggregate.wilson_interval(0, 0) is None


def test_single_attempt_shows_wide_interval(crucible_home, capsys):
    make_record(crucible_home, config="sonnet:low", n=1, success=True)
    make_record(crucible_home, config="opus:low", n=1, success=False)
    make_record(crucible_home, config="haiku:low", n=1, success=None)
    _, out, _ = run_report(capsys, "t1")
    rows = {r["Config"]: r for r in parse_table(out)}
    assert rows["sonnet:low"]["Success rate"] == "100% (21–100%)"
    assert rows["opus:low"]["Success rate"] == "0% (0–79%)"
    assert rows["haiku:low"]["Success rate"] == "—"
    _, jout, _ = run_report(capsys, "t1", "--json")
    by = {c["config"]: c for c in json.loads(jout)["configs"]}
    assert by["haiku:low"]["success_ci"] is None
    assert by["sonnet:low"]["success_ci"][1] == 1.0


def test_label_pooling_several_models_warns(crucible_home, capsys):
    make_record(crucible_home, config="sonnet:low", n=1, resolved_model="claude-sonnet-5-5")
    make_record(crucible_home, config="sonnet:low", n=2, resolved_model="claude-sonnet-5-6")
    make_record(crucible_home, config="sonnet:low", n=3, resolved_model=None, status="error",
                success=False)
    make_record(crucible_home, config="opus:high", n=1, resolved_model="claude-opus-5-5")
    _, out, _ = run_report(capsys, "t1")
    rows = {r["Config"]: r for r in parse_table(out)}
    assert set(rows) == {"sonnet:low", "opus:high"}  # the errored attempt forms no group
    assert rows["sonnet:low"]["Attempts"] == "3"
    warnings = [ln for ln in out.splitlines() if ln.startswith("warning:")]
    assert warnings == [
        "warning: sonnet:low pools 2 models: claude-sonnet-5-5, claude-sonnet-5-6"
    ]
    assert out.index("warning:") > out.rindex("|")
    _, jout, err = run_report(capsys, "t1", "--json")
    by = {c["config"]: c for c in json.loads(jout)["configs"]}
    assert by["sonnet:low"]["resolved_models"] == ["claude-sonnet-5-5", "claude-sonnet-5-6"]
    assert by["opus:high"]["resolved_models"] == ["claude-opus-5-5"]
    assert "warning: sonnet:low pools 2 models" in err
    _, out, _ = run_report(capsys)  # cross-task report warns too
    assert "warning: sonnet:low pools 2 models" in out
