from crucible import aggregate

M = 1_000_000


_SAME = object()


def row(config, attempts=5, successes=5, unknown=0, cost=1.0, wall=10.0, tasks=1, cps=_SAME):
    known = attempts - unknown
    return {
        "config": config, "attempts": attempts, "successes": successes, "unknown": unknown,
        "success_rate": successes / known if known else None,
        "median_cost": cost, "median_wall_seconds": wall, "tasks": tasks,
        "cost_per_success": cost if cps is _SAME else cps,
    }


def rec(task, kind, cfg, ok, inp=M, wall=10.0):
    m, e = cfg.split(":")
    return {"task_id": task, "task_kind": kind, "config": {"model": m, "effort": e},
            "status": "completed", "check": {"ran": ok is not None, "passed": ok},
            "success": ok, "wall_seconds": wall,
            "tokens_by_model": {"claude-sonnet-5-5": {"input": inp}}}


def test_picks_cheapest_qualifying():
    out = aggregate.recommend([row("a:low", cost=3.0), row("b:low", cost=1.0)])
    assert out["status"] == "recommended"
    assert out["config"] == "b:low"
    assert out["runner_up"]["config"] == "a:low"
    assert out["evidence"]["attempts"] == 5 and out["evidence"]["tasks"] == 1


def test_cheaper_below_threshold_not_recommended():
    out = aggregate.recommend([row("cheap:low", successes=3, cost=0.5), row("pricey:high", cost=9.0)])
    assert out["config"] == "pricey:high"
    assert out["runner_up"] is None


def test_threshold_boundary_exactly_point_eight_qualifies():
    out = aggregate.recommend([row("a:low", attempts=5, successes=4)])
    assert out["status"] == "recommended" and out["config"] == "a:low"


def test_just_below_threshold_does_not_qualify():
    out = aggregate.recommend([row("a:low", attempts=10, successes=7)], threshold=0.8)
    assert out["status"] == "none_qualify"
    out = aggregate.recommend([row("a:low", attempts=10, successes=8)], threshold=0.8)
    assert out["status"] == "recommended"


def test_custom_threshold():
    out = aggregate.recommend([row("a:low", attempts=4, successes=2)], threshold=0.5)
    assert out["status"] == "recommended"


def test_min_attempts_boundary():
    assert aggregate.recommend([row("a:low", attempts=3, successes=3)])["status"] == "recommended"
    assert aggregate.recommend([row("a:low", attempts=2, successes=2)])["status"] == "none_qualify"
    out = aggregate.recommend([row("a:low", attempts=2, successes=2)], min_attempts=2)
    assert out["status"] == "recommended"


def test_min_attempts_counts_known_only():
    r = row("a:low", attempts=5, successes=2, unknown=3)  # 2 known, 100%
    assert aggregate.recommend([r])["status"] == "none_qualify"


def test_underpowered_config_never_recommended_even_if_cheapest():
    out = aggregate.recommend([row("few:low", attempts=2, successes=2, cost=0.1),
                               row("many:high", cost=5.0)])
    assert out["config"] == "many:high"


def test_tie_on_cost_broken_by_wall_time():
    out = aggregate.recommend([row("slow:low", cost=2.0, wall=50.0), row("fast:low", cost=2.0, wall=5.0)])
    assert out["config"] == "fast:low"
    assert out["runner_up"]["config"] == "slow:low"


def test_all_unknown_config_never_qualifies_and_no_crash():
    r = row("u:low", attempts=4, successes=0, unknown=4)
    assert r["success_rate"] is None
    out = aggregate.recommend([r])
    assert out["status"] == "none_qualify"
    assert out["config"] is None and out["best_effort"] is None


def test_nothing_qualifies_names_highest_success_rate():
    out = aggregate.recommend([row("a:low", successes=2), row("b:low", successes=3),
                               row("c:low", attempts=1, successes=1)])
    assert out["status"] == "none_qualify"
    assert out["config"] is None
    assert out["best_effort"]["config"] == "c:low"  # 100% even though under-powered
    assert out["runner_up"]["config"] == "b:low"


def test_nothing_qualifies_rate_tie_goes_to_cheaper():
    out = aggregate.recommend([row("a:low", successes=3, cost=4.0), row("b:low", successes=3, cost=1.0)])
    assert out["best_effort"]["config"] == "b:low"


def test_unpriced_config_not_recommended_over_priced():
    out = aggregate.recommend([row("n:low", cost=None), row("p:low", cost=9.0)])
    assert out["config"] == "p:low"


def test_empty():
    assert aggregate.recommend([])["status"] == "none_qualify"


def test_kind_reports_group_pool_and_recommend():
    recs = [rec("t1", "bugfix", "a:low", True, M), rec("t2", "bugfix", "a:low", True, M),
            rec("t2", "bugfix", "a:low", True, M),
            rec("t3", "feature", "b:low", False)]
    reports = aggregate.kind_reports(recs, min_attempts=3, threshold=0.8)
    assert [r["kind"] for r in reports] == ["bugfix", "feature"]
    bug, feat = reports
    assert bug["tasks"] == 2 and bug["attempts"] == 3
    assert bug["configs"][0]["tasks"] == 2 and bug["configs"][0]["attempts"] == 3
    assert bug["recommendation"]["config"] == "a:low"
    assert feat["recommendation"]["status"] == "none_qualify"


def test_ranks_by_cost_per_success_not_median_cost():
    # flaky is cheaper per attempt but fails 1 in 5, so each pass costs more
    out = aggregate.recommend([row("flaky:low", successes=4, cost=1.0, cps=1.25),
                               row("steady:high", cost=1.1, cps=1.1)])
    assert out["config"] == "steady:high"
    assert out["runner_up"]["config"] == "flaky:low"


def test_qualifier_needs_cost_per_success():
    out = aggregate.recommend([row("n:low", cost=0.5, cps=None), row("p:low", cost=9.0)])
    assert out["config"] == "p:low" and out["runner_up"] is None


def test_cost_per_success_tie_broken_by_wall_time():
    # same cost per success but different median cost: the tie must be on cost per success
    out = aggregate.recommend([row("slow:low", cost=1.0, cps=2.0, wall=50.0),
                               row("fast:low", cost=3.0, cps=2.0, wall=5.0)])
    assert out["config"] == "fast:low"
    assert out["runner_up"]["config"] == "slow:low"
