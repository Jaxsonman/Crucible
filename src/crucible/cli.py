"""Crucible command line interface."""

import argparse
import difflib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from crucible import (
    __version__,
    aggregate,
    configs,
    judge,
    paths,
    pricing,
    prompt,
    runner,
    workspace,
)
from crucible.mine import calibrate as mine_calibrate
from crucible.mine import candidates as mine_candidates
from crucible.mine import config as mine_config
from crucible.mine import gh as mine_gh
from crucible.mine import leak as mine_leak
from crucible.mine import scaffold as mine_scaffold
from crucible.mine import verify as mine_verify
from crucible.records import load_records
from crucible.task import TaskError, load_task, scaffold_repo_task, scaffold_task

COMMANDS = {
    "init": "Create .crucible/ in the current git repo",
    "task": "Manage task documents",
    "run": "Run a task against a matrix of configurations",
    "judge": "Judge recorded attempts against acceptance criteria",
    "report": "Compare results and recommend a configuration",
    "configs": "List named configuration matrices",
    "mine": "Mine merged PRs into benchmark tasks",
}


def _not_implemented(args: argparse.Namespace) -> int:
    print("not implemented yet", file=sys.stderr)
    return 2


INIT_README = """# .crucible

Crucible benchmark data for this repo: model/effort comparisons on your own tasks.

- `tasks/`: task documents (`crucible task new <id>`).
- `results/`: run records (git-ignored by default).
- `configs/`: named model matrices; `pricing.yaml` here overrides the bundled prices.

Main commands: `crucible task new <id>`, `crucible run tasks/<id> --matrix default`,
`crucible report [task]`.

To share results, delete `results/` from `.crucible/.gitignore`.
"""


def _init(args: argparse.Namespace) -> int:
    try:
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("crucible init must run inside a git repo (git rev-parse failed)", file=sys.stderr)
        return 1
    home = Path(top) / paths.HOME_DIRNAME
    wanted = {
        "tasks/.gitkeep": "",
        "results/.gitkeep": "",
        "configs/.gitkeep": "",
        ".gitignore": "results/\nmine/\n",
        "README.md": INIT_README,
    }
    created = False
    for rel, content in wanted.items():
        f = home / rel
        if f.exists():
            continue
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content, encoding="utf-8")
        created = True
    print(f"{'created' if created else 'already initialised:'} {home}")
    return 0


def _task_validate(args: argparse.Namespace) -> int:
    try:
        task = load_task(_task_path(args.task_dir))
    except TaskError as e:
        print(e, file=sys.stderr)
        return 1
    print(f"ok: {task.id}")
    return 0


def _task_new(args: argparse.Namespace) -> int:
    try:
        if paths.home_kind() == "repo":
            task_dir, dirty = scaffold_repo_task(paths.tasks_dir(), args.id, args.ref or "HEAD")
            if dirty:
                print(
                    "warning: working tree has uncommitted changes; "
                    "uncommitted changes are not part of the task (it starts from the committed ref)",
                    file=sys.stderr,
                )
        elif args.ref:
            print("--ref only applies to tasks inside a repo's .crucible/ home", file=sys.stderr)
            return 1
        else:
            task_dir = scaffold_task(paths.tasks_dir(), args.id)
    except TaskError as e:
        print(e, file=sys.stderr)
        return 1
    print(f"created {task_dir / 'task.md'}")
    return 0


def _task_path(arg: str) -> Path:
    """The task directory for `arg`: the path itself, or a bare task id resolved against
    `<home>/tasks/` (also when a same-named directory in the cwd is not a task). A bare id
    that is neither raises TaskError listing the available tasks."""
    path = Path(arg)
    if "/" in arg or os.sep in arg or (path / "task.md").is_file():
        return path
    try:
        tasks = paths.tasks_dir()
    except paths.CrucibleHomeError:
        return path
    if (tasks / arg).is_dir():
        return tasks / arg
    if path.exists():
        return path  # not a task either: let load_task say what is wrong with it
    names = sorted(p.name for p in tasks.iterdir() if (p / "task.md").is_file()) \
        if tasks.is_dir() else []
    msg = f"no task named {arg} in {tasks} (available: "
    msg += (", ".join(names[:10]) + (", \u2026" if len(names) > 10 else "")) if names else "none"
    close = difflib.get_close_matches(arg, names, n=1)
    msg += ")" + (f"; did you mean {close[0]}?" if close else "")
    raise TaskError(msg)


def _run(args: argparse.Namespace) -> int:
    try:
        task = load_task(_task_path(args.task_dir))
        cfgs = (
            configs.parse_configs(args.configs) if args.configs else configs.load_named(args.matrix)
        )
        pricing.load_pricing()
    except (TaskError, configs.ConfigError, pricing.PricingError) as e:
        print(e, file=sys.stderr)
        return 1
    judge_cfg = None
    if args.judge_config and not args.judge:
        print("--judge-config requires --judge", file=sys.stderr)
        return 1
    if args.judge:
        try:
            judge_cfg = judge.parse_judge_config(args.judge_config)
        except configs.ConfigError as e:
            print(e, file=sys.stderr)
            return 1
    labels = [c.label for c in cfgs]
    dups = sorted({label for label in labels if labels.count(label) > 1})
    if dups:
        print(f"duplicate config(s): {', '.join(dups)}; each config may appear once", file=sys.stderr)
        return 1
    if args.repeat < 1:
        print("--repeat must be at least 1", file=sys.stderr)
        return 1
    if args.max_budget_usd <= 0:
        print("--max-budget-usd must be positive", file=sys.stderr)
        return 1
    if args.check_timeout is not None and args.check_timeout <= 0:
        print("--check-timeout must be positive", file=sys.stderr)
        return 1
    prompt_text = prompt.build_prompt(task)
    sha = prompt.prompt_sha256(prompt_text)
    attempts = runner.plan(cfgs, args.repeat)
    if args.dry_run:
        print(f"task {task.id}  prompt sha256 {sha}")
        for a in attempts:
            print(f"planned: {a.config.label} #{a.n}")
        return 0
    batch_id = runner.new_batch_id()
    print(f"batch {batch_id}: {len(attempts)} attempt(s)")
    for a in attempts:
        try:
            rec, interrupted = runner.run_attempt(
                task, batch_id, a, prompt_text, sha, args.max_budget_usd, args.keep_workspaces,
                args.check_timeout, judge_cfg,
            )
        except FileExistsError as e:
            print(e, file=sys.stderr)
            return 1
        print(runner.format_line(a, rec), flush=True)
        if interrupted:
            print("interrupted", file=sys.stderr)
            return 130
    return 0


def _judge(args: argparse.Namespace) -> int:
    try:
        task = load_task(paths.tasks_dir() / args.task_id)
        cfg = judge.parse_judge_config(args.judge_config)
    except (TaskError, configs.ConfigError) as e:
        print(e, file=sys.stderr)
        return 1
    root = paths.results_dir() / task.id
    if args.batch and not (root / args.batch).is_dir():
        print(f"no such batch {args.batch!r} for task {task.id}", file=sys.stderr)
        return 1
    try:
        done = runner.judge_existing(task, args.batch, cfg)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    for adir, rec in done:
        j = rec["judge"]
        verdict = {True: "pass", False: "fail", None: "no verdict"}[j["passed"]]
        print(f"{adir.parent.name}/{adir.name}  judge {verdict}  success={rec['success']}")
    print(f"judged {len(done)} attempt(s)")
    return 0


def _fmt_num(value, fmt: str) -> str:
    return "—" if value is None else format(value, fmt)


REPORT_COLUMNS = [
    ("Config", lambda r: r["config"]),
    ("Attempts", lambda r: str(r["attempts"])),
    ("Successes", lambda r: str(r["successes"])),
    ("Success rate", lambda r: _fmt_num(r["success_rate"], ".0%")),
    ("Unknown", lambda r: str(r["unknown"])),
    ("Median time (s)", lambda r: _fmt_num(r["median_wall_seconds"], ".1f")),
    ("Time range (s)", lambda r: "—" if r["min_wall_seconds"] is None
        else f"{r['min_wall_seconds']:.1f}–{r['max_wall_seconds']:.1f}"),
    ("Median tokens", lambda r: _fmt_num(r["median_tokens"], ",.0f")),
    ("Median cost", lambda r: "—" if r["median_cost"] is None else f"${r['median_cost']:.4f}"),
    ("Cost/success", lambda r: "—" if r["cost_per_success"] is None
        else f"${r['cost_per_success']:.4f}"),
    ("Unpriced", lambda r: str(r["unpriced"])),
]


KIND_COLUMNS = [REPORT_COLUMNS[0], ("Tasks", lambda r: str(r["tasks"])), *REPORT_COLUMNS[1:]]


def format_report_table(rows: list[dict], columns=REPORT_COLUMNS) -> str:
    lines = ["| " + " | ".join(name for name, _ in columns) + " |",
             "|" + "|".join("---" for _ in columns) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(fn(row) for _, fn in columns) + " |")
    return "\n".join(lines)


def _evidence(row: dict) -> str:
    known = row["attempts"] - row["unknown"]
    cost = "—" if row["median_cost"] is None else f"${row['median_cost']:.4f}"
    return (
        f"{row['tasks']} tasks, {row['attempts']} attempts, "
        f"success {_fmt_num(row['success_rate'], '.0%')} ({row['successes']}/{known} known), "
        f"median cost {cost}, median time {_fmt_num(row['median_wall_seconds'], '.1f')}s"
    )


def _format_recommendation(rec: dict) -> list[str]:
    lines = []
    if rec["status"] == "recommended":
        lines.append(f"Recommendation: {rec['config']}")
        lines.append(f"  Evidence: {_evidence(rec['evidence'])}")
        if rec["runner_up"]:
            lines.append(f"  Runner-up: {rec['runner_up']['config']}: {_evidence(rec['runner_up'])}")
        else:
            lines.append("  Runner-up: none")
        return lines
    lines.append(
        f"Recommendation: no config meets the bar (at least {rec['min_attempts']} attempts with "
        f"known results and success rate >= {rec['threshold']:.0%})"
    )
    best = rec["best_effort"]
    if best:
        lines.append(f"  Highest success rate: {best['config']}: {_evidence(best)}")
        if rec["runner_up"]:
            lines.append(f"  Next: {rec['runner_up']['config']}: {_evidence(rec['runner_up'])}")
    else:
        lines.append("  No config has a known success rate.")
    return lines


def _cross_task_report(args: argparse.Namespace) -> int:
    if args.batch:
        print("--batch requires a task id", file=sys.stderr)
        return 1
    if args.min_attempts < 1:
        print("--min-attempts must be at least 1", file=sys.stderr)
        return 1
    if not 0 <= args.threshold <= 1:
        print("--threshold must be between 0 and 1", file=sys.stderr)
        return 1
    records = load_records()
    if args.kind:
        records = [r for r in records if (r.get("task_kind") or aggregate.NO_KIND) == args.kind]
    if not records:
        what = f"kind {args.kind!r}" if args.kind else "any task"
        print(f"no results for {what}", file=sys.stderr)
        return 1
    try:
        reports = aggregate.kind_reports(records, args.min_attempts, args.threshold)
    except pricing.PricingError as e:
        print(e, file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps({"min_attempts": args.min_attempts, "threshold": args.threshold,
                          "kinds": reports}, indent=2))
        return 0
    for i, rep in enumerate(reports):
        if i:
            print()
        print(f"## Kind: {rep['kind']}  ({rep['tasks']} tasks, {rep['attempts']} attempts)")
        print()
        print("\n".join(_format_recommendation(rep["recommendation"])))
        print()
        print(format_report_table(rep["configs"], KIND_COLUMNS))
    return 0


def _report(args: argparse.Namespace) -> int:
    if not args.task_id:
        return _cross_task_report(args)
    if args.kind:
        print("--kind applies only to the cross-task report (omit the task id)", file=sys.stderr)
        return 1
    records = load_records(args.task_id)
    if not records:
        print(f"no results for task {args.task_id!r}", file=sys.stderr)
        return 1
    if args.batch:
        records = [r for r in records if r.get("batch_id") == args.batch]
        if not records:
            print(f"no such batch {args.batch!r} for task {args.task_id}", file=sys.stderr)
            return 1
    try:
        rows = aggregate.aggregate_configs(records)
    except pricing.PricingError as e:
        print(e, file=sys.stderr)
        return 1
    summary = aggregate.task_summary(args.task_id, records)
    warning = (
        f"WARNING: task changed between batches ({len(summary['prompt_shas'])} different "
        "prompt SHAs); results are not directly comparable."
        if summary["prompt_changed"] else None
    )
    if args.json:
        print(json.dumps({**summary, "configs": rows}, indent=2))
        if warning:
            print(warning, file=sys.stderr)
        return 0
    print(
        f"Task: {summary['task_id']}  kind: {summary['kind'] or '—'}  "
        f"batches: {summary['batches']}  prompt sha256: {', '.join(summary['prompt_shas']) or '—'}"
    )
    if warning:
        print(warning)
    print()
    print(format_report_table(rows))
    return 0


def _configs(args: argparse.Namespace) -> int:
    try:
        if not args.name:
            for name, is_local in configs.list_all_matrices():
                print(f"{name}  (local)" if is_local else name)
            return 0
        table = pricing.load_pricing()
        for cfg in configs.load_named(args.name):
            e = table[pricing.resolve_model(cfg.model)]
            print(
                f"{cfg.label}  {e.id}  "
                f"${e.input:.2f} in / ${e.output:.2f} out / "
                f"${e.cache_write:.2f} cache-write / ${e.cache_read:.2f} cache-read per MTok"
            )
        return 0
    except (configs.ConfigError, pricing.PricingError) as e:
        print(e, file=sys.stderr)
        return 1


def _positive_int(text: str) -> int:
    n = int(text)  # a ValueError becomes argparse's "invalid _positive_int value"
    if n < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {n}")
    return n


def _mine_setup(args: argparse.Namespace):
    """(repo top, config path, MineConfig), or an exit code after printing the problem. Writes
    the default mine.yaml on first use (returning 0)."""
    try:
        home = paths.home()
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except paths.CrucibleHomeError as e:
        print(e, file=sys.stderr)
        return 1
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("crucible mine must run inside a git repo (git rev-parse failed)", file=sys.stderr)
        return 1
    if paths.home_kind() == "repo":
        mine_config.ensure_gitignored(home)
    cfg_path = home / mine_config.CONFIG_NAME
    if not cfg_path.exists():
        suites = mine_config.write_default(cfg_path, Path(top))
        if suites:
            found = ", ".join(s.name for s in suites)
            print(f"detected {len(suites)} suite(s): {found}")
        else:
            print("no test stack detected; wrote an empty template")
        print(f"wrote {cfg_path}\nreview it, then run `crucible mine` again")
        return 0
    try:
        return Path(top), cfg_path, mine_config.load_config(cfg_path)
    except mine_config.MineConfigError as e:
        print(f"invalid {cfg_path}:", file=sys.stderr)
        for err in e.errors:
            print(f"  {err}", file=sys.stderr)
        return 1


def _mine_collect(args, top: Path, cfg):
    """(kept, excluded) from `gh` merged PRs, or from local git with `--local` or when `gh`
    can't be used (one line on stderr says why). Raises MineGitError."""
    max_lines = args.max_lines if args.max_lines is not None else cfg.max_changed_lines
    mined = mine_candidates.mined_tasks(paths.tasks_dir())
    if not args.local:
        try:
            found, pre, slug = mine_gh.pr_candidates(top, cfg, args.limit, args.branch)
            kept, excluded = mine_candidates.filter_candidates(found, cfg, max_lines, mined)
            kept = mine_gh.with_issue_details(kept, slug)
            return kept, excluded + pre
        except mine_gh.GhUnavailable as e:
            print(f"gh source unavailable ({e}); using local git history", file=sys.stderr)
    found = mine_candidates.local_candidates(top, cfg, args.branch or "HEAD", args.limit)
    return mine_candidates.filter_candidates(found, cfg, max_lines, mined)


def _mine(args: argparse.Namespace) -> int:
    setup = _mine_setup(args)
    if isinstance(setup, int):
        return setup
    top, cfg_path, cfg = setup
    if args.verify is not None and not args.verify:
        print("--verify needs at least one candidate id", file=sys.stderr)
        return 2
    note = mine_calibrate.note(cfg.suites)
    if note:
        print(note, file=sys.stderr)
    try:
        kept, excluded = _mine_collect(args, top, cfg)
        if args.verify:
            return _mine_verify(args, top, cfg, kept, excluded)
    except mine_candidates.MineGitError as e:
        print(e, file=sys.stderr)
        return 1
    labels = [mine_verify.verified_label(c, cfg.suites) for c in kept]
    _limit_hint(args, kept, excluded)
    if args.json:
        print(json.dumps({
            "candidates": [{**c.to_dict(), "verified": v} for c, v in zip(kept, labels)],
            "excluded": [e.to_dict() for e in excluded] if args.show_excluded else [],
        }, indent=2))
        return 0
    print(f"{cfg_path}: ok ({len(cfg.suites)} suite(s): {', '.join(s.name for s in cfg.suites)})")
    _print_candidates(kept, verified=[v or "" for v in labels])
    if args.show_excluded and excluded:
        print("\nexcluded:")
        _print_candidates([e.candidate for e in excluded], [e.reason for e in excluded])
    print(f"\n{len(kept)} candidate(s), {len(excluded)} excluded")
    return 0


def _limit_hint(args, kept, excluded) -> None:
    """Few candidates among many exclusions after a walk that hit `--limit`: older history was
    not scanned."""
    scanned = len(kept) + len(excluded)
    if scanned >= args.limit and len(kept) < 5 and len(excluded) > 2 * len(kept):
        print(f"hint: {len(kept)} candidate(s) among {scanned} scanned ({len(excluded)} "
              f"excluded); raise --limit (now {args.limit}) to scan further back",
              file=sys.stderr)


def _print_calibration(report) -> None:
    rows = [["suite", "status", "setup", "samples", "detail"]]
    for s in report.suites:
        setup = ("none" if not s.setup.log and s.setup.ok
                 else f"{'ok' if s.setup.ok else 'FAILED'} {s.setup.seconds:.1f}s")
        done = sum(x.outcome == "verified" for x in s.samples)
        rows.append([s.name, s.status, setup, f"{done}/{len(s.samples)} verified", s.detail])
    widths = [max(len(r[j]) for r in rows) for j in range(len(rows[0]))]
    for r in rows:
        print("  ".join(cell.ljust(w) for cell, w in zip(r, widths)).rstrip())
    for s in report.suites:
        if s.status == "ok":
            continue
        print(f"\n{s.name}: {s.status}")
        if not s.setup.ok:
            print(f"  setup log: {s.setup.log}")
        for x in s.samples:
            if x.outcome != "verified":
                log = list(x.logs.values())[-1] if x.logs else "-"
                print(f"  {x.id}: {x.reason} (log {log})")
        for h in s.hints:
            print(f"  hint [{h.id}]: {h.hint}")
        if s.status == "no-candidates":
            print("  warning: nothing to sample; the suite is untested")


def _mine_calibrate(args: argparse.Namespace) -> int:
    setup = _mine_setup(args)
    if isinstance(setup, int):
        return setup
    top, _, cfg = setup
    if not cfg.suites:
        print("no suites configured in mine.yaml; nothing to calibrate", file=sys.stderr)
        return 1
    unknown = [n for n in args.suite or [] if n not in {s.name for s in cfg.suites}]
    if unknown:
        print(f"unknown suite(s): {', '.join(unknown)} (configured: "
              f"{', '.join(s.name for s in cfg.suites)})", file=sys.stderr)
        return 2
    try:
        kept, _ = _mine_collect(args, top, cfg)
        report = mine_calibrate.calibrate(
            top, cfg.suites, kept, names=args.suite, n=args.candidates, timeout=args.timeout,
            force=args.force)
    except (mine_candidates.MineGitError, workspace.WorkspaceError,
            mine_verify.ScriptsLinkError) as e:
        print(e, file=sys.stderr)
        return 1
    mine_calibrate.save(report, cfg.suites)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        _print_calibration(report)
    return 0 if report.ok else 1


def _mine_verify(args, top: Path, cfg, kept, excluded) -> int:
    """Verify the requested candidates. Exit 1 if any id was skipped (unknown or excluded);
    a rejection is a normal outcome and exits 0."""
    by_id = {c.id: c for c in kept}
    why = {e.candidate.id: e.reason for e in excluded}
    results, skipped = [], False
    wanted = list(dict.fromkeys(args.verify))
    for i, cid in enumerate(wanted, 1):
        c = by_id.get(cid)
        if c is None:
            reason = f"excluded: {why[cid]}" if cid in why else "not in the candidate list"
            print(f"{cid}: skipped ({reason})", file=sys.stderr)
            skipped = True
            continue
        started = time.monotonic()
        try:
            r = mine_verify.verify_candidate(
                top, c, mine_verify.involved_suites(c, cfg.suites), timeout=args.timeout, keep=args.keep, force=args.force)
        except mine_verify.ScriptsLinkError as e:
            print(e, file=sys.stderr)
            return 1
        except (mine_candidates.MineGitError, workspace.WorkspaceError) as e:
            print(f"{cid}: skipped ({e})", file=sys.stderr)
            skipped = True
            continue
        results.append(r)
        took = "" if r.cached else f" in {time.monotonic() - started:.0f}s"
        label = "verified" if r.verified else f"rejected: {r.reason}"
        print(f"[{i}/{len(wanted)}] {cid}: {label}{' (cached)' if r.cached else took}",
              file=sys.stderr, flush=True)
        log, hints = mine_verify.rejection_diagnosis(r)
        r.hints = [h.to_dict() for h in hints]
        if not args.json:
            detail = "verified" if r.verified else f"rejected: {r.reason}"
            print(f"{cid}: {detail}" + (" (cached)" if r.cached else ""))
            if log:
                print(f"  log: {log}")
            for h in hints:
                print(f"  hint [{h.id}]: {h.hint}")
            for w in r.warnings:
                print(f"  warning: {w}")
            for d in r.kept:
                print(f"  kept {d}")
    if args.json:
        print(json.dumps({"verify": [r.to_dict() for r in results]}, indent=2))
    return 1 if skipped else 0


def _mine_show(args: argparse.Namespace) -> int:
    setup = _mine_setup(args)
    if isinstance(setup, int):
        return setup
    top, _, cfg = setup
    try:
        kept, excluded = _mine_collect(args, top, cfg)
        match = next((c for c in kept if c.id == args.id), None)
        reason = None
        if match is None:
            ex = next((e for e in excluded if e.candidate.id == args.id), None)
            if ex is None:
                print(f"no candidate with id {args.id!r} (try `crucible mine --show-excluded`)",
                      file=sys.stderr)
                return 1
            match, reason = ex.candidate, ex.reason
        out = match.to_dict()
        out["excluded"] = reason
        out["test_diff"] = mine_candidates.diff_text(
            top, match.base, match.merge, [f.path for f in match.test_files])
        out["non_test_diff"] = mine_candidates.diff_text(
            top, match.base, match.merge, [f.path for f in match.other_files])
    except mine_candidates.MineGitError as e:
        print(e, file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(out, indent=2))
        return 0
    print(f"{out['id']}  {out['title']}\nsource: {out['source']}  kind: {out['suggested_kind']}"
          f"  suite: {'+'.join(out['suites']) or '-'}" + (f"\nexcluded: {reason}" if reason else ""))
    for issue in out["issues"]:
        print(f"issue #{issue.get('number')}: {issue.get('title', '')}")
    print(f"\n--- test diff ---\n{out['test_diff']}\n--- non-test diff ---\n{out['non_test_diff']}")
    return 0


def _mine_scaffold(args: argparse.Namespace) -> int:
    setup = _mine_setup(args)
    if isinstance(setup, int):
        return setup
    top, _, cfg = setup
    try:
        kept, excluded = _mine_collect(args, top, cfg)
        c = next((c for c in kept if c.id == args.id), None)
        if c is None:
            ex = next((e for e in excluded if e.candidate.id == args.id), None)
            why = f"excluded: {ex.reason}" if ex else "not in the candidate list"
            print(f"cannot scaffold {args.id}: {why}", file=sys.stderr)
            return 1
        suites = mine_verify.involved_suites(c, cfg.suites)
        r = mine_verify.cached_result(c, suites)
        if r is None:
            if mine_verify.has_stale_cache(c):
                print(f"cannot scaffold {args.id}: config changed since verification; "
                      f"re-run --verify {c.id} (`crucible mine --verify {c.id}`)", file=sys.stderr)
            else:
                print(f"cannot scaffold {args.id}: not verified; run "
                      f"`crucible mine --verify {c.id}`", file=sys.stderr)
            return 1
        if not r.verified:
            print(f"cannot scaffold {args.id}: rejected by verification ({r.reason})",
                  file=sys.stderr)
            return 1
        task_dir = mine_scaffold.scaffold_task_dir(
            top, c, suites, paths.tasks_dir(), slug=args.task_id, kind=args.kind)
    except (mine_candidates.MineGitError, mine_scaffold.ScaffoldError) as e:
        print(e, file=sys.stderr)
        return 1
    print(f"created {task_dir / 'task.md'}")
    needs = mine_scaffold.recorded_requires(task_dir)
    if needs:
        print(f"warning: this task requires {', '.join(needs)} on the machine that runs it "
              "(recorded as `requires` in .mine.json)", file=sys.stderr)
    print("fill in the What / Why / Acceptance criteria sections, then run "
          f"`crucible mine prove {task_dir}`")
    return 0


def _leak_flags(task) -> list:
    """Leak flags for a loaded mined task (config from the Crucible home's mine.yaml)."""
    cfg_path = paths.home() / mine_config.CONFIG_NAME
    try:
        cfg = mine_config.load_config(cfg_path)
    except mine_config.MineConfigError as e:
        raise mine_scaffold.ScaffoldError(f"invalid {cfg_path}: " + "; ".join(e.errors)) from e
    return mine_leak.check_task(task, cfg)


def _print_flags(flags) -> None:
    for f in flags:
        print(f"  leak [{f.kind}] in {f.section}: {f.span} ({f.reason})", file=sys.stderr)


def _mine_leakcheck(args: argparse.Namespace) -> int:
    try:
        task = load_task(_task_path(args.task_dir))
        flags = _leak_flags(task)
    except (TaskError, mine_scaffold.ScaffoldError, mine_candidates.MineGitError,
            paths.CrucibleHomeError, workspace.WorkspaceError) as e:
        print(e, file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps([f.to_dict() for f in flags], indent=2))
    elif not flags:
        print(f"clean: {task.id}")
    else:
        print(f"leaks: {task.id}: {len(flags)} flag(s)", file=sys.stderr)
        _print_flags(flags)
    return 1 if flags else 0


def _mine_prove(args: argparse.Namespace) -> int:
    try:
        task = load_task(_task_path(args.task_dir))
        result = mine_scaffold.prove_task(task, args.timeout)
        flags = _leak_flags(task) if result.ok else []
    except (TaskError, mine_scaffold.ScaffoldError, mine_candidates.MineGitError,
            paths.CrucibleHomeError, workspace.WorkspaceError) as e:
        print(e, file=sys.stderr)
        return 1
    if result.ok and flags:
        _print_flags(flags)
        if not args.allow_leaks:
            print(f"not proven: {result.task_id}: task text leaks the solution "
                  f"({len(flags)} flag(s)); rewrite it or pass --allow-leaks", file=sys.stderr)
            return 1
    if result.ok:
        print(f"proven: {result.task_id}")
        return 0
    print(f"not proven: {result.task_id}: {result.failure}", file=sys.stderr)
    if result.log:
        print(result.tail(), file=sys.stderr)
    return 1


def _print_candidates(cands, reasons: list[str] | None = None,
                      verified: list[str] | None = None) -> None:
    rows = [["id", "title", "kind", "suite", "tests/other", "lines", "base->merge"]
            + (["verified"] if verified is not None else [])
            + (["reason"] if reasons else [])]
    for i, c in enumerate(cands):
        title = c.title if len(c.title) <= 40 else c.title[:39] + "\u2026"
        row = [c.id, title, c.suggested_kind, "+".join(c.suites) or "-",
               f"{len(c.test_files)}/{len(c.other_files)}", str(c.lines_changed),
               f"{c.base[:7] or '-'}->{c.merge[:7]}"]
        rows.append(row + ([verified[i]] if verified is not None else [])
                    + ([reasons[i]] if reasons else []))
    widths = [max(len(r[j]) for r in rows) for j in range(len(rows[0]))]
    for r in rows:
        print("  ".join(cell.ljust(w) for cell, w in zip(r, widths)).rstrip())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crucible", description="Benchmark Claude model/effort configurations."
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", metavar="{" + ",".join(COMMANDS) + "}")
    for name, help_text in COMMANDS.items():
        p = sub.add_parser(name, help=help_text, description=help_text)
        p.set_defaults(func=_not_implemented)
        if name == "init":
            p.set_defaults(func=_init)
        if name == "task":
            p.set_defaults(func=lambda a, p=p: (p.print_help(sys.stderr), 2)[1])
            task_sub = p.add_subparsers(dest="task_command", metavar="{validate,new}")
            v = task_sub.add_parser("validate", help="Validate a task directory")
            v.add_argument("task_dir")
            v.set_defaults(func=_task_validate)
            n = task_sub.add_parser("new", help="Scaffold tasks/<id>/ from a template")
            n.add_argument("id")
            n.add_argument("--ref", help="git revision the task starts from (default HEAD; repo homes only)")
            n.set_defaults(func=_task_new)
        if name == "run":
            p.add_argument("task_dir")
            group = p.add_mutually_exclusive_group(required=True)
            group.add_argument("--configs", help="comma-separated model:effort list")
            group.add_argument("--matrix", help="named matrix (configs/<name>.yaml)")
            p.add_argument("--repeat", type=int, default=1, help="attempts per config")
            p.add_argument("--max-budget-usd", type=float, default=5.0)
            p.add_argument("--check-timeout", type=float, default=None,
                           help="seconds before a hanging check is killed (default: the task's "
                                "check_timeout_minutes, else 300)")
            p.add_argument("--judge", action="store_true",
                           help="judge each attempt against the acceptance criteria")
            p.add_argument("--judge-config", help=f"judge model:effort (default {judge.DEFAULT_CONFIG})")
            p.add_argument("--keep-workspaces", action="store_true")
            p.add_argument("--dry-run", action="store_true")
            p.set_defaults(func=_run)
        if name == "judge":
            p.add_argument("task_id")
            p.add_argument("--batch", help="only judge attempts in this batch")
            p.add_argument("--judge-config", help=f"judge model:effort (default {judge.DEFAULT_CONFIG})")
            p.set_defaults(func=_judge)
        if name == "report":
            p.add_argument("task_id", nargs="?", help="task id (omit for the cross-task report)")
            p.add_argument("--batch", help="only include attempts from this batch")
            p.add_argument("--json", action="store_true", help="print aggregates as JSON")
            p.add_argument("--kind", help="cross-task report: only this task kind")
            p.add_argument("--min-attempts", type=int, default=aggregate.DEFAULT_MIN_ATTEMPTS,
                           help="cross-task report: attempts with known success a config needs "
                                "to be recommended (default 3)")
            p.add_argument("--threshold", type=float, default=aggregate.DEFAULT_THRESHOLD,
                           help="cross-task report: minimum success rate to be recommended "
                                "(default 0.8)")
            p.set_defaults(func=_report)
        if name == "mine":
            def common(q, hidden=False):
                # on a subcommand, SUPPRESS keeps values given before it (`mine --local show 1`)
                def dflt(value):
                    return argparse.SUPPRESS if hidden else value
                q.add_argument("--branch", default=dflt(None),
                               help="local: history to walk (default HEAD); gh: only PRs "
                                    "merged into this base branch (default all)")
                q.add_argument("--limit", type=int, default=dflt(mine_candidates.DEFAULT_LIMIT),
                               help="max PRs (gh) or commits (local) scanned, newest first "
                                    "(default 300); raise it when few candidates are found")
                q.add_argument("--max-lines", type=_positive_int, default=dflt(None),
                               help="override max_changed_lines from mine.yaml")
                q.add_argument("--local", action="store_true", default=dflt(False),
                               help="skip gh; list candidates from local git history")
                q.add_argument("--json", action="store_true", default=dflt(False),
                               help="print as JSON")
            common(p)
            p.add_argument("--show-excluded", action="store_true",
                           help="also list excluded commits with reasons")
            p.add_argument("--verify", nargs="*", metavar="ID", default=None,
                           help="prove each candidate's tests fail at base and pass at merge")
            p.add_argument("--force", action="store_true",
                           help="with --verify: ignore cached results and run again")
            p.add_argument("--keep", action="store_true",
                           help="with --verify: keep the workspace dirs and print their paths")
            p.add_argument("--timeout", type=float, default=None,
                           help="with --verify: seconds allowed for each test step, overriding "
                                "the suites' `timeout` (default: per suite, 600)")
            p.set_defaults(func=_mine)
            mine_sub = p.add_subparsers(
                dest="mine_command", metavar="{calibrate,show,scaffold,prove,leakcheck}")
            ca = mine_sub.add_parser(
                "calibrate", help="Prove each suite's config runs the repo's tests; diagnose it")
            ca.add_argument("--candidates", type=_positive_int,
                            default=mine_calibrate.DEFAULT_CANDIDATES, metavar="N",
                            help="candidates to verify per suite (default 3)")
            ca.add_argument("--suite", action="append", metavar="NAME",
                            help="only this suite (repeatable; default all)")
            ca.add_argument("--timeout", type=float, default=argparse.SUPPRESS,
                            help="seconds allowed for each test step, overriding the suites' "
                                 "`timeout` (setup keeps `setup_timeout`)")
            ca.add_argument("--force", action="store_true", default=argparse.SUPPRESS,
                            help="ignore cached verification results and run again")
            common(ca, hidden=True)
            ca.set_defaults(func=_mine_calibrate)
            sh = mine_sub.add_parser("show", help="One candidate with its test and non-test diffs")
            sh.add_argument("id", help="candidate id (PR number or short SHA)")
            common(sh, hidden=True)
            sh.set_defaults(func=_mine_show)
            sc = mine_sub.add_parser(
                "scaffold", help="Write .crucible/tasks/<slug>/ for a verified candidate")
            sc.add_argument("id", help="candidate id (PR number or short SHA)")
            sc.add_argument("--task-id", help="task id / directory name (default pr-<n>-<title>)")
            sc.add_argument("--kind", help="task kind (default: the suggested kind)")
            common(sc, hidden=True)
            sc.set_defaults(func=_mine_scaffold)
            pr = mine_sub.add_parser(
                "prove", help="Prove a finished mined task's check: fails at ref, passes at merge")
            pr.add_argument("task_dir")
            pr.add_argument("--timeout", type=float, default=None,
                            help="seconds allowed per check run (default: recorded at scaffold "
                                 "time as the suites' setup + test timeouts, else 600)")
            pr.add_argument("--allow-leaks", action="store_true",
                            help="do not fail when the task text leaks the solution")
            pr.set_defaults(func=_mine_prove)
            lk = mine_sub.add_parser(
                "leakcheck", help="Flag task text that gives away the fix")
            lk.add_argument("task_dir")
            lk.add_argument("--json", action="store_true", help="print flags as JSON")
            lk.set_defaults(func=_mine_leakcheck)
        if name == "configs":
            p.add_argument("name", nargs="?", help="matrix name (configs/<name>.yaml)")
            p.set_defaults(func=_configs)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help(sys.stderr)
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
