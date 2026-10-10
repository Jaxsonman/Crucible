# Benchmark validity fixes: spec

Date: 2026-10-08. Branch `fix/benchmark-validity`. Source: methodology review of the benchmark
engine (see the review summary at the end).

## Goal

Make `crucible report` output trustworthy once real task sets exist: fix the criteria parsing
bug, make the success signal honest, show uncertainty, compare like with like, and stop
structurally penalising expensive configurations.

## Global constraints

- Python 3.12+, `uv`, runtime dependency `pyyaml` only. No new runtime dependencies.
- Test-first. Tests use the autouse `crucible_home` fixture in `tests/conftest.py` and never
  write into the real `tasks/`, `results/` or `.crucible/`. Tests never call the real `claude` or
  `gh` (use `tests/fake_claude.py` and `tests/fake_gh.py`).
- `uv run pytest` and `uv run ruff check .` pass after every task.
- `schema_version` stays 1. New record fields are additive and optional; existing records under
  `results/` must still load and report.
- Nothing under `results/` is edited by hand.
- `docs/design.md` is updated in the same task as the decision it records.
- Every task ends in one commit with a conventional message (`fix:`, `feat:`, `docs:`) ending
  with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

## Decisions

### D1. Wrapped acceptance criteria are one criterion (bug fix)

`task.py` currently keeps only lines matching `BULLET_RE` in the `## Acceptance criteria`
section. A bullet whose prose wraps onto following lines loses everything after the first line,
in both the agent prompt and the judge prompt. The committed `tasks/todo-list/task.md` is
affected (its recorded prompt SHA matches the truncated prompt).

Rule: inside the criteria section, a non-blank line that does not start a bullet continues the
preceding bullet (Markdown lazy continuation). Continuation lines are joined to the bullet with a
single space after stripping leading whitespace. A nested bullet (`  - sub`) is also a
continuation and is joined the same way, keeping its own `- ` marker text verbatim. A blank line
ends a bullet. A non-blank, non-bullet line that appears before the first bullet is a validation
error: `section '## Acceptance criteria' has text outside bullets`.

Consequence: the prompt SHA of existing wrapped tasks changes. That is correct; the report's
`prompt_changed` warning already covers it. Do not rerun any attempts.

### D2. Success is the check's verdict when a check ran; the judge is reported beside it

`records.compute_success` currently ANDs check and judge. New rule, in this order:

1. `status` is not `completed` → `false`.
2. `check.ran` and `check.passed` is not null → `success = check.passed`.
3. Else `judge.ran` and `judge.passed` is not null → `success = judge.passed`.
4. Else `null`.

Reports recompute `success` from the record with `compute_success` instead of reading the
stored `success` field, the same way they re-price from tokens, so old records follow the new
rule without being edited. `judge.apply` and `build_record` keep writing `success` for
convenience.

Aggregation gains, per config row: `judge_disagreements` = attempts where the check produced a
verdict, the judge produced a verdict, and they differ. The report table gets a column
`Judge≠check`. The judge's own pass/fail is still in `judge.json` and `record.json`.

`skills/run/SKILL.md` step 1 changes from "Add `--judge` unless the user said not to judge" to:
add `--judge` only when the task has no `check:` or the user asks for it; say in the preview
that the judge is advisory when a check exists.

### D3. Confidence intervals on success rates

`aggregate.summarize_config` adds `success_ci` = the 95% Wilson score interval `(low, high)` of
`successes / known` (null when `known` is 0). Formula with z = 1.96, p = k/n:

```
centre = (p + z²/2n) / (1 + z²/n)
half   = z * sqrt(p(1-p)/n + z²/4n²) / (1 + z²/n)
low, high = max(0, centre - half), min(1, centre + half)
```

Report tables show the interval in the `Success rate` cell as `100% (44–100%)`. The
recommendation evidence line includes it. Pure stdlib (`math.sqrt`).

### D4. Rank qualifiers by cost per success

`aggregate._cheapest_key` sorts by `cost_per_success` (total priced cost ÷ priced successes),
then median wall time, then config label; unknown values sort last. `median_cost` stays in the
table. A qualifier needs a non-null `cost_per_success` (replaces the `median_cost is not None`
condition). Docstring and `docs/design.md` recommendation text say why: the decision is "what
does a passing result cost", and failed attempts are part of that cost.

### D5. Kind recommendations compare configs on the same tasks

`aggregate.kind_reports` returns, per kind, in addition to the pooled `configs` rows:

- `common_tasks`: sorted task ids attempted by every config that has at least one attempt in
  that kind.
- `comparable`: rows from `aggregate_configs` over only the records whose `task_id` is in
  `common_tasks` (each row carries `tasks`).
- `recommendation`: computed from `comparable`, not from the pooled rows. When `common_tasks` is
  empty the recommendation is `{"status": "no_common_tasks", "config": None, ...}` and the
  report says `Recommendation: none; no task was attempted by every config`.

The CLI kind report prints the pooled table as today, then
`Compared on N of M tasks attempted by every config` before the recommendation lines.

### D6. Warn when one config label pools several resolved models

Rows gain `resolved_models`: sorted distinct non-null `resolved_model` values of the group. The
report prints, after the table, one warning per row with more than one:
`warning: sonnet:low pools 2 models: claude-sonnet-5-5, claude-sonnet-5-6`. Grouping stays by
`model:effort` label (errored attempts have no resolved model and must not form their own group).

### D7. Stopped attempts are visible, and the budget cap can be set per task

Rows gain `stopped` = attempts with `status == "timeout"` or `subtype` in
`{"error_max_turns", "error_max_budget_usd"}`. New table column `Stopped` after `Unknown`.
Stopped attempts still count as failures in the rate (the config did not deliver), but the
reader can now see why.

Task front matter gains optional `max_budget_usd` (positive number). `crucible run` uses it when
the `--max-budget-usd` flag was not given on the command line; an explicit flag always wins.
The CLI default stays 5.0. `crucible task new` templates include a commented example.

### D8. Mined checks can run a regression suite

`mine.yaml` suites gain optional `regression_command` (a plain command string, no
placeholders; placeholders are a validation error). When set, the generated `check.sh` runs it
after the suite's PR tests, in the same `cwd` and `env`, and the check fails if it exits
non-zero (exit code collected like a test failure). `detect_suites` suggests one per stack:
pytest `python -m pytest -q`, Django `python manage.py test`, Vitest `npx vitest run`, Jest
`npx jest`. The written default `mine.yaml` includes it commented out, with a comment that it
runs the whole suite at check time and that `prove` rejects a task whose regression fails at
the merge commit (so pre-existing failures must be excluded or the line removed).

`prove` needs no code change: it runs the task's own `check.sh` at merge, so a failing
regression surfaces as `fails at merge`.

### D9. Mined tasks record their size

`scaffold` writes `source_lines: <counted_lines>` to the task front matter. `task.py` accepts an
optional positive integer `source_lines` (`Task.source_lines`). `build_record` stores it as
`task_source_lines` (null when absent). Reporting by size is a follow-up; this task only makes
the data exist.

### D10. Document the isolation versus fidelity trade-off

`docs/design.md` gets a short "Isolation versus fidelity" paragraph under the executor section:
`--safe-mode` drops CLAUDE.md, skills, hooks, memory and MCP, so attempts are reproducible but
run without the project's instructions; that is deliberate and a flag to include the project
CLAUDE.md at `ref` is an open follow-up. No code change.

## Out of scope

- Rerunning any benchmark (spends usage).
- Grouping rows by resolved model, size-bucketed reports, judge calibration data, significance
  tests.
- Changing `min_attempts` (3) or `threshold` (0.8) defaults.

## Review summary (source of these decisions)

- Bug: wrapped criteria truncated in prompt and judge.
- Data so far: 4 attempts on 2 toy tasks, every config passes, Opus never run.
- Validity gaps: 3/3 rule with no interval; pooled kind reports across different task sets;
  ranking ignores cost per success; alias grouping across model versions; budget and turn stops
  hidden as plain failures; mined checks have no regression step; judge on by default,
  uncalibrated, ANDed into success; safe mode drops CLAUDE.md; no size signal.
