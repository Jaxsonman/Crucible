"""Aggregation math for reports. Works on any list of attempt records (one task or pooled).

Costs are always re-priced from `tokens_by_model` with the current pricing table; the stored
`cost_usd.computed` is never read. See docs/design.md (Tokens and cost).
"""

import math
import statistics
from typing import Any

from crucible import pricing
from crucible.records import compute_success, judge_disagrees

Record = dict[str, Any]
Row = dict[str, Any]


def config_label(record: Record) -> str:
    cfg = record.get("config") or {}
    return f"{cfg.get('model')}:{cfg.get('effort')}"


STOP_SUBTYPES = frozenset({"error_max_turns", "error_max_budget_usd"})


def is_stopped(record: Record) -> bool:
    """The attempt was ended by a limit (timeout, turn cap, budget cap), not by the model."""
    return record.get("status") == "timeout" or record.get("subtype") in STOP_SUBTYPES


def total_tokens(record: Record) -> int:
    """Sum of every token kind over every model in the record."""
    total = 0
    for tokens in (record.get("tokens_by_model") or {}).values():
        total += sum(int(tokens.get(k) or 0) for k in pricing.TOKEN_KEYS.values())
    return total


def reprice(record: Record) -> float | None:
    """Current-price cost of one attempt, or None if any model is unpriced."""
    cost, _unpriced = pricing.cost_usd(record.get("tokens_by_model") or {})
    return cost


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


Z95 = 1.96


def wilson_interval(successes: int, n: int, z: float = Z95) -> tuple[float, float] | None:
    """95% Wilson score interval of `successes / n`, clamped to [0, 1]; None when n is 0."""
    if n <= 0:
        return None
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def summarize_config(label: str, records: list[Record]) -> Row:
    """Aggregate the attempts of a single config. Success is recomputed from each record with
    `compute_success` (the stored `success` is never read), the same way cost is re-priced
    from tokens, so older records follow the current success rule without being edited."""
    flags = [compute_success(r) for r in records]
    known = [s for s in flags if s is not None]
    successes = sum(1 for s in known if s)
    walls = [float(r["wall_seconds"]) for r in records if r.get("wall_seconds") is not None]
    priced = [(reprice(r), s) for r, s in zip(records, flags)]
    costs = [c for c, _ in priced if c is not None]
    priced_successes = sum(1 for c, s in priced if c is not None and s)
    cost_per_success = sum(costs) / priced_successes if priced_successes else None
    return {
        "config": label,
        "attempts": len(records),
        "successes": successes,
        "unknown": len(records) - len(known),
        "stopped": sum(1 for r in records if is_stopped(r)),
        "success_rate": successes / len(known) if known else None,
        "success_ci": wilson_interval(successes, len(known)),
        "median_wall_seconds": _median(walls),
        "min_wall_seconds": min(walls) if walls else None,
        "max_wall_seconds": max(walls) if walls else None,
        "median_tokens": _median([total_tokens(r) for r in records]),
        "median_cost": _median(costs),
        "cost_per_success": cost_per_success,
        "unpriced": len(records) - len(costs),
        "judge_disagreements": sum(1 for r in records if judge_disagrees(r)),
        "resolved_models": sorted(
            {r["resolved_model"] for r in records if r.get("resolved_model")}
        ),
    }


def _sort_key(row: Row) -> tuple:
    rate, cost = row["success_rate"], row["median_cost"]
    return (
        rate is None,
        -(rate or 0.0),
        cost is None,
        cost if cost is not None else 0.0,
        row["config"],
    )


def aggregate_configs(records: list[Record]) -> list[Row]:
    """Group records by config and aggregate each; sorted by success rate desc, median cost asc."""
    groups: dict[str, list[Record]] = {}
    for r in records:
        groups.setdefault(config_label(r), []).append(r)
    return sorted((summarize_config(k, v) for k, v in groups.items()), key=_sort_key)


def task_summary(task_id: str, records: list[Record]) -> dict[str, Any]:
    """Header facts for a set of one task's records."""
    kinds = sorted({r["task_kind"] for r in records if r.get("task_kind")})
    shas = sorted({r["prompt_sha256"] for r in records if r.get("prompt_sha256")})
    return {
        "task_id": task_id,
        "kind": ", ".join(kinds) if kinds else None,
        "batches": len({r.get("batch_id") for r in records}),
        "prompt_shas": shas,
        "prompt_changed": len(shas) > 1,
    }


DEFAULT_MIN_ATTEMPTS = 3
DEFAULT_THRESHOLD = 0.8
NO_KIND = "(no kind)"


def _known_attempts(row: Row) -> int:
    return row["attempts"] - row["unknown"]


def _cheapest_key(row: Row) -> tuple:
    """Lower cost per success, then lower median wall time, then label; unknown values sort
    last. Cost per success (total priced cost ÷ priced successes) answers "what does a passing
    result cost": failed attempts are part of that cost, so a config that is cheap per attempt
    but often fails does not win on price alone."""
    cps, wall = row["cost_per_success"], row["median_wall_seconds"]
    return (cps is None, cps or 0.0, wall is None, wall or 0.0, row["config"])


def _best_rate_key(row: Row) -> tuple:
    return (-(row["success_rate"] or 0.0), *_cheapest_key(row))


def recommend(
    rows: list[Row],
    min_attempts: int = DEFAULT_MIN_ATTEMPTS,
    threshold: float = DEFAULT_THRESHOLD,
) -> dict[str, Any]:
    """Pick a config from per-config rows (as produced by `summarize_config`, plus `tasks`).

    A config qualifies with at least `min_attempts` attempts of known success, a success rate
    of at least `threshold`, and a priceable cost per success. The lowest cost per success
    wins (ties go to lower median wall time, then label). If none qualifies, `best_effort` is
    the highest success rate. `runner_up` is the next config in whichever ranking applied.
    """
    qualifiers = sorted(
        (
            r for r in rows
            if _known_attempts(r) >= min_attempts
            and r["success_rate"] is not None
            and r["success_rate"] >= threshold
            and r["cost_per_success"] is not None
        ),
        key=_cheapest_key,
    )
    base = {"min_attempts": min_attempts, "threshold": threshold}
    if qualifiers:
        win = qualifiers[0]
        return {**base, "status": "recommended", "config": win["config"], "evidence": win,
                "best_effort": None,
                "runner_up": qualifiers[1] if len(qualifiers) > 1 else None}
    rated = sorted((r for r in rows if r["success_rate"] is not None), key=_best_rate_key)
    return {**base, "status": "none_qualify", "config": None, "evidence": None,
            "best_effort": rated[0] if rated else None,
            "runner_up": rated[1] if len(rated) > 1 else None}


def common_tasks(records: list[Record]) -> list[str]:
    """Sorted task ids attempted by every config that has at least one attempt in `records`."""
    by_config: dict[str, set] = {}
    for r in records:
        by_config.setdefault(config_label(r), set()).add(r.get("task_id"))
    if not by_config:
        return []
    return sorted(t for t in set.intersection(*by_config.values()) if t is not None)


def _add_task_counts(rows: list[Row], records: list[Record]) -> list[Row]:
    for row in rows:
        row["tasks"] = len(
            {r.get("task_id") for r in records if config_label(r) == row["config"]}
        )
    return rows


def kind_reports(
    records: list[Record],
    min_attempts: int = DEFAULT_MIN_ATTEMPTS,
    threshold: float = DEFAULT_THRESHOLD,
) -> list[dict[str, Any]]:
    """Per task kind: pooled per-config rows (`configs`, with `tasks` counts), the
    `common_tasks` every config attempted, `comparable` rows over only those tasks, and a
    recommendation from `comparable` so configs are compared on the same tasks. With no common
    task the recommendation status is `no_common_tasks`."""
    by_kind: dict[str, list[Record]] = {}
    for r in records:
        by_kind.setdefault(r.get("task_kind") or NO_KIND, []).append(r)
    reports = []
    for kind in sorted(by_kind):
        recs = by_kind[kind]
        rows = _add_task_counts(aggregate_configs(recs), recs)
        common = common_tasks(recs)
        wanted = set(common)
        comp_recs = [r for r in recs if r.get("task_id") in wanted]
        comparable = _add_task_counts(aggregate_configs(comp_recs), comp_recs)
        rec = recommend(comparable, min_attempts, threshold)
        if not common:
            # Same shape as every other recommendation; only the status differs.
            rec = {**rec, "status": "no_common_tasks"}
        reports.append({
            "kind": kind,
            "tasks": len({r.get("task_id") for r in recs}),
            "attempts": len(recs),
            "configs": rows,
            "common_tasks": common,
            "comparable": comparable,
            "recommendation": rec,
        })
    return reports
