"""`crucible run`: matrix runs and attempt records. See docs/design.md."""

import json
import secrets
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from crucible import checker, executor, judge, paths, pricing, records, workspace
from crucible.configs import Config
from crucible.executor import ExecResult
from crucible.task import Task

INTERRUPTED = "interrupted"


@dataclass
class Attempt:
    config: Config
    n: int

    @property
    def slug(self) -> str:
        return f"{self.config.slug}-{self.n}"


def new_batch_id() -> str:
    return f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{secrets.token_hex(2)}"


def plan(configs: list[Config], repeat: int) -> list[Attempt]:
    """Repeat-major: all configs for repeat 1, then repeat 2, ..."""
    return [Attempt(c, n) for n in range(1, repeat + 1) for c in configs]


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_record(
    task: Task,
    batch_id: str,
    attempt: Attempt,
    prompt_sha: str,
    baseline: str | None,
    started_at: str,
    res: ExecResult,
    computed_cost: float | None,
    check: checker.CheckResult | None = None,
) -> records.Record:
    rec: records.Record = {
        "schema_version": 1,
        "task_id": task.id,
        "task_kind": task.kind,
        "task_source_lines": task.source_lines,
        "batch_id": batch_id,
        "attempt": attempt.n,
        "config": {"model": attempt.config.model, "effort": attempt.config.effort},
        "resolved_model": res.resolved_model,
        "prompt_sha256": prompt_sha,
        "baseline_sha": baseline,
        "started_at": started_at,
        "wall_seconds": round(res.wall_seconds, 3),
        "status": res.status,
        "error": res.error,
        "num_turns": res.num_turns,
        "session_id": res.session_id,
        "subtype": res.subtype,
        "reported_duration_ms": res.reported_duration_ms,
        "tokens": res.tokens or dict.fromkeys(executor.TOKEN_FIELDS.values(), 0),
        "tokens_by_model": res.tokens_by_model,
        "cost_usd": {"computed": computed_cost, "reported": res.reported_cost_usd},
        "check": (check or checker.CheckResult()).as_record(),
        "judge": {
            "ran": False, "passed": None, "criteria_passed": None,
            "criteria_total": None, "judge_config": None, "judge_cost_usd": None,
        },
        "success": None,
    }
    rec["success"] = records.compute_success(rec)
    return rec


def format_line(attempt: Attempt, rec: records.Record) -> str:
    t = rec["tokens"]
    total = sum(t.values())
    cost = rec["cost_usd"]["computed"]
    cost_s = f"${cost:.4f}" if cost is not None else "$?"
    return (
        f"{attempt.config.label} #{attempt.n}  {rec['status']}  {rec['wall_seconds']:.1f}s  "
        f"{total} tokens  {cost_s}"
    )


def run_attempt(
    task: Task,
    batch_id: str,
    attempt: Attempt,
    prompt_text: str,
    prompt_sha: str,
    max_budget_usd: float,
    keep_workspaces: bool,
    check_timeout_s: float | None = None,
    judge_config: Config | None = None,
) -> tuple[records.Record, bool]:
    """Run and record one attempt. Returns (record, interrupted)."""
    attempt_dir = paths.results_dir() / task.id / batch_id / attempt.slug
    if attempt_dir.exists():
        raise FileExistsError(f"refusing to overwrite existing attempt {attempt_dir}")
    started_at = _now()
    ws: Path | None = None
    baseline: str | None = None
    res: ExecResult
    diff_text = ""
    check = checker.CheckResult()
    interrupted = False
    t0 = time.monotonic()
    try:
        ws = workspace.create(task, batch_id, attempt.slug)
        baseline = workspace.baseline_sha(ws)
        res = executor.execute(
            prompt_text, attempt.config, ws, task.max_turns,
            task.timeout_minutes * 60, max_budget_usd,
        )
    except KeyboardInterrupt:
        interrupted = True
        res = ExecResult(
            status="error", error=INTERRUPTED, wall_seconds=time.monotonic() - t0
        )
    except Exception as e:  # noqa: BLE001 - any failure must not abort the batch
        res = ExecResult(
            status="error",
            error=records.redact(f"{type(e).__name__}: {e}")[:500],
            wall_seconds=time.monotonic() - t0,
        )
    try:
        if ws is not None:
            try:
                diff_text = workspace.diff(ws)
            except Exception as e:  # noqa: BLE001
                print(f"warning: could not capture diff: {e}", file=sys.stderr)
    except KeyboardInterrupt:
        interrupted = True
        res = ExecResult(
            status="error", error=INTERRUPTED, wall_seconds=time.monotonic() - t0
        )
    if ws is not None and not interrupted:
        try:
            check = checker.run_check(task, ws, checker.timeout_for(task, check_timeout_s))
        except KeyboardInterrupt:
            interrupted = True
            res = ExecResult(
                status="error", error=INTERRUPTED, wall_seconds=time.monotonic() - t0
            )
        except Exception as e:  # noqa: BLE001
            check = checker.CheckResult(
                ran=True, passed=False, log=f"check failed to run: {type(e).__name__}: {e}\n"
            )
    cost, unpriced = pricing.cost_usd(res.tokens_by_model)
    if unpriced:
        print(
            f"warning: no price for model(s) {', '.join(unpriced)}; cost_usd.computed is null",
            file=sys.stderr,
        )
    rec = build_record(task, batch_id, attempt, prompt_sha, baseline, started_at, res, cost, check)
    outcome = None
    if judge_config is not None and not interrupted and rec["status"] == "completed":
        try:
            outcome = judge.judge_attempt(
                task.criteria, diff_text, check.log if check.ran else None, judge_config
            )
            judge.apply(rec, outcome)
        except KeyboardInterrupt:
            interrupted = True
        except Exception as e:  # noqa: BLE001 - a judge failure must not lose the attempt
            print(f"warning: judge failed: {type(e).__name__}: {e}", file=sys.stderr)
    records.write_record(attempt_dir, rec)
    records.write_text(attempt_dir / "output.json", res.raw_output)
    records.write_text(attempt_dir / "diff.patch", diff_text)
    if check.ran:
        records.write_text(attempt_dir / "check.log", check.log)
    if outcome is not None and rec["judge"]["ran"]:
        records.write_text(attempt_dir / "judge.json", outcome.judge_json())
    if ws is not None and not keep_workspaces:
        try:
            workspace.cleanup(ws)
        except Exception as e:  # noqa: BLE001
            print(f"warning: could not remove workspace {ws}: {e}", file=sys.stderr)
    return rec, interrupted


def judge_existing(
    task: Task, batch_id: str | None, judge_config: Config
) -> list[tuple[Path, records.Record]]:
    """Judge attempts under results/<task>/ that have no judge.json. Returns judged attempts."""
    root = paths.results_dir() / task.id
    batches = [root / batch_id] if batch_id else sorted(p for p in root.glob("*") if p.is_dir())
    done: list[tuple[Path, records.Record]] = []
    for batch in batches:
        for rec_file in sorted(batch.glob("*/record.json")):
            adir = rec_file.parent
            if (adir / "judge.json").exists():
                continue
            try:
                rec = json.loads(rec_file.read_text(encoding="utf-8"))
                diff = (adir / "diff.patch").read_text(encoding="utf-8", errors="replace")
                log_file = adir / "check.log"
                log = log_file.read_text(encoding="utf-8", errors="replace") if log_file.exists() else None
            except (OSError, ValueError) as e:
                print(f"warning: skipping {adir}: {e}", file=sys.stderr)
                continue
            if rec.get("status") != "completed":
                print(
                    f"{adir.parent.name}/{adir.name}  skipped: status {rec.get('status')}, not judged"
                )
                continue
            outcome = judge.judge_attempt(task.criteria, diff, log, judge_config)
            judge.apply(rec, outcome)
            records.update_record(adir, rec)
            records.write_text(adir / "judge.json", outcome.judge_json())
            done.append((adir, rec))
    return done
