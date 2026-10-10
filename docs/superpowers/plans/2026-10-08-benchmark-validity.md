# Benchmark Validity Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `crucible report` output trustworthy: whole acceptance criteria, an honest success signal, confidence intervals, like-for-like kind comparisons, cost-per-success ranking, visible stops and pooled models, mined regression checks and task size data.

**Architecture:** Most changes live in the pure aggregation layer (`src/crucible/aggregate.py`), which recomputes success from each record (`records.compute_success`) the same way it already re-prices cost from tokens, so no stored result is edited. The CLI (`src/crucible/cli.py`) only formats new row fields. Task parsing (`task.py`), mining (`mine/config.py`, `mine/scaffold.py`) and the run path (`cli._run`, `runner.build_record`) get small additive changes; every new record or front-matter field is optional.

**Tech Stack:** Python 3.12+, `uv`, stdlib `argparse`/`math`/`statistics`, `pyyaml`, `pytest`, `ruff`.

**Spec:** `docs/superpowers/specs/2026-10-08-benchmark-validity.md`

## Global Constraints

- Python 3.12+, `uv`, runtime dependency `pyyaml` only. No new runtime dependencies.
- Test-first. Tests use the autouse `crucible_home` fixture in `tests/conftest.py` and never write into the real `tasks/`, `results/` or `.crucible/`. Tests never call the real `claude` or `gh` (use `tests/fake_claude.py` and `tests/fake_gh.py`).
- `uv run pytest` and `uv run ruff check .` pass after every task.
- `schema_version` stays 1. New record fields are additive and optional; existing records under `results/` must still load and report.
- Nothing under `results/` is edited by hand.
- `docs/design.md` is updated in the same task as the decision it records.
- Every task ends in one commit with a conventional message (`fix:`, `feat:`, `docs:`) ending with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- Ruff line length is 100 (`pyproject.toml`); keep new lines within it.
- Do not rerun any benchmark (spends usage). Do not change `min_attempts` (3) or `threshold` (0.8) defaults.

## Review Focus

1. **Criteria section with only continuation text** (indented prose, no bullet at all) must fail validation with `has no bullet criteria`, never load as an empty or garbage criterion list; prose before the first bullet must fail with `has text outside bullets`. Pinned in Task 1 (`test_criteria_only_continuation_text_has_no_bullets`, `test_text_before_first_bullet_is_error`).
2. **Record with `check.ran` true but `check.passed` null** (a check that produced no verdict) must fall through to the judge's verdict, be unknown without one, and never count as a judge disagreement. Pinned in Task 2 (`test_check_ran_without_verdict_falls_back_to_judge`, `test_judge_disagrees_needs_two_verdicts`).
3. **n = 1 Wilson interval** must render `100% (21–100%)` / `0% (0–79%)` (clamped to [0, 1], no crash, no zero-width interval), and n = 0 must give `success_ci` null and a `—` cell. Pinned in Task 3 (`test_wilson_interval`, `test_single_attempt_shows_wide_interval`).
4. **A kind where every config ran exactly one disjoint task** must produce `common_tasks: []`, `comparable: []`, recommendation status `no_common_tasks` with `config: null`, print `Recommendation: none; no task was attempted by every config`, and still print the pooled table. Pinned in Task 7 (`test_kind_with_disjoint_tasks_has_no_common_tasks`, `test_disjoint_tasks_give_no_recommendation`).
5. **A `regression_command` containing `{tests}` or `{modules}`** must be a `mine.yaml` validation error naming `suites[i].regression_command`, never silently filled or passed to the shell with literal braces. Pinned in Task 8 (`test_regression_command_rejects_placeholders`).

---

## File map

| File | Responsibility | Tasks |
|---|---|---|
| `src/crucible/task.py` | criteria parsing, `max_budget_usd`, `source_lines` front matter, templates | 1, 6, 9 |
| `src/crucible/records.py` | `compute_success` rule, `judge_disagrees` | 2 |
| `src/crucible/aggregate.py` | row fields, Wilson CI, ranking, common tasks, stopped | 2, 3, 4, 5, 6, 7 |
| `src/crucible/cli.py` | report columns/lines, dry-run lines, budget resolution | 2, 3, 4, 5, 6, 7 |
| `src/crucible/runner.py` | `task_source_lines` record field | 9 |
| `src/crucible/mine/config.py` | `Suite.regression_command`, validation, suggestions, default yaml | 8 |
| `src/crucible/mine/scaffold.py` | regression lines in `check.sh`, `source_lines` front matter | 8, 9 |
| `skills/run/SKILL.md` | when to add `--judge`, budget wording | 2, 6 |
| `skills/report/SKILL.md`, `README.md` | user-facing wording | 10 |
| `docs/design.md` | every decision | 1–10 |

Line numbers below are as of the base commit; earlier tasks shift them. Locate edits by the quoted code, not by number.

---

### Task 1: D1 — wrapped acceptance criteria are one criterion

**Files:**
- Modify: `src/crucible/task.py:166-169` (criteria extraction in `load_task`), add helper `_criteria` after `_positive_int` (around line 96)
- Modify: `docs/design.md:67-69` (Task document section)
- Test: `tests/test_task.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `task._criteria(text: str) -> tuple[list[str], bool]` (criteria, stray). `Task.criteria` now holds whole wrapped bullets. `prompt.build_prompt` and `judge.build_prompt` are unchanged and pick this up automatically.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_task.py`:

```python
def criteria_body(criteria_text: str) -> str:
    return f"\n## What\n\nDo it.\n\n## Why\n\nBecause.\n\n## Acceptance criteria\n\n{criteria_text}"


def test_wrapped_bullet_is_one_criterion(crucible_home):
    body = criteria_body(
        "- `add` trims the title. An empty title\n"
        "  raises `ValueError`.\n"
        "- Second bullet\n"
        "continues lazily here.\n"
    )
    t = load_task(make_task(crucible_home, body=body))
    assert t.criteria == [
        "`add` trims the title. An empty title raises `ValueError`.",
        "Second bullet continues lazily here.",
    ]


def test_nested_bullet_is_a_continuation(crucible_home):
    body = criteria_body("- Parent text\n  - sub one\n  * sub two\n- Next\n")
    t = load_task(make_task(crucible_home, body=body))
    assert t.criteria == ["Parent text - sub one * sub two", "Next"]


def test_blank_line_ends_a_bullet(crucible_home):
    body = criteria_body("- First\n\n- Second\n  wrapped\n")
    assert load_task(make_task(crucible_home, body=body)).criteria == ["First", "Second wrapped"]


def test_text_before_first_bullet_is_error(crucible_home):
    body = criteria_body("Some intro prose.\n- First\n")
    with pytest.raises(TaskError, match="'## Acceptance criteria' has text outside bullets"):
        load_task(make_task(crucible_home, body=body))


def test_text_after_blank_line_is_error(crucible_home):
    body = criteria_body("- First\n\nA trailing paragraph.\n")
    with pytest.raises(TaskError, match="text outside bullets"):
        load_task(make_task(crucible_home, body=body))


def test_criteria_only_continuation_text_has_no_bullets(crucible_home):
    body = criteria_body("  indented prose\n  more prose\n")
    with pytest.raises(TaskError, match="has no bullet criteria"):
        load_task(make_task(crucible_home, body=body))


def test_committed_todo_list_criteria_are_whole():
    t = load_task(REPO_ROOT / "tasks" / "todo-list")
    assert len(t.criteria) == 6
    assert t.criteria[0] == (
        "`add` trims leading and trailing whitespace from the title. An empty or "
        "whitespace-only title raises `ValueError`."
    )
    assert "- " + t.criteria[0] in build_prompt(t)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_task.py -q`
Expected: FAIL — `test_wrapped_bullet_is_one_criterion` (criteria truncated to the first line), `test_nested_bullet_is_a_continuation`, `test_blank_line_ends_a_bullet` (`"Second"` without `wrapped`), the two `text outside bullets` tests (DID NOT RAISE), `test_committed_todo_list_criteria_are_whole` (truncated first criterion). `test_criteria_only_continuation_text_has_no_bullets` may already pass.

- [ ] **Step 3: Implement**

In `src/crucible/task.py`, add after `_positive_int` (ends around line 95):

```python
def _criteria(text: str) -> tuple[list[str], bool]:
    """Parse the Acceptance criteria section. Returns (criteria, stray).

    A bullet (`-` or `*`) at column 0, or any bullet while none is open, starts a criterion.
    Any other non-blank line continues the open criterion (Markdown lazy continuation), joined
    with one space after stripping its whitespace; an indented nested bullet (`  - sub`) is a
    continuation too and keeps its `- ` text. A blank line closes the open criterion. `stray`
    is True when a non-blank line had no open criterion to continue.
    """
    criteria: list[str] = []
    is_open = False
    stray = False
    for line in text.splitlines():
        if not line.strip():
            is_open = False
            continue
        m = BULLET_RE.match(line)
        if m and (not is_open or not line[:1].isspace()):
            criteria.append(m.group(1))
            is_open = True
        elif is_open:
            criteria[-1] += " " + line.strip()
        else:
            stray = True
    return criteria, stray
```

In `load_task`, replace:

```python
    criteria = [m.group(1) for line in sections["Acceptance criteria"].splitlines()
                if (m := BULLET_RE.match(line))]
    if not criteria:
        fail("section '## Acceptance criteria' has no bullet criteria")
```

with:

```python
    criteria, stray = _criteria(sections["Acceptance criteria"])
    if not criteria:
        fail("section '## Acceptance criteria' has no bullet criteria")
    if stray:
        fail("section '## Acceptance criteria' has text outside bullets")
```

In `docs/design.md`, replace the paragraph (lines 67-69):

```
The prompt given to every config is a fixed preamble (`src/crucible/prompt.py`) plus the
What, Why and Acceptance criteria sections, verbatim. The prompt text and its SHA-256 are
stored in every attempt record so identical input can be proven.
```

with:

```
The prompt given to every config is a fixed preamble (`src/crucible/prompt.py`) plus the
What, Why and Acceptance criteria sections, verbatim. The prompt text and its SHA-256 are
stored in every attempt record so identical input can be proven.

Acceptance criteria are the section's bullets (`-` or `*`), used by both the agent prompt and
the judge. A non-blank line that does not start a bullet continues the bullet before it
(Markdown lazy continuation) and is joined to it with one space after stripping its leading
whitespace; an indented nested bullet (`  - sub`) is a continuation too and keeps its `- `
text. A blank line ends a bullet. A section with no bullet fails validation (`has no bullet
criteria`); text that continues no bullet (before the first bullet, or after a blank line)
fails with `has text outside bullets`. Fixing the old first-line-only parsing changed the
prompt SHA of wrapped tasks such as `tasks/todo-list`; the report's prompt-changed warning
covers that and no attempt was rerun.
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_task.py -q`
Expected: PASS (all, including the unchanged `test_rule_at_least_one_bullet`, which still gets `no bullet`).

- [ ] **Step 5: Run the whole suite and lint**

Run: `uv run pytest -q && uv run ruff check .`
Expected: all tests pass, `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add src/crucible/task.py tests/test_task.py docs/design.md
git commit -m "$(cat <<'EOF'
fix: keep wrapped acceptance criteria whole

A bullet that wrapped onto following lines lost everything after its first line in both
the agent and judge prompts. Continuation lines now join the bullet; stray prose fails
validation.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: D2 — success is the check's verdict when a check ran; judge reported beside it

**Files:**
- Modify: `src/crucible/records.py:70-83` (`compute_success`), add `judge_disagrees`
- Modify: `src/crucible/aggregate.py:7-10` (imports), `:39-62` (`summarize_config`)
- Modify: `src/crucible/cli.py:158-162` (dry-run block), `:209-223` (`REPORT_COLUMNS`)
- Modify: `skills/run/SKILL.md:15-22` (steps 1 and 2)
- Modify: `docs/design.md:121-125` (success rule)
- Test: `tests/test_records.py`, `tests/test_report.py`, `tests/test_recommend.py`, `tests/test_judge.py:174-182`, `tests/test_run.py`

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `records.compute_success(record: dict) -> bool | None` (new rule).
  - `records.judge_disagrees(record: dict) -> bool`.
  - Row key `judge_disagreements: int` from `aggregate.summarize_config`.
  - `summarize_config` reads success only through `compute_success`; the stored `success` field is never read by reports. Later tasks extend `summarize_config` by inserting keys into the returned dict.
  - Table column `("Judge≠check", ...)` as the last entry of `cli.REPORT_COLUMNS`.
  - `crucible run --dry-run` prints a line `check: <task.check>` or `check: none` right after the `task ... prompt sha256 ...` line.
  - Test helper `tests/test_report.py::make_record(..., check=None, judge=None)`: when `check` is None it derives a check block from `success`.

**Circular import check:** `aggregate` will import from `records`. `records` imports only `crucible.paths` (which imports only stdlib), and nothing imports `aggregate` except `cli`, so there is no cycle. Use `from crucible.records import compute_success, judge_disagrees` (a function-level name, not `import records`), because `summarize_config`'s parameter is named `records` and would shadow the module. If a future change makes `records` import `aggregate`, move `compute_success` and `judge_disagrees` into a new `src/crucible/success.py` that imports nothing from `crucible`, and import it from both.

- [ ] **Step 1: Write the failing tests**

In `tests/test_records.py`, replace the last two asserts of `test_success_follows_verdicts`:

```python
    assert records.compute_success(rec(check=chk(True), judge=jdg(False))) is False
    assert records.compute_success(rec(check=chk(False), judge=jdg(True))) is False
```

with:

```python
    assert records.compute_success(rec(check=chk(True), judge=jdg(False))) is True  # check wins
    assert records.compute_success(rec(check=chk(False), judge=jdg(True))) is False
```

and append:

```python
def test_check_ran_without_verdict_falls_back_to_judge():
    no_verdict = {"ran": True, "passed": None, "exit_code": None}
    assert records.compute_success(rec(check=no_verdict, judge={"ran": True, "passed": True})) is True
    assert records.compute_success(rec(check=no_verdict, judge={"ran": True, "passed": False})) is False
    assert records.compute_success(rec(check=no_verdict)) is None


def test_judge_disagrees_needs_two_verdicts():
    chk = lambda p: {"ran": True, "passed": p, "exit_code": 0 if p else 1}
    jdg = lambda p: {"ran": True, "passed": p}
    assert records.judge_disagrees(rec(check=chk(True), judge=jdg(False))) is True
    assert records.judge_disagrees(rec(check=chk(False), judge=jdg(True))) is True
    assert records.judge_disagrees(rec(check=chk(True), judge=jdg(True))) is False
    assert records.judge_disagrees(rec(check=chk(True))) is False  # judge did not run
    assert records.judge_disagrees(rec(check=chk(True), judge=jdg(None))) is False
    no_verdict = {"ran": True, "passed": None, "exit_code": None}
    assert records.judge_disagrees(rec(check=no_verdict, judge=jdg(False))) is False
    assert records.judge_disagrees({"status": "completed"}) is False  # old record, no blocks
```

In `tests/test_report.py`, change `make_record` so the check block follows `success` (reports now recompute success from it). Replace the signature and the record dict:

```python
def make_record(home, task="t1", batch="20261001T000000Z-aaaa", config="sonnet:medium", n=1,
                success=True, wall=10.0, tokens_by_model=None, sha="sha-one", kind="feature",
                computed=999.0, check=None, judge=None):
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
        "wall_seconds": wall, "status": "completed",
        "tokens": {"input": sum(t["input"] for t in tbm.values()), "output": 0,
                   "cache_creation": 0, "cache_read": 0},
        "tokens_by_model": tbm, "cost_usd": {"computed": computed, "reported": 0.0},
        "check": check, "judge": judge or {"ran": False, "passed": None},
        "success": success,
    }
    (d / "record.json").write_text(json.dumps(rec))
    return rec
```

In `test_sort_by_rate_then_cost` (same file), replace the inner `rec` with:

```python
    def rec(cfg, ok, inp):
        m, e = cfg.split(":")
        return {"config": {"model": m, "effort": e}, "status": "completed",
                "check": {"ran": ok is not None, "passed": ok}, "success": ok,
                "wall_seconds": 1.0, "tokens_by_model": sonnet(inp)}
```

Append to `tests/test_report.py`:

```python
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
```

In `tests/test_recommend.py`, replace the `rec` helper:

```python
def rec(task, kind, cfg, ok, inp=M, wall=10.0):
    m, e = cfg.split(":")
    return {"task_id": task, "task_kind": kind, "config": {"model": m, "effort": e},
            "status": "completed", "check": {"ran": ok is not None, "passed": ok},
            "success": ok, "wall_seconds": wall,
            "tokens_by_model": {"claude-sonnet-5-5": {"input": inp}}}
```

In `tests/test_judge.py`, replace `test_failing_criterion_gives_judge_false_and_success_false` (line 174) with:

```python
def test_failing_criterion_gives_judge_false_but_check_decides_success(crucible_home, monkeypatch):
    n = len(criteria(crucible_home))
    monkeypatch.setenv("FAKE_CLAUDE_RESULT", verdict(crucible_home, [True] * (n - 1) + [False]))
    assert run(crucible_home, "--judge") == 0
    r = only_record()
    assert r["check"]["passed"] is True
    assert r["judge"]["passed"] is False
    assert r["judge"]["criteria_passed"] == n - 1 and r["judge"]["criteria_total"] == n
    assert r["success"] is True  # the check ran, so its verdict is success; the judge is advisory
```

Append to `tests/test_run.py`:

```python
def test_dry_run_says_whether_task_has_a_check(crucible_home, capsys):
    assert main(["run", task_dir(crucible_home), "--configs", "sonnet:low", "--dry-run"]) == 0
    assert "check: ./check.sh" in capsys.readouterr().out.splitlines()
    md = crucible_home / "tasks" / "example-fizzbuzz" / "task.md"
    md.write_text(md.read_text().replace("check: ./check.sh\n", ""))
    assert main(["run", task_dir(crucible_home), "--configs", "sonnet:low", "--dry-run"]) == 0
    assert "check: none" in capsys.readouterr().out.splitlines()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_records.py tests/test_report.py tests/test_recommend.py tests/test_judge.py tests/test_run.py -q`
Expected: FAIL — `AttributeError: module 'crucible.records' has no attribute 'judge_disagrees'`, `compute_success(...) is True` assertion in `test_success_follows_verdicts`, `KeyError: 'Judge≠check'`, `success is True` in the judge test, and `check: ./check.sh` missing from the dry run.

- [ ] **Step 3: Implement**

In `src/crucible/records.py`, replace `compute_success` (lines 70-83) with:

```python
def compute_success(record: Record) -> bool | None:
    """docs/design.md success rule: false if not completed; else the check's verdict when the
    check produced one; else the judge's verdict; else null (unknown)."""
    if record.get("status") != "completed":
        return False
    check = record.get("check") or {}
    if check.get("ran") and check.get("passed") is not None:
        return bool(check["passed"])
    judge = record.get("judge") or {}
    if judge.get("ran") and judge.get("passed") is not None:
        return bool(judge["passed"])
    return None


def judge_disagrees(record: Record) -> bool:
    """True when the check and the judge both produced a verdict and the verdicts differ."""
    check = record.get("check") or {}
    judge = record.get("judge") or {}
    if not (check.get("ran") and check.get("passed") is not None):
        return False
    if not (judge.get("ran") and judge.get("passed") is not None):
        return False
    return bool(check["passed"]) != bool(judge["passed"])
```

In `src/crucible/aggregate.py`, change the imports (lines 7-10) to:

```python
import statistics
from typing import Any

from crucible import pricing
from crucible.records import compute_success, judge_disagrees
```

and replace `summarize_config` (lines 39-62) with:

```python
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
        "success_rate": successes / len(known) if known else None,
        "median_wall_seconds": _median(walls),
        "min_wall_seconds": min(walls) if walls else None,
        "max_wall_seconds": max(walls) if walls else None,
        "median_tokens": _median([total_tokens(r) for r in records]),
        "median_cost": _median(costs),
        "cost_per_success": cost_per_success,
        "unpriced": len(records) - len(costs),
        "judge_disagreements": sum(1 for r in records if judge_disagrees(r)),
    }
```

In `src/crucible/cli.py`, in `REPORT_COLUMNS` add a last entry after `("Unpriced", ...)`:

```python
    ("Unpriced", lambda r: str(r["unpriced"])),
    ("Judge≠check", lambda r: str(r["judge_disagreements"])),
]
```

In `_run`, replace the dry-run block:

```python
    if args.dry_run:
        print(f"task {task.id}  prompt sha256 {sha}")
        for a in attempts:
```

with:

```python
    if args.dry_run:
        print(f"task {task.id}  prompt sha256 {sha}")
        print(f"check: {task.check or 'none'}")
        for a in attempts:
```

In `skills/run/SKILL.md`, replace steps 1 and 2 (lines 15-22):

```
1. **Resolve the command.** Build `crucible run .crucible/tasks/<id> <flags>`. If the user gave
   neither `--matrix` nor `--configs`, use `--matrix default`. Add `--judge` unless the user said
   not to judge. Pass `--repeat`, `--judge-config` and `--max-budget-usd` through only if the user
   gave them. Never raise `--max-budget-usd` on your own.
2. **Preview.** Run the same command plus `--dry-run` (it costs nothing) and show the planned
   attempts and their count. Each attempt is a real `claude` run, and `--judge` adds a judge run
   per attempt. State the per-attempt budget cap (`--max-budget-usd`, default 5.0) so the user
   sees the worst case.
```

with:

```
1. **Resolve the command.** Build `crucible run .crucible/tasks/<id> <flags>`. If the user gave
   neither `--matrix` nor `--configs`, use `--matrix default`. Pass `--repeat`, `--judge-config`
   and `--max-budget-usd` through only if the user gave them. Never raise `--max-budget-usd` on
   your own. Do not add `--judge` yet; step 2 decides.
2. **Preview.** Run the same command plus `--dry-run` (it costs nothing). Its `check:` line says
   whether the task has a check. Add `--judge` only when it says `check: none` or the user asked
   for judging, and if you add it, run the dry run again with it. Show the planned attempts and
   their count. Each attempt is a real `claude` run, and `--judge` adds a judge run per attempt.
   When the task has a check and `--judge` is on, say that the judge is advisory: the check
   decides success and the report shows judge disagreements beside it. State the per-attempt
   budget cap (`--max-budget-usd`, default 5.0) so the user sees the worst case.
```

In `docs/design.md`, replace the success rule (lines 121-125):

```
`success` (`records.compute_success`), evaluated in this order:
1. `status` is not `completed` → `false`.
2. Neither check nor judge produced a verdict (`check.ran` false and `judge.passed` null or
   judge not run) → `null` (unknown), never `true`.
3. Otherwise `true` only if every verdict produced is a pass.
```

with:

```
`success` (`records.compute_success`), evaluated in this order:
1. `status` is not `completed` → `false`.
2. The check ran and produced a verdict (`check.ran` and `check.passed` not null) →
   `check.passed`.
3. Else the judge ran and produced a verdict (`judge.ran` and `judge.passed` not null) →
   `judge.passed`.
4. Else `null` (unknown), never `true`.

When a check produced a verdict it decides; the judge is advisory beside it, because it is an
uncalibrated model grade and ANDing it in let a judge false negative fail a passing check.
Reports recompute `success` from each record with `compute_success` and never read the stored
field (the same way they re-price cost from tokens), so older records follow this rule without
being edited; `judge.apply` and `runner.build_record` still store it for convenience. Report
rows count `judge_disagreements` (`records.judge_disagrees`: the check and the judge both
produced a verdict and they differ), shown in the `Judge≠check` column. The judge's own verdict
stays in `judge.json` and in the record's `judge` block. `crucible run --dry-run` prints
`check: <path>` or `check: none`, and the run skill adds `--judge` only for tasks without a
check or when the user asks.
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_records.py tests/test_report.py tests/test_recommend.py tests/test_judge.py tests/test_run.py tests/test_report_kinds.py -q`
Expected: PASS.

- [ ] **Step 5: Run the whole suite and lint**

Run: `uv run pytest -q && uv run ruff check .`
Expected: all tests pass, `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add src/crucible/records.py src/crucible/aggregate.py src/crucible/cli.py skills/run/SKILL.md docs/design.md tests/test_records.py tests/test_report.py tests/test_recommend.py tests/test_judge.py tests/test_run.py
git commit -m "$(cat <<'EOF'
fix: take success from the check when it ran; report judge disagreements

The judge was ANDed into success, so an uncalibrated judge could fail a passing check.
Reports now recompute success from each record (check verdict, else judge, else unknown)
and count check/judge disagreements in a new column. The run skill only adds --judge for
tasks without a check.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: D3 — 95% Wilson intervals on success rates

**Files:**
- Modify: `src/crucible/aggregate.py` (add `import math`, `Z95`, `wilson_interval`; one key in `summarize_config`)
- Modify: `src/crucible/cli.py:205-244` (`_fmt_num` neighbourhood: add `_fmt_ci`, `_fmt_rate`; `REPORT_COLUMNS` `Success rate`; `_evidence`)
- Modify: `docs/design.md` (new `## Reports` section immediately above `## Mining`)
- Test: `tests/test_report.py`, `tests/test_report_kinds.py`

**Interfaces:**
- Consumes: `summarize_config` from Task 2 (returns dict with `successes`, `unknown`, `judge_disagreements`, ...).
- Produces:
  - `aggregate.Z95 = 1.96`
  - `aggregate.wilson_interval(successes: int, n: int, z: float = Z95) -> tuple[float, float] | None`
  - Row key `success_ci: tuple[float, float] | None` (a 2-element list in JSON), inserted right after `success_rate`.
  - `cli._fmt_ci(row: dict) -> str | None` (`"44–100%"`), `cli._fmt_rate(row: dict) -> str` (`"100% (44–100%)"`, `"—"`).
  - `_evidence` success text: `success 100% (3/3 known, 95% CI 44–100%)`.
  - `docs/design.md` gains a `## Reports` section above `## Mining`; Tasks 4–7 append paragraphs at the end of it.

- [ ] **Step 1: Write the failing tests**

In `tests/test_report.py`, add `import pytest` to the imports (after `import json`), then update existing asserts:

- In `test_two_configs_three_attempts`, replace `assert s["Success rate"] == "67%"` with `assert s["Success rate"] == "67% (21–94%)"`, and `assert o["Success rate"] == "0%"` with `assert o["Success rate"] == "0% (0–56%)"`.
- In `test_unknown_success_rate`, replace `s["Success rate"] == "50%"` with `s["Success rate"] == "50% (9–91%)"`.
- In `test_json_matches_table`, replace `assert rows[0]["Success rate"] == f"{s['success_rate']:.0%}"` with:

```python
    assert rows[0]["Success rate"] == f"{s['success_rate']:.0%} (21–94%)"
    assert s["success_ci"] == pytest.approx([0.2076549551264879, 0.9385096847238394])
    assert o["success_ci"] == pytest.approx([0.0, 0.5615060804490177])
```

Append to `tests/test_report.py`:

```python
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
```

Append to `tests/test_report_kinds.py`:

```python
def test_evidence_shows_interval(crucible_home, capsys):
    for n in (1, 2, 3):
        make_record(crucible_home, task="r1", kind="refactor", config="sonnet:low", n=n,
                    success=True)
    _, out, _ = run(capsys)
    assert "Recommendation: sonnet:low" in out
    assert "success 100% (3/3 known, 95% CI 44–100%)" in out
    assert "| 100% (44–100%) |" in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_report.py tests/test_report_kinds.py -q`
Expected: FAIL — `AttributeError: module 'crucible.aggregate' has no attribute 'wilson_interval'`, `'67%' == '67% (21–94%)'`, `KeyError: 'success_ci'`, evidence text missing.

- [ ] **Step 3: Implement**

In `src/crucible/aggregate.py`, change the imports to:

```python
import math
import statistics
from typing import Any

from crucible import pricing
from crucible.records import compute_success, judge_disagrees
```

Add after `_median`:

```python
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
```

In `summarize_config`'s returned dict, insert right after the `"success_rate": ...` line:

```python
        "success_ci": wilson_interval(successes, len(known)),
```

In `src/crucible/cli.py`, add after `_fmt_num`:

```python
def _fmt_ci(row: dict) -> str | None:
    ci = row.get("success_ci")
    return None if not ci else f"{ci[0] * 100:.0f}–{ci[1] * 100:.0f}%"


def _fmt_rate(row: dict) -> str:
    """`100% (44–100%)`: the success rate and its 95% Wilson interval; `—` when unknown."""
    if row["success_rate"] is None:
        return "—"
    ci = _fmt_ci(row)
    return f"{row['success_rate']:.0%}" + (f" ({ci})" if ci else "")
```

In `REPORT_COLUMNS`, replace `("Success rate", lambda r: _fmt_num(r["success_rate"], ".0%")),` with:

```python
    ("Success rate", _fmt_rate),
```

Replace `_evidence` with:

```python
def _evidence(row: dict) -> str:
    known = row["attempts"] - row["unknown"]
    cost = "—" if row["median_cost"] is None else f"${row['median_cost']:.4f}"
    return (
        f"{row['tasks']} tasks, {row['attempts']} attempts, "
        f"success {_fmt_num(row['success_rate'], '.0%')} ({row['successes']}/{known} known, "
        f"95% CI {_fmt_ci(row) or '—'}), "
        f"median cost {cost}, median time {_fmt_num(row['median_wall_seconds'], '.1f')}s"
    )
```

In `docs/design.md`, insert immediately above the line `## Mining`:

```
## Reports

`crucible report` aggregates attempt records per config (`aggregate.summarize_config`),
grouped by the `model:effort` label. Every number is recomputed from the records: cost from
`tokens_by_model`, success from `compute_success`.

**Success-rate interval.** Each row carries `success_ci`, the 95% Wilson score interval
`[low, high]` of successes over attempts with a known result (null when there are none). With
z = 1.96 and p = k/n: centre = (p + z²/2n) / (1 + z²/n), half = z·√(p(1−p)/n + z²/4n²) /
(1 + z²/n), and the interval is centre ± half clamped to [0, 1]. Tables show it in the
`Success rate` cell (`100% (44–100%)`) and recommendation evidence repeats it, so 3 passes out
of 3 reads as "somewhere between 44% and 100%", not as certainty. Pure stdlib (`math.sqrt`).

```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_report.py tests/test_report_kinds.py tests/test_recommend.py -q`
Expected: PASS.

- [ ] **Step 5: Run the whole suite and lint**

Run: `uv run pytest -q && uv run ruff check .`
Expected: all tests pass, `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add src/crucible/aggregate.py src/crucible/cli.py docs/design.md tests/test_report.py tests/test_report_kinds.py
git commit -m "$(cat <<'EOF'
feat: show 95% Wilson intervals on success rates

A 3/3 pass rate printed as a bare 100%. Rows now carry success_ci and the table and
recommendation evidence show it, e.g. 100% (44–100%).

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: D4 — rank qualifiers by cost per success

**Files:**
- Modify: `src/crucible/aggregate.py:106-147` (`_cheapest_key`, `recommend`)
- Modify: `src/crucible/cli.py` (`_evidence`: add cost per success)
- Modify: `docs/design.md` (append to `## Reports`)
- Test: `tests/test_recommend.py`, `tests/test_report_kinds.py`

**Interfaces:**
- Consumes: rows from `summarize_config` (Tasks 2–3) with `cost_per_success`, `median_wall_seconds`, `config`, `success_rate`, `success_ci`.
- Produces:
  - `aggregate._cheapest_key(row) -> tuple` ranks by `cost_per_success`, then `median_wall_seconds`, then `config`; `None` sorts last.
  - `aggregate.recommend(rows, min_attempts=3, threshold=0.8)` qualifier condition `r["cost_per_success"] is not None` (replaces `median_cost is not None`). Return shape unchanged.
  - `_evidence` text gains `cost per success $X.XXXX` after `median cost`.
  - Test helper `tests/test_recommend.py::row(..., cps=_SAME)`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_recommend.py`, replace the `row` helper:

```python
_SAME = object()


def row(config, attempts=5, successes=5, unknown=0, cost=1.0, wall=10.0, tasks=1, cps=_SAME):
    known = attempts - unknown
    return {
        "config": config, "attempts": attempts, "successes": successes, "unknown": unknown,
        "success_rate": successes / known if known else None,
        "median_cost": cost, "median_wall_seconds": wall, "tasks": tasks,
        "cost_per_success": cost if cps is _SAME else cps,
    }
```

Append:

```python
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
    out = aggregate.recommend([row("slow:low", cps=2.0, wall=50.0), row("fast:low", cps=2.0, wall=5.0)])
    assert out["config"] == "fast:low"
```

In `tests/test_report_kinds.py`, replace `test_threshold_flag` (the old assertion depended on ranking by median cost):

```python
def test_threshold_flag(crucible_home, capsys):
    build(crucible_home)
    _, out, _ = run(capsys)
    assert "no config meets the bar" in kind_section(out, "feature")
    _, out, _ = run(capsys, "--threshold", "0.3")
    feat = kind_section(out, "feature")
    # sonnet:low (1/3) now qualifies too; it and opus:high (2/3) both cost $6.0000 per
    # success and take the same time, so the tie goes to the label
    assert "Recommendation: opus:high" in feat and "Runner-up: sonnet:low" in feat
```

Append:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_recommend.py tests/test_report_kinds.py -q`
Expected: FAIL — `test_ranks_by_cost_per_success_not_median_cost` picks `flaky:low`, `test_qualifier_needs_cost_per_success` picks `n:low`, `test_recommendation_ranks_by_cost_per_success` recommends `sonnet:low`, `test_threshold_flag` recommends `sonnet:low`.

- [ ] **Step 3: Implement**

In `src/crucible/aggregate.py`, replace `_cheapest_key`:

```python
def _cheapest_key(row: Row) -> tuple:
    """Lower cost per success, then lower median wall time, then label; unknown values sort
    last. Cost per success (total priced cost ÷ priced successes) answers "what does a passing
    result cost": failed attempts are part of that cost, so a config that is cheap per attempt
    but often fails does not win on price alone."""
    cps, wall = row["cost_per_success"], row["median_wall_seconds"]
    return (cps is None, cps or 0.0, wall is None, wall or 0.0, row["config"])
```

In `recommend`, replace the docstring and the qualifier condition:

```python
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
```

(The rest of `recommend` is unchanged.)

In `src/crucible/cli.py`, replace `_evidence` with:

```python
def _evidence(row: dict) -> str:
    known = row["attempts"] - row["unknown"]
    cost = "—" if row["median_cost"] is None else f"${row['median_cost']:.4f}"
    cps = "—" if row["cost_per_success"] is None else f"${row['cost_per_success']:.4f}"
    return (
        f"{row['tasks']} tasks, {row['attempts']} attempts, "
        f"success {_fmt_num(row['success_rate'], '.0%')} ({row['successes']}/{known} known, "
        f"95% CI {_fmt_ci(row) or '—'}), "
        f"median cost {cost}, cost per success {cps}, "
        f"median time {_fmt_num(row['median_wall_seconds'], '.1f')}s"
    )
```

In `docs/design.md`, append at the end of the `## Reports` section (immediately above `## Mining`):

```
**Recommendation.** `aggregate.recommend` picks from per-config rows. A config qualifies with
at least `min_attempts` (default 3) attempts of known result, a success rate of at least
`threshold` (default 0.8) and a non-null `cost_per_success`. Qualifiers are ranked by
`cost_per_success` (total priced cost of every attempt ÷ priced successes), then median wall
time, then label; unknown values sort last. The question a recommendation answers is "what
does a passing result cost", and failed attempts are part of that cost, so a config that is
cheap per attempt but fails often can lose to a pricier one that passes every time.
`median_cost` stays in the table. If nothing qualifies, `best_effort` is the highest success
rate (ties by the same key). Evidence lines show the interval, median cost and cost per
success.

```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_recommend.py tests/test_report_kinds.py tests/test_report.py -q`
Expected: PASS.

- [ ] **Step 5: Run the whole suite and lint**

Run: `uv run pytest -q && uv run ruff check .`
Expected: all tests pass, `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add src/crucible/aggregate.py src/crucible/cli.py docs/design.md tests/test_recommend.py tests/test_report_kinds.py
git commit -m "$(cat <<'EOF'
fix: rank recommended configs by cost per success

Ranking by median attempt cost ignored that failed attempts are part of what a passing
result costs. Qualifiers now need and sort by cost per success.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: D6 — warn when one config label pools several resolved models

**Files:**
- Modify: `src/crucible/aggregate.py` (`summarize_config`: one key)
- Modify: `src/crucible/cli.py` (add `_pool_warnings`; print in `_report` and `_cross_task_report`)
- Modify: `docs/design.md` (append to `## Reports`)
- Test: `tests/test_report.py`

**Interfaces:**
- Consumes: `summarize_config` (Tasks 2–4); `_cross_task_report` / `_report` as left by Task 3/4.
- Produces:
  - Row key `resolved_models: list[str]` (sorted distinct non-null `resolved_model`).
  - `cli._pool_warnings(rows: list[dict]) -> list[str]`, each `warning: <config> pools <n> models: <a>, <b>`.
  - Text reports print the warnings on stdout right after each table; `--json` prints them on stderr.
  - Test helper `make_record(..., resolved_model=None, status="completed")` (Task 6 adds `subtype`).

- [ ] **Step 1: Write the failing tests**

In `tests/test_report.py`, extend `make_record`: add `resolved_model=None, status="completed"` to the end of the signature, and in the `rec` dict replace `"wall_seconds": wall, "status": "completed",` with:

```python
        "resolved_model": resolved_model, "wall_seconds": wall, "status": status,
```

Append:

```python
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
    assert warnings == ["warning: sonnet:low pools 2 models: claude-sonnet-5-5, claude-sonnet-5-6"]
    assert out.index("warning:") > out.rindex("|")
    _, jout, err = run_report(capsys, "t1", "--json")
    by = {c["config"]: c for c in json.loads(jout)["configs"]}
    assert by["sonnet:low"]["resolved_models"] == ["claude-sonnet-5-5", "claude-sonnet-5-6"]
    assert by["opus:high"]["resolved_models"] == ["claude-opus-5-5"]
    assert "warning: sonnet:low pools 2 models" in err
    _, out, _ = run_report(capsys)  # cross-task report warns too
    assert "warning: sonnet:low pools 2 models" in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_report.py::test_label_pooling_several_models_warns -q`
Expected: FAIL — `assert [] == ['warning: sonnet:low pools 2 models: ...']`.

- [ ] **Step 3: Implement**

In `src/crucible/aggregate.py`, `summarize_config`'s returned dict, insert after the `"judge_disagreements": ...` line:

```python
        "resolved_models": sorted(
            {r["resolved_model"] for r in records if r.get("resolved_model")}
        ),
```

(Grouping in `aggregate_configs` stays by `config_label`; do not change it.)

In `src/crucible/cli.py`, add after `format_report_table`:

```python
def _pool_warnings(rows: list[dict]) -> list[str]:
    """One line per row whose label pooled attempts from more than one resolved model."""
    return [
        f"warning: {r['config']} pools {len(r['resolved_models'])} models: "
        f"{', '.join(r['resolved_models'])}"
        for r in rows if len(r.get("resolved_models") or []) > 1
    ]
```

In `_report`, replace:

```python
    if args.json:
        print(json.dumps({**summary, "configs": rows}, indent=2))
        if warning:
            print(warning, file=sys.stderr)
        return 0
```

with:

```python
    if args.json:
        print(json.dumps({**summary, "configs": rows}, indent=2))
        if warning:
            print(warning, file=sys.stderr)
        for w in _pool_warnings(rows):
            print(w, file=sys.stderr)
        return 0
```

and at the end of `_report` replace:

```python
    print(format_report_table(rows))
    return 0
```

with:

```python
    print(format_report_table(rows))
    for w in _pool_warnings(rows):
        print(w)
    return 0
```

In `_cross_task_report`, replace:

```python
    if args.json:
        print(json.dumps({"min_attempts": args.min_attempts, "threshold": args.threshold,
                          "kinds": reports}, indent=2))
        return 0
```

with:

```python
    if args.json:
        print(json.dumps({"min_attempts": args.min_attempts, "threshold": args.threshold,
                          "kinds": reports}, indent=2))
        for rep in reports:
            for w in _pool_warnings(rep["configs"]):
                print(w, file=sys.stderr)
        return 0
```

and replace the final loop's last lines:

```python
        print(format_report_table(rep["configs"], KIND_COLUMNS))
    return 0
```

with:

```python
        print(format_report_table(rep["configs"], KIND_COLUMNS))
        for w in _pool_warnings(rep["configs"]):
            print(w)
    return 0
```

In `docs/design.md`, append at the end of `## Reports` (immediately above `## Mining`):

```
**Resolved models.** Rows are grouped by the `model:effort` label, never by `resolved_model`
(errored attempts have none and must stay in their label's row). Each row lists
`resolved_models`, the sorted distinct non-null `resolved_model` values. When there is more than
one, the report prints `warning: <label> pools N models: a, b` after the table (on stderr with
`--json`), because an alias such as `sonnet` can move to a newer model between batches.

```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_report.py tests/test_report_kinds.py -q`
Expected: PASS.

- [ ] **Step 5: Run the whole suite and lint**

Run: `uv run pytest -q && uv run ruff check .`
Expected: all tests pass, `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add src/crucible/aggregate.py src/crucible/cli.py docs/design.md tests/test_report.py
git commit -m "$(cat <<'EOF'
feat: warn when a config label pools several resolved models

Rows list resolved_models and the report warns when an alias like sonnet:low mixed
model versions across batches.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: D7 — stopped attempts are visible; budget cap per task

**Files:**
- Modify: `src/crucible/aggregate.py` (`STOP_SUBTYPES`, `is_stopped`, `stopped` row key)
- Modify: `src/crucible/cli.py` (`REPORT_COLUMNS` `Stopped`; `_run` budget resolution and dry-run line; `--max-budget-usd` argparse default)
- Modify: `src/crucible/task.py` (`import math`; `Task.max_budget_usd`; validation; `TEMPLATE`, `REPO_TEMPLATE`)
- Modify: `skills/run/SKILL.md` (step 2 budget sentence)
- Modify: `docs/design.md` (Task document front matter block; append to `## Reports`)
- Test: `tests/test_report.py`, `tests/test_run.py`, `tests/test_task.py`

**Interfaces:**
- Consumes: `make_record(..., resolved_model=None, status="completed")` from Task 5; `summarize_config` (Tasks 2–5); dry-run block from Task 2 (prints `task ...` then `check: ...`).
- Produces:
  - `aggregate.STOP_SUBTYPES = frozenset({"error_max_turns", "error_max_budget_usd"})`, `aggregate.is_stopped(record) -> bool`, row key `stopped: int` (right after `unknown`).
  - Column `("Stopped", ...)` immediately after `("Unknown", ...)` in `REPORT_COLUMNS` (so also in `KIND_COLUMNS`).
  - `Task.max_budget_usd: float | int | None = None` (dataclass field after `source`).
  - `cli.DEFAULT_MAX_BUDGET_USD = 5.0`, `cli._budget(args, task) -> tuple[float, str]` returning `(cap, source)` with source in `"--max-budget-usd" | "task" | "default"`.
  - Dry run prints `max budget per attempt: $<cap:g> (<source>)` after the `check:` line.
  - Test helpers `tests/test_report.py::make_record(..., subtype="success")`, `tests/test_run.py::set_front(home, extra: str)` (Task 9 reuses it).

- [ ] **Step 1: Write the failing tests**

In `tests/test_report.py`, add `subtype="success"` to the end of `make_record`'s signature and in the `rec` dict replace `"resolved_model": resolved_model, "wall_seconds": wall, "status": status,` with:

```python
        "resolved_model": resolved_model, "wall_seconds": wall, "status": status,
        "subtype": subtype,
```

Append to `tests/test_report.py`:

```python
def test_stopped_column(crucible_home, capsys):
    make_record(crucible_home, n=1, success=True)
    make_record(crucible_home, n=2, success=False, status="timeout", subtype=None)
    make_record(crucible_home, n=3, success=False, status="error", subtype="error_max_turns")
    make_record(crucible_home, n=4, success=False, status="error", subtype="error_max_budget_usd")
    make_record(crucible_home, n=5, success=False, status="error", subtype=None)  # crash
    _, out, _ = run_report(capsys, "t1")
    (row,) = parse_table(out)
    assert row["Stopped"] == "3" and row["Attempts"] == "5" and row["Successes"] == "1"
    assert row["Success rate"].startswith("20% ")  # stopped attempts still count as failures
    header_line = next(ln for ln in out.splitlines() if ln.startswith("|"))
    header = [c.strip() for c in header_line.strip("|").split("|")]
    assert header.index("Stopped") == header.index("Unknown") + 1
    _, jout, _ = run_report(capsys, "t1", "--json")
    assert json.loads(jout)["configs"][0]["stopped"] == 3
```

Append to `tests/test_run.py`:

```python
def set_front(home, extra: str) -> None:
    """Add front-matter lines to the copied example task (after `max_turns: 30`)."""
    md = home / "tasks" / "example-fizzbuzz" / "task.md"
    md.write_text(md.read_text().replace("max_turns: 30\n", f"max_turns: 30\n{extra}"))


def test_task_max_budget_used_unless_flag_given(crucible_home, tmp_path, monkeypatch):
    set_front(crucible_home, "max_budget_usd: 1.25\n")
    argv_file = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_CLAUDE_ARGV_FILE", str(argv_file))
    assert main(["run", task_dir(crucible_home), "--configs", "sonnet:medium"]) == 0
    argv = json.loads(argv_file.read_text())
    assert argv[argv.index("--max-budget-usd") + 1] == "1.25"
    # an explicit flag wins, even when it equals the CLI default
    assert main(["run", task_dir(crucible_home), "--configs", "sonnet:medium",
                 "--max-budget-usd", "5"]) == 0
    argv = json.loads(argv_file.read_text())
    assert argv[argv.index("--max-budget-usd") + 1] == "5.0"


def test_default_budget_without_task_value(crucible_home, tmp_path, monkeypatch):
    argv_file = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_CLAUDE_ARGV_FILE", str(argv_file))
    assert main(["run", task_dir(crucible_home), "--configs", "sonnet:medium"]) == 0
    argv = json.loads(argv_file.read_text())
    assert argv[argv.index("--max-budget-usd") + 1] == "5.0"


def test_dry_run_shows_effective_budget(crucible_home, capsys):
    def dry(*extra):
        assert main(["run", task_dir(crucible_home), "--configs", "sonnet:low", "--dry-run",
                     *extra]) == 0
        return capsys.readouterr().out.splitlines()

    assert "max budget per attempt: $5 (default)" in dry()
    set_front(crucible_home, "max_budget_usd: 1.25\n")
    assert "max budget per attempt: $1.25 (task)" in dry()
    assert "max budget per attempt: $0.5 (--max-budget-usd)" in dry("--max-budget-usd", "0.5")


def test_non_positive_budget_flag_rejected(crucible_home, capsys):
    assert main(["run", task_dir(crucible_home), "--configs", "sonnet:low",
                 "--max-budget-usd", "0"]) == 1
    assert "--max-budget-usd must be positive" in capsys.readouterr().err
```

Append to `tests/test_task.py`:

```python
def test_max_budget_usd_optional_positive_number(crucible_home):
    assert load_task(make_task(crucible_home)).max_budget_usd is None
    d = make_task(crucible_home, name="t2", front="id: t2\nkind: x\nrepo: fixture\nmax_budget_usd: 1.5")
    assert load_task(d).max_budget_usd == 1.5
    d = make_task(crucible_home, name="t3", front="id: t3\nkind: x\nrepo: fixture\nmax_budget_usd: 2")
    assert load_task(d).max_budget_usd == 2


@pytest.mark.parametrize("value", ["0", "-1", "lots", "true", ".nan", ".inf"])
def test_max_budget_usd_must_be_positive_number(crucible_home, value):
    d = make_task(crucible_home, front=f"id: t1\nkind: x\nrepo: fixture\nmax_budget_usd: {value}")
    with pytest.raises(TaskError, match="max_budget_usd"):
        load_task(d)


def test_templates_show_commented_budget(crucible_home):
    from crucible.task import repo_task_md

    assert main(["task", "new", "demo"]) == 0
    assert "# max_budget_usd:" in (crucible_home / "tasks" / "demo" / "task.md").read_text()
    assert "# max_budget_usd:" in repo_task_md("x", "../..", "abc123")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_report.py tests/test_run.py tests/test_task.py -q`
Expected: FAIL — `KeyError: 'Stopped'`, task budget ignored (`'5.0' == '1.25'`), no `max budget per attempt` line, `AttributeError: 'Task' object has no attribute 'max_budget_usd'`, no `# max_budget_usd:` in templates.

- [ ] **Step 3: Implement**

`src/crucible/aggregate.py` — add after `config_label`:

```python
STOP_SUBTYPES = frozenset({"error_max_turns", "error_max_budget_usd"})


def is_stopped(record: Record) -> bool:
    """The attempt was ended by a limit (timeout, turn cap, budget cap), not by the model."""
    return record.get("status") == "timeout" or record.get("subtype") in STOP_SUBTYPES
```

In `summarize_config`'s returned dict, insert right after the `"unknown": ...` line:

```python
        "stopped": sum(1 for r in records if is_stopped(r)),
```

`src/crucible/cli.py` — in `REPORT_COLUMNS`, insert right after `("Unknown", lambda r: str(r["unknown"])),`:

```python
    ("Stopped", lambda r: str(r["stopped"])),
```

Add above `_run`:

```python
DEFAULT_MAX_BUDGET_USD = 5.0


def _budget(args: argparse.Namespace, task) -> tuple[float, str]:
    """Per-attempt cap and where it came from: the flag when given on the command line (its
    argparse default is None, so `--max-budget-usd 5` still counts as given), else the task's
    `max_budget_usd`, else the default."""
    if args.max_budget_usd is not None:
        return args.max_budget_usd, "--max-budget-usd"
    if task.max_budget_usd is not None:
        return float(task.max_budget_usd), "task"
    return DEFAULT_MAX_BUDGET_USD, "default"
```

In `_run`, replace:

```python
    if args.max_budget_usd <= 0:
        print("--max-budget-usd must be positive", file=sys.stderr)
        return 1
```

with:

```python
    budget, budget_from = _budget(args, task)
    if budget <= 0:
        print("--max-budget-usd must be positive", file=sys.stderr)
        return 1
```

Replace the dry-run block:

```python
    if args.dry_run:
        print(f"task {task.id}  prompt sha256 {sha}")
        print(f"check: {task.check or 'none'}")
```

with:

```python
    if args.dry_run:
        print(f"task {task.id}  prompt sha256 {sha}")
        print(f"check: {task.check or 'none'}")
        print(f"max budget per attempt: ${budget:g} ({budget_from})")
```

In the attempt loop replace `task, batch_id, a, prompt_text, sha, args.max_budget_usd, args.keep_workspaces,` with:

```python
                task, batch_id, a, prompt_text, sha, budget, args.keep_workspaces,
```

In the `run` argparse block replace `p.add_argument("--max-budget-usd", type=float, default=5.0)` with:

```python
            p.add_argument("--max-budget-usd", type=float, default=None,
                           help="per-attempt spend cap in USD (default: the task's "
                                "max_budget_usd, else 5.0)")
```

`src/crucible/task.py` — add `import math` to the imports. Add the dataclass field after `source`:

```python
    source: str | None = None  # mined tasks: PR URL, or `commit:<merge sha>`
    max_budget_usd: float | None = None  # per-attempt cap; `crucible run --max-budget-usd` wins
```

In `load_task`, after the `source` validation block add:

```python
    max_budget = fm.get("max_budget_usd")
    if max_budget is not None and (
        isinstance(max_budget, bool) or not isinstance(max_budget, (int, float))
        or not math.isfinite(max_budget) or max_budget <= 0
    ):
        fail(f"`max_budget_usd` must be a positive number, got {max_budget!r}")
```

and pass `max_budget_usd=max_budget,` in the `return Task(...)` call after `source=source,`.

In `TEMPLATE`, replace the line `# check: ./check.sh` with:

```
# check: ./check.sh
# max_budget_usd: 2.0   # optional per-attempt cap in USD; --max-budget-usd overrides it
```

In `REPO_TEMPLATE`, replace the line `{extra}check: ./check.sh` with:

```
{extra}# max_budget_usd: 2.0   # optional per-attempt cap in USD; --max-budget-usd overrides it
check: ./check.sh
```

`skills/run/SKILL.md` — in step 2 replace the final sentence `State the per-attempt
   budget cap (`--max-budget-usd`, default 5.0) so the user sees the worst case.` with:

```
   State the per-attempt budget cap from the dry run's `max budget per attempt` line (the flag,
   else the task's `max_budget_usd`, else 5.0) so the user sees the worst case.
```

`docs/design.md` — in the Task document front-matter block, replace the line
`max_turns: 100                # optional, default 100` with:

```
max_turns: 100                # optional, default 100
max_budget_usd: 2.0           # optional per-attempt spend cap (USD); --max-budget-usd overrides it
```

Append at the end of `## Reports` (immediately above `## Mining`):

```
**Stopped attempts.** `stopped` counts attempts that a limit ended: `status` `timeout`, or
`subtype` `error_max_turns` or `error_max_budget_usd` (`aggregate.is_stopped`). The `Stopped`
column follows `Unknown`. Stopped attempts still count as failures in the success rate (the
config did not deliver), but the reader can see that a limit, not the model, ended them.
`crucible run` caps each attempt at `--max-budget-usd` when the flag is given, else at the
task's `max_budget_usd`, else at 5.0; `--dry-run` prints the effective cap and its source
(`max budget per attempt: $1.25 (task)`). `crucible task new` templates show the field
commented out.

```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_report.py tests/test_run.py tests/test_task.py tests/test_report_kinds.py -q`
Expected: PASS.

- [ ] **Step 5: Run the whole suite and lint**

Run: `uv run pytest -q && uv run ruff check .`
Expected: all tests pass, `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add src/crucible/aggregate.py src/crucible/cli.py src/crucible/task.py skills/run/SKILL.md docs/design.md tests/test_report.py tests/test_run.py tests/test_task.py
git commit -m "$(cat <<'EOF'
feat: show stopped attempts and allow a per-task budget cap

Timeouts and turn/budget stops were indistinguishable from plain failures; a Stopped
column now counts them. Tasks may set max_budget_usd, used unless --max-budget-usd is
given; the dry run prints the effective cap.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: D5 — kind recommendations compare configs on the same tasks

**Files:**
- Modify: `src/crucible/aggregate.py:150-172` (`kind_reports`; add `common_tasks`, `_add_task_counts`)
- Modify: `src/crucible/cli.py` (`_format_recommendation`: `no_common_tasks`; `_cross_task_report`: `Compared on` line)
- Modify: `docs/design.md` (append to `## Reports`)
- Test: `tests/test_recommend.py`, `tests/test_report_kinds.py`

**Interfaces:**
- Consumes: `aggregate_configs`, `recommend` (Task 4 ranking), `_pool_warnings` (Task 5). `_cross_task_report`'s text loop currently ends with the table then the pool-warning loop.
- Produces:
  - `aggregate.common_tasks(records: list[Record]) -> list[str]`: sorted task ids attempted by every config that has at least one attempt in `records` (`[]` for no records; `None` task ids ignored).
  - Each `kind_reports` entry: `{"kind", "tasks", "attempts", "configs", "common_tasks", "comparable", "recommendation"}`. `configs` = pooled rows (unchanged). `comparable` = `aggregate_configs` over records whose `task_id` is in `common_tasks`, each row with `tasks`. `recommendation` = `recommend(comparable, ...)`, or when `common_tasks` is empty `{"min_attempts", "threshold", "status": "no_common_tasks", "config": None, "evidence": None, "best_effort": None, "runner_up": None}`.
  - **A config with attempts in the kind but on no common task:** by definition every config with an attempt in the kind attempted every common task, so this happens only when `common_tasks` is `[]`. Then `comparable` is `[]`, every config appears only in the pooled `configs` table, and the recommendation is `no_common_tasks`. When `common_tasks` is non-empty, every config in `configs` also has a row in `comparable` (with only its common-task attempts).
  - `--json` output: each kind object gains `common_tasks` (list of task ids) and `comparable` (list of rows, same shape as `configs` rows including `tasks`); `recommendation.evidence`, `runner_up` and `best_effort` are `comparable` rows. `configs`, `tasks`, `attempts` keep their pooled meaning.
  - Text: `Compared on N of M tasks attempted by every config` (N = `len(common_tasks)`, M = the kind's task count) printed right before the recommendation lines; the pooled table and warnings print as before. `no_common_tasks` prints `Recommendation: none; no task was attempted by every config`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_recommend.py`:

```python
def test_kind_reports_compare_on_common_tasks():
    recs = ([rec("t1", "bugfix", "a:low", True) for _ in range(3)]
            + [rec("t2", "bugfix", "a:low", False) for _ in range(3)]
            + [rec("t1", "bugfix", "b:low", True, 2 * M) for _ in range(3)])
    (bug,) = aggregate.kind_reports(recs)
    assert bug["common_tasks"] == ["t1"]
    pooled = {r["config"]: r for r in bug["configs"]}
    comp = {r["config"]: r for r in bug["comparable"]}
    assert pooled["a:low"]["attempts"] == 6 and pooled["a:low"]["tasks"] == 2
    assert pooled["a:low"]["success_rate"] == 0.5
    assert set(comp) == {"a:low", "b:low"}
    assert comp["a:low"]["attempts"] == 3 and comp["a:low"]["tasks"] == 1
    out = bug["recommendation"]
    assert out["status"] == "recommended" and out["config"] == "a:low"
    assert out["evidence"]["attempts"] == 3 and out["runner_up"]["config"] == "b:low"


def test_kind_with_disjoint_tasks_has_no_common_tasks():
    recs = ([rec("t1", "bugfix", "a:low", True) for _ in range(3)]
            + [rec("t2", "bugfix", "b:low", True) for _ in range(3)])
    (bug,) = aggregate.kind_reports(recs)
    assert bug["common_tasks"] == [] and bug["comparable"] == []
    assert [r["config"] for r in bug["configs"]] == ["a:low", "b:low"]
    out = bug["recommendation"]
    assert out["status"] == "no_common_tasks" and out["config"] is None
    assert out["evidence"] is None and out["best_effort"] is None and out["runner_up"] is None
    assert out["min_attempts"] == 3 and out["threshold"] == 0.8


def test_common_tasks_helper():
    assert aggregate.common_tasks([]) == []
    recs = [rec("t2", "k", "a:low", True), rec("t1", "k", "a:low", True),
            rec("t1", "k", "b:low", True), rec("t2", "k", "b:low", True),
            rec("t3", "k", "b:low", True)]
    assert aggregate.common_tasks(recs) == ["t1", "t2"]
```

In `tests/test_report_kinds.py`:

- In `test_two_kinds_each_get_recommendation_and_table`, replace `assert "2 tasks" in bug and "5 attempts" in bug  # opus:high evidence` with:

```python
    assert "Compared on 1 of 2 tasks attempted by every config" in bug
    assert "1 tasks, 3 attempts" in bug  # opus:high evidence: b1 only, the one common task
    assert bug.index("Compared on") < bug.index("Recommendation:")
    assert "Compared on 1 of 1 tasks attempted by every config" in feat
```

- In `test_json_matches_text`, replace `assert rec["evidence"]["success_rate"] == 1.0 and rec["evidence"]["tasks"] == 2` with `assert rec["evidence"]["success_rate"] == 1.0 and rec["evidence"]["tasks"] == 1`, replace `assert rec["evidence"]["median_cost"] == 4.0 and rec["evidence"]["attempts"] == 5` with `assert rec["evidence"]["median_cost"] == 4.0 and rec["evidence"]["attempts"] == 3`, and append to the end of that test:

```python
    assert bug["common_tasks"] == ["b1"] and feat["common_tasks"] == ["f1"]
    assert [c["config"] for c in bug["comparable"]] == [
        "sonnet:medium", "opus:high", "sonnet:high", "sonnet:low"]
    assert all(c["tasks"] == 1 for c in bug["comparable"])
    comp_low = next(c for c in bug["comparable"] if c["config"] == "sonnet:low")
    assert comp_low["attempts"] == 3 and comp_low["successes"] == 2
```

Append to `tests/test_report_kinds.py`:

```python
def test_disjoint_tasks_give_no_recommendation(crucible_home, capsys):
    for n in (1, 2, 3):
        make_record(crucible_home, task="d1", kind="docs", config="sonnet:low", n=n, success=True)
        make_record(crucible_home, task="d2", kind="docs", config="opus:high", n=n, success=True,
                    tokens_by_model=opus(M))
    code, out, _ = run(capsys)
    assert code == 0
    assert "Compared on 0 of 2 tasks attempted by every config" in out
    assert "Recommendation: none; no task was attempted by every config" in out
    table = [ln for ln in out.splitlines() if ln.startswith("|")]
    assert len(table) == 4  # the pooled table still lists both configs
    code, jout, _ = run(capsys, "--json")
    (kind,) = json.loads(jout)["kinds"]
    assert kind["common_tasks"] == [] and kind["comparable"] == []
    assert kind["recommendation"]["status"] == "no_common_tasks"
    assert kind["recommendation"]["config"] is None
    assert len(kind["configs"]) == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_recommend.py tests/test_report_kinds.py -q`
Expected: FAIL — `KeyError: 'common_tasks'`, `AttributeError: ... 'common_tasks'`, `Compared on` missing, evidence still `2 tasks, 5 attempts`.

- [ ] **Step 3: Implement**

In `src/crucible/aggregate.py`, replace `kind_reports` (lines 150-172) with:

```python
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
        row["tasks"] = len({r.get("task_id") for r in records if config_label(r) == row["config"]})
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
        if common:
            rec = recommend(comparable, min_attempts, threshold)
        else:
            rec = {"min_attempts": min_attempts, "threshold": threshold,
                   "status": "no_common_tasks", "config": None, "evidence": None,
                   "best_effort": None, "runner_up": None}
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
```

In `src/crucible/cli.py`, at the top of `_format_recommendation` (before `lines = []`), add:

```python
    if rec["status"] == "no_common_tasks":
        return ["Recommendation: none; no task was attempted by every config"]
```

In `_cross_task_report`'s text loop, replace:

```python
        print(f"## Kind: {rep['kind']}  ({rep['tasks']} tasks, {rep['attempts']} attempts)")
        print()
        print("\n".join(_format_recommendation(rep["recommendation"])))
```

with:

```python
        print(f"## Kind: {rep['kind']}  ({rep['tasks']} tasks, {rep['attempts']} attempts)")
        print()
        print(f"Compared on {len(rep['common_tasks'])} of {rep['tasks']} tasks attempted by "
              "every config")
        print("\n".join(_format_recommendation(rep["recommendation"])))
```

(The table and `_pool_warnings` lines after it stay as Task 5 left them.)

In `docs/design.md`, append at the end of `## Reports` (immediately above `## Mining`):

```
**Kind reports.** The cross-task report (`crucible report` without a task id) groups records
by `task_kind`. Each kind prints the pooled per-config table (`configs`: every attempt in the
kind, each row with `tasks`), but recommends only from `comparable`: rows aggregated over the
kind's `common_tasks`, the task ids attempted by every config that has at least one attempt in
that kind, so a config that only ran the easy task is not compared with one that ran them all.
The text prints `Compared on N of M tasks attempted by every config` before the recommendation
lines. With no common task (for example each config ran a different task) `comparable` is
empty, the recommendation status is `no_common_tasks` with a null config, and the report says
`Recommendation: none; no task was attempted by every config`. `--json` adds `common_tasks` and
`comparable` to each kind; the recommendation's `evidence`, `runner_up` and `best_effort` are
`comparable` rows.

```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_recommend.py tests/test_report_kinds.py tests/test_report.py -q`
Expected: PASS.

- [ ] **Step 5: Run the whole suite and lint**

Run: `uv run pytest -q && uv run ruff check .`
Expected: all tests pass, `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add src/crucible/aggregate.py src/crucible/cli.py docs/design.md tests/test_recommend.py tests/test_report_kinds.py
git commit -m "$(cat <<'EOF'
fix: recommend per kind only from tasks every config attempted

Pooled kind rows compared configs across different task sets. The recommendation now
uses rows over the tasks common to every config, says how many tasks that is, and
declines when there are none.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: D8 — mined checks can run a regression suite

**Files:**
- Modify: `src/crucible/mine/config.py` (`_SUITE_KEYS`; `Suite.regression_command`, `to_dict`; `_parse_suite`; `REGRESSION_SUGGESTIONS`, `suggest_regression`; `_TEMPLATE_HEADER`, `_REGRESSION_NOTE`, `render_default`)
- Modify: `src/crucible/mine/scaffold.py:52-97` (`check_script`)
- Modify: `docs/design.md` (`### .crucible/mine.yaml` example and validation paragraph; `detect_suites` paragraph; `### Scaffold and prove` bullets)
- Test: `tests/test_mine_config.py`, `tests/test_mine_scaffold.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `Suite.regression_command: str = ""` (last dataclass field, so positional `Suite(name, test_paths, test_command, cwd, setup, module_root, env)` calls keep working). `to_dict()` includes `regression_command` after `test_command` only when set, so `settings_hash()` of suites without it is unchanged (verify cache stays valid; adding one re-verifies that suite, which is harmless).
  - `config.REGRESSION_SUGGESTIONS: dict[str, str]` keyed by the detected `test_command`; `config.suggest_regression(suite: Suite) -> str` (`""` when none).
  - `detect_suites` keeps returning suites with `regression_command == ""`; the suggestion is written commented out by `render_default`, so `load_config(write_default(...))` still equals the detected suites.
  - `scaffold.check_script(suites, runnable) -> str`: unchanged output for suites without `regression_command`.

Current generator (`src/crucible/mine/scaffold.py:78-97`), for reference:

```python
    if len(suites) == 1:
        suite = suites[0]
        lines += _suite_prelude(suite)
        if suite.setup:
            lines.append(f'bash -c {shlex.quote(suite.setup)} || {{ echo "{SETUP_FAILED}" >&2; '
                         f"exit {SETUP_EXIT}; }}")
        lines.append(f"exec bash -c {shlex.quote(verify.build_command(suite, runnable))}")
        return "\n".join(lines) + "\n"
    lines.append("rc=0")
    for suite, files in plan:
        pre = "; ".join(_suite_prelude(suite))
        lines.append(f"echo {shlex.quote(f'== suite {suite.name} ==')}")
        if suite.setup:
            lines.append(f"( {pre}; bash -c {shlex.quote(suite.setup)} ) || {{ "
                         f'echo {shlex.quote(f"{SETUP_FAILED} ({suite.name})")} >&2; exit {SETUP_EXIT}; }}')
        cmd = verify.build_command(suite, files)
        lines.append(f"( {pre}; bash -c {shlex.quote(cmd)} ); code=$?")
        lines.append('[ "$code" -ne 0 ] && [ "$rc" -eq 0 ] && rc=$code')
    lines.append('exit "$rc"')
    return "\n".join(lines) + "\n"
```

Setup still runs before tests and still `exit 97`s at once; the regression runs after the suite's tests in the same `cwd`/`env` (same prelude in the multi-suite subshell, the main shell's prelude in the single-suite case) and its exit code is collected exactly like a test failure (first failing code wins, every suite still runs).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_mine_config.py`:

```python
def with_regression(cmd: str) -> str:
    return GOOD.replace("    module_root: backend\n",
                        f"    module_root: backend\n    regression_command: {cmd}\n")


def test_regression_command_loads_and_is_optional(tmp_path):
    s = load(tmp_path, with_regression("python manage.py test")).suites[0]
    assert s.regression_command == "python manage.py test"
    assert s.to_dict()["regression_command"] == "python manage.py test"
    plain = load(tmp_path, GOOD).suites[0]
    assert plain.regression_command == "" and "regression_command" not in plain.to_dict()


@pytest.mark.parametrize("cmd,needle", [
    ("python -m pytest {tests}", "{tests}"),
    ("python manage.py test {modules}", "{modules}"),
])
def test_regression_command_rejects_placeholders(tmp_path, cmd, needle):
    errs = errors_of(tmp_path, with_regression(cmd))
    assert any(e.startswith("suites[0].regression_command: must not contain placeholders")
               and needle in e for e in errs)


def test_regression_command_must_be_a_string(tmp_path):
    errs = errors_of(tmp_path, with_regression("[a, b]"))
    assert any("suites[0].regression_command: must be a string" in e for e in errs)


@pytest.mark.parametrize("files,expected", [
    ({"pyproject.toml": "[tool.pytest.ini_options]\n"}, "python -m pytest -q"),
    ({"manage.py": ""}, "python manage.py test"),
    ({"package.json": json.dumps({"devDependencies": {"vitest": "^1"}})}, "npx vitest run"),
    ({"package.json": json.dumps({"devDependencies": {"jest": "^1"}})}, "npx jest"),
])
def test_suggest_regression_per_stack(tmp_path, files, expected):
    for rel, text in files.items():
        write(tmp_path, rel, text)
    [s] = mc.detect_suites(tmp_path)
    assert mc.suggest_regression(s) == expected
    assert s.regression_command == ""  # suggested, never switched on by detection


def test_written_default_suggests_regression_commented_out(tmp_path):
    write(tmp_path, "backend/manage.py")
    write(tmp_path, "frontend/package.json", pkg("vitest"))
    out = tmp_path / "mine.yaml"
    mc.write_default(out, tmp_path)
    text = out.read_text()
    assert "  # regression_command: python manage.py test\n" in text
    assert "  # regression_command: npx vitest run\n" in text
    assert "whole suite" in text and "prove" in text
    assert [s.regression_command for s in mc.load_config(out).suites] == ["", ""]
    out.write_text(text.replace("  # regression_command:", "  regression_command:"))
    assert [s.regression_command for s in mc.load_config(out).suites] == [
        "python manage.py test", "npx vitest run"]


def test_empty_template_mentions_regression_command(tmp_path):
    out = tmp_path / "mine.yaml"
    mc.write_default(out, tmp_path)
    assert "regression_command:" in out.read_text()
```

Append to `tests/test_mine_scaffold.py` (add `from crucible.mine.config import Suite` to the imports):

```python
def run_script(tmp_path, suites, files, **env):
    """Generate check.sh for `suites` and run it in a fresh workspace with no hidden tests."""
    task = tmp_path / "task"
    (task / "hidden_tests").mkdir(parents=True, exist_ok=True)
    (task / "check.sh").write_text(sc.check_script(suites, files))
    ws = tmp_path / "ws"
    for s in suites if isinstance(suites, list) else [suites]:
        (ws / s.cwd).mkdir(parents=True, exist_ok=True)
    return subprocess.run(["bash", str(task / "check.sh")], cwd=ws, capture_output=True,
                          text=True, check=False,
                          env={"PATH": os.environ["PATH"], "CRUCIBLE_TASK_DIR": str(task), **env})


REG_SUITE = Suite("py", ["tests/**"], "echo tests {tests}; exit $TRC",
                  regression_command="echo regression-ran; exit $RRC")


def test_single_suite_without_regression_is_unchanged():
    script = sc.check_script(Suite("py", ["tests/**"], "echo tests {tests}"), ["tests/test_a.py"])
    assert script.rstrip().splitlines()[-1] == "exec bash -c 'echo tests tests/test_a.py'"
    assert "regression" not in script


@pytest.mark.parametrize("trc,rrc,expected", [("0", "0", 0), ("0", "3", 3), ("1", "3", 1),
                                              ("1", "0", 1)])
def test_single_suite_runs_regression_after_tests(tmp_path, trc, rrc, expected):
    r = run_script(tmp_path, REG_SUITE, ["tests/test_a.py"], TRC=trc, RRC=rrc)
    assert r.returncode == expected
    assert "== regression py ==" in r.stdout and "regression-ran" in r.stdout
    assert r.stdout.index("tests tests/test_a.py") < r.stdout.index("regression-ran")


def test_setup_failure_exits_97_before_regression(tmp_path):
    s = Suite("py", ["tests/**"], "echo tests {tests}", setup="false",
              regression_command="echo regression-ran")
    r = run_script(tmp_path, s, ["tests/test_a.py"])
    assert r.returncode == 97 and "regression-ran" not in r.stdout
    assert sc.SETUP_FAILED in r.stderr


def test_multi_suite_regression_failures_are_collected(tmp_path):
    files = ["a/tests/test_a.py", "b/tests/test_b.py"]
    a = Suite("a", ["a/**"], "echo a-tests", cwd="a",
              regression_command="echo a-regression; exit 4")
    b = Suite("b", ["b/**"], "echo b-tests; exit $BRC", cwd="b")
    r = run_script(tmp_path, [a, b], files, BRC="0")
    assert r.returncode == 4
    assert "a-regression" in r.stdout and "b-tests" in r.stdout  # b still ran
    assert r.stdout.count("== suite") == 2 and "== regression a ==" in r.stdout
    assert run_script(tmp_path, [a, b], files, BRC="6").returncode == 4  # first failure wins
    b_bad_setup = Suite("b", ["b/**"], "echo b-tests", cwd="b", setup="false")
    assert run_script(tmp_path, [a, b_bad_setup], files).returncode == 97


def test_failing_regression_makes_prove_fail_at_merge(repo, crucible_home, capsys):
    (crucible_home / "mine.yaml").write_text(
        mine_yaml(f"{PY} -m pytest -q -p no:cacheprovider {{tests}}")
        + '    regression_command: "echo regression-ran; exit 5"\n')
    cid = ids(repo, "fix")[0]
    assert verify("--verify", cid) == 0
    assert scaffold(cid) == 0
    d = tasks(crucible_home) / f"commit-{cid}-fix-add"
    assert "regression-ran" in (d / "check.sh").read_text()
    fill(d)
    capsys.readouterr()
    assert main(["mine", "prove", str(d)]) == 1
    err = capsys.readouterr().err
    assert "fails at merge" in err and "regression-ran" in err
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_mine_config.py tests/test_mine_scaffold.py -q`
Expected: FAIL — `suites[0].regression_command: unknown field`, `TypeError: Suite.__init__() got an unexpected keyword argument 'regression_command'`, `AttributeError: ... 'suggest_regression'`.

- [ ] **Step 3: Implement**

`src/crucible/mine/config.py`:

Replace `_SUITE_KEYS`:

```python
_SUITE_KEYS = {"name", "test_paths", "cwd", "setup", "test_command", "module_root", "env",
               "regression_command"}
```

In `Suite`, add the last field and extend `to_dict`:

```python
    env: dict[str, str] = field(default_factory=dict)
    regression_command: str = ""  # optional whole-suite command run after the PR's tests

    def to_dict(self) -> dict:
        """Plain-data form (stable key order), e.g. for hashing a suite's settings."""
        d = {"name": self.name, "test_paths": self.test_paths, "cwd": self.cwd}
        if self.setup:
            d["setup"] = self.setup
        d["test_command"] = self.test_command
        if self.regression_command:
            d["regression_command"] = self.regression_command
        if self.module_root is not None:
            d["module_root"] = self.module_root
        if self.env:
            d["env"] = self.env
        return d
```

In `_parse_suite`, insert before `env = raw.get("env") or {}`:

```python
    regression = raw.get("regression_command") or ""
    if not isinstance(regression, str):
        errors.append(f"{where}.regression_command: must be a string")
        regression = ""
    else:
        found = sorted(set(re.findall(r"\{(tests|modules)\}", regression)))
        if found:
            errors.append(
                f"{where}.regression_command: must not contain placeholders "
                f"({', '.join('{' + p + '}' for p in found)}); it runs the whole suite"
            )
        regression = regression.strip()
```

and change the final return to:

```python
    return Suite(name, test_paths, command, cwd, setup, module_root, clean_env, regression)
```

Add after `detect_suites`:

```python
REGRESSION_SUGGESTIONS = {
    "python -m pytest {tests}": "python -m pytest -q",
    "python manage.py test {modules}": "python manage.py test",
    "npx vitest run {tests}": "npx vitest run",
    "npx jest {tests}": "npx jest",
}


def suggest_regression(suite: Suite) -> str:
    """The whole-suite `regression_command` suggested for a detected stack ('' if none)."""
    return REGRESSION_SUGGESTIONS.get(suite.test_command, "")
```

In `_TEMPLATE_HEADER`, after the line `#     test_command: python manage.py test {modules}   # {tests}: file paths relative to cwd` add:

```
#     regression_command: python manage.py test   # optional; whole suite, no placeholders
```

Add after `_TAIL`:

```python
_REGRESSION_NOTE = """\
  # regression_command: {cmd}
  #   optional: runs the whole suite at check time, after the PR's tests (no placeholders);
  #   the check fails if it fails. `crucible mine prove` rejects a task whose regression fails
  #   at the merge commit, so exclude pre-existing failures in the command or remove the line.
"""
```

Replace `render_default`:

```python
def render_default(suites: list[Suite]) -> str:
    """The commented mine.yaml text `crucible mine` writes on first use. Each detected suite
    is followed by its suggested `regression_command`, commented out."""
    exclude = json.dumps(DEFAULT_EXCLUDE)
    tail = _TAIL.format(exclude=exclude, max_lines=DEFAULT_MAX_LINES)
    if not suites:
        return _TEMPLATE_HEADER + "suites: []\n" + tail
    body = "suites:\n"
    for s in suites:
        body += yaml.safe_dump([s.to_dict()], sort_keys=False, default_flow_style=None,
                               width=100)
        cmd = suggest_regression(s)
        if cmd:
            body += _REGRESSION_NOTE.format(cmd=cmd)
    return _DETECTED_HEADER + body + tail
```

(`yaml.safe_dump([d])` emits `- name: ...` with keys indented two spaces, identical to the old `{"suites": [...]}` dump body, so uncommenting `  # regression_command:` to `  regression_command:` lands at key level.)

`src/crucible/mine/scaffold.py` — replace the single-suite branch:

```python
        lines.append(f"exec bash -c {shlex.quote(verify.build_command(suite, runnable))}")
        return "\n".join(lines) + "\n"
```

with:

```python
        test_cmd = verify.build_command(suite, runnable)
        if not suite.regression_command:
            lines.append(f"exec bash -c {shlex.quote(test_cmd)}")
            return "\n".join(lines) + "\n"
        lines.append(f"bash -c {shlex.quote(test_cmd)}; rc=$?")
        lines.append(f"echo {shlex.quote(f'== regression {suite.name} ==')}")
        lines.append(f"bash -c {shlex.quote(suite.regression_command)}; code=$?")
        lines.append('[ "$code" -ne 0 ] && [ "$rc" -eq 0 ] && rc=$code')
        lines.append('exit "$rc"')
        return "\n".join(lines) + "\n"
```

and in the multi-suite loop, after:

```python
        lines.append(f"( {pre}; bash -c {shlex.quote(cmd)} ); code=$?")
        lines.append('[ "$code" -ne 0 ] && [ "$rc" -eq 0 ] && rc=$code')
```

add:

```python
        if suite.regression_command:
            lines.append(f"echo {shlex.quote(f'== regression {suite.name} ==')}")
            lines.append(f"( {pre}; bash -c {shlex.quote(suite.regression_command)} ); code=$?")
            lines.append('[ "$code" -ne 0 ] && [ "$rc" -eq 0 ] && rc=$code')
```

Update the `check_script` docstring's last sentence to: `... a setup failure exits 97 at once, test and regression failures are collected so every suite runs, and the script exits with the first failing code. A suite's optional regression_command runs after its tests in the same cwd and env.`

`docs/design.md`:

In the `### .crucible/mine.yaml` example, replace the line `    test_command: python manage.py test {modules}` with:

```
    test_command: python manage.py test {modules}
    regression_command: python manage.py test   # optional; whole suite, no placeholders
```

In the validation paragraph, replace `` `{modules}` needs `module_root`. `` with:

```
`{modules}` needs `module_root`. `regression_command` is optional, a plain command with no
placeholders (`{tests}` or `{modules}` in it is an error).
```

At the end of the `detect_suites(repo_root)` paragraph (after `... an invalid file prints its errors and exits 1.`) add:

```
Each detected stack gets a suggested `regression_command` (`config.suggest_regression`: pytest
`python -m pytest -q`, Django `python manage.py test`, Vitest `npx vitest run`, Jest
`npx jest`), written commented out under its suite with a note that it runs the whole suite at
check time and that `prove` rejects a task whose regression fails at the merge commit (so
pre-existing failures must be excluded or the line removed). Detected suites themselves keep an
empty `regression_command`, so the written file loads back to exactly the detected suites.
```

In `### Scaffold and prove`, after the `- **Several suites.** ...` bullet add:

```
- **Regression suite.** When a suite has `regression_command`, `check.sh` runs it after that
  suite's PR tests, in the same `cwd` and `env`, printing `== regression <name> ==` first. Its
  exit code is collected like a test failure (the script exits with the first failing code,
  and every suite still runs); a setup failure still exits 97 before anything else. A
  single-suite task without one still `exec`s its test command, byte for byte as before.
  `prove` needs no change: a regression that fails at the merge commit shows up as
  `fails at merge`.
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_mine_config.py tests/test_mine_scaffold.py tests/test_mine_multi_suite.py tests/test_mine_verify.py -q`
Expected: PASS (including the unchanged `test_written_default_loads_and_roundtrips`).

- [ ] **Step 5: Run the whole suite and lint**

Run: `uv run pytest -q && uv run ruff check .`
Expected: all tests pass, `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add src/crucible/mine/config.py src/crucible/mine/scaffold.py docs/design.md tests/test_mine_config.py tests/test_mine_scaffold.py
git commit -m "$(cat <<'EOF'
feat: let mined checks run a regression suite

mine.yaml suites accept an optional regression_command that check.sh runs after the
PR's tests; a failure fails the check. Detection suggests one per stack, written
commented out.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
EOF
)"
```

---

### Task 9: D9 — mined tasks record their size

**Files:**
- Modify: `src/crucible/task.py` (`Task.source_lines`, validation, `return Task(...)`)
- Modify: `src/crucible/mine/scaffold.py:133-134` (front-matter `extra`)
- Modify: `src/crucible/runner.py:53-57` (`build_record`)
- Modify: `docs/design.md` (Task document front matter; record schema; scaffold `task.md` bullet)
- Test: `tests/test_task.py`, `tests/test_run.py:67-75`, `tests/test_mine_scaffold.py`

**Interfaces:**
- Consumes: `Task` dataclass with `max_budget_usd` (Task 6); `tests/test_run.py::set_front(home, extra)` (Task 6): writes `extra` after `max_turns: 30\n` in the copied example task.
- Produces: `Task.source_lines: int | None = None` (field after `max_budget_usd`); record key `task_source_lines: int | None` placed right after `task_kind`; mined `task.md` front matter `source_lines: <Candidate.size>` (omitted when 0).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_task.py`:

```python
def test_source_lines_optional_positive_int(crucible_home):
    assert load_task(make_task(crucible_home)).source_lines is None
    d = make_task(crucible_home, name="t2", front="id: t2\nkind: x\nrepo: fixture\nsource_lines: 120")
    assert load_task(d).source_lines == 120


@pytest.mark.parametrize("value", ["0", "-3", "many", "true", "1.5"])
def test_source_lines_must_be_positive_int(crucible_home, value):
    d = make_task(crucible_home, front=f"id: t1\nkind: x\nrepo: fixture\nsource_lines: {value}")
    with pytest.raises(TaskError, match="source_lines"):
        load_task(d)
```

In `tests/test_run.py::test_record_schema_and_order`, replace the expected key list with:

```python
    assert list(r) == [
        "schema_version", "task_id", "task_kind", "task_source_lines", "batch_id", "attempt",
        "config", "resolved_model", "prompt_sha256", "baseline_sha", "started_at",
        "wall_seconds", "status", "error", "num_turns", "session_id", "subtype",
        "reported_duration_ms", "tokens", "tokens_by_model", "cost_usd", "check", "judge",
        "success",
    ]
    assert r["task_source_lines"] is None
```

Append to `tests/test_run.py`:

```python
def test_record_carries_task_source_lines(crucible_home):
    set_front(crucible_home, "source_lines: 42\n")
    assert main(["run", task_dir(crucible_home), "--configs", "sonnet:low"]) == 0
    (r,) = records.load_records("example-fizzbuzz")
    assert r["task_source_lines"] == 42
```

In `tests/test_mine_scaffold.py::test_scaffold_writes_task_and_validate_fails_only_on_placeholders`, after `assert fm["timeout_minutes"] == 30 and fm["max_turns"] == 100` add:

```python
    # counted lines of the `fix add` commit: src/calc.py 1+1, tests/test_calc.py 4,
    # tests/helpers.py 1
    assert fm["source_lines"] == 7
```

and after the final `assert load_task(d).source == ...` add:

```python
    assert load_task(d).source_lines == 7
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_task.py tests/test_run.py tests/test_mine_scaffold.py -q`
Expected: FAIL — `AttributeError: 'Task' object has no attribute 'source_lines'`, record key list mismatch, `KeyError: 'source_lines'` in scaffold front matter.

- [ ] **Step 3: Implement**

`src/crucible/task.py` — add the dataclass field after `max_budget_usd`:

```python
    source_lines: int | None = None  # mined tasks: counted changed lines of the source change
```

In `load_task`, after the `max_budget_usd` validation add:

```python
    source_lines = fm.get("source_lines")
    if source_lines is not None and (
        isinstance(source_lines, bool) or not isinstance(source_lines, int) or source_lines <= 0
    ):
        fail(f"`source_lines` must be a positive integer, got {source_lines!r}")
```

and pass `source_lines=source_lines,` in `return Task(...)` after `max_budget_usd=max_budget,`.

`src/crucible/mine/scaffold.py` — in `scaffold_task_dir`, replace:

```python
        extra = (f"source: {json.dumps(c.source)}\n"
                 f"timeout_minutes: {DEFAULT_TIMEOUT_MINUTES}\nmax_turns: {DEFAULT_MAX_TURNS}\n")
```

with:

```python
        extra = (f"source: {json.dumps(c.source)}\n"
                 + (f"source_lines: {c.size}\n" if c.size > 0 else "")
                 + f"timeout_minutes: {DEFAULT_TIMEOUT_MINUTES}\nmax_turns: {DEFAULT_MAX_TURNS}\n")
```

(`Candidate.size` is `counted_lines`, falling back to `lines_changed`; a 0 would fail task validation, so it is omitted.)

`src/crucible/runner.py` — in `build_record`, replace:

```python
        "task_kind": task.kind,
        "batch_id": batch_id,
```

with:

```python
        "task_kind": task.kind,
        "task_source_lines": task.source_lines,
        "batch_id": batch_id,
```

`docs/design.md`:

In the Task document front-matter block, after the `max_budget_usd` line add:

```
source_lines: 42              # optional; written by mine scaffold (counted changed lines)
```

In the record schema JSON, replace `  "task_kind": "feature",` with:

```
  "task_kind": "feature",
  "task_source_lines": null,
```

After the schema block's closing fence (before the `` `success` (`records.compute_success`) `` paragraph) add:

```
`task_source_lines` copies the task's optional `source_lines` (null when absent), so results
can later be broken down by task size; no report uses it yet.
```

In the `### Scaffold and prove` `**task.md**` bullet, replace `` `source` (PR URL or `commit:<merge>`), `` with:

```
`source` (PR URL or `commit:<merge>`), `source_lines` (the candidate's counted lines, the
same number the `max_changed_lines` filter used; omitted when 0),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_task.py tests/test_run.py tests/test_mine_scaffold.py -q`
Expected: PASS.

- [ ] **Step 5: Run the whole suite and lint**

Run: `uv run pytest -q && uv run ruff check .`
Expected: all tests pass, `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add src/crucible/task.py src/crucible/mine/scaffold.py src/crucible/runner.py docs/design.md tests/test_task.py tests/test_run.py tests/test_mine_scaffold.py
git commit -m "$(cat <<'EOF'
feat: record the size of mined tasks

Mined tasks get source_lines (counted changed lines) in their front matter and attempt
records carry it as task_source_lines, so results can later be split by size.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
EOF
)"
```

---

### Task 10: D10 and final docs pass

**Files:**
- Modify: `docs/design.md` (new `## Executor` section between `## Task document` and `## Results layout`)
- Modify: `README.md:39-41` (budget), `:168-169` (success rule), `:171-180` (Report section)
- Modify: `skills/report/SKILL.md:18-23` (step 3)
- Test: none (documentation only); the full suite still runs.

**Interfaces:**
- Consumes: the behaviour of Tasks 1–9 (wording must match: `Judge≠check`, `Stopped`, `success_ci`, cost per success, `Compared on N of M tasks attempted by every config`, `no_common_tasks`, pool warnings, `max_budget_usd`).
- Produces: nothing code-facing.

- [ ] **Step 1: Check no earlier task left design.md work behind**

Run: `grep -n "Isolation versus fidelity\|## Reports\|Judge≠check\|regression_command\|source_lines\|max_budget_usd" docs/design.md`
Expected: `## Reports`, `Judge≠check`, `regression_command`, `source_lines`, `max_budget_usd` present (Tasks 2–9); `Isolation versus fidelity` absent. If any of the others is missing, add the text from the owning task before continuing.

- [ ] **Step 2: Add the executor section to `docs/design.md`**

`docs/design.md` has no executor section yet; insert this immediately above the line `## Results layout`:

```
## Executor

Each attempt runs the `claude` CLI headless in a fresh workspace (flags and output format in
`docs/claude-headless.md`).

**Isolation versus fidelity.** By default every attempt runs with `--safe-mode`, which drops
CLAUDE.md, skills, hooks, auto memory and MCP servers. Attempts are therefore reproducible and
comparable across machines, but they run without the project's own instructions: a config is
measured on the task text alone, not on how it behaves with the team's CLAUDE.md. That is
deliberate. A flag to include the project's CLAUDE.md at `ref` is an open follow-up.

```

- [ ] **Step 3: Update `README.md`**

Replace (lines 39-41):

```
Every attempt is capped by `--max-budget-usd` (default 5.0 per attempt; judge runs add cost, so
pass a lower cap for cheap trials). Only a real `crucible run` spends money, and the slash
commands never start one without you asking.
```

with:

```
Every attempt is capped by `--max-budget-usd` (else the task's `max_budget_usd`, else 5.0 per
attempt; judge runs add cost, so pass a lower cap for cheap trials). `--dry-run` prints the
effective cap. Only a real `crucible run` spends money, and the slash commands never start one
without you asking.
```

Replace (lines 168-169):

```
An attempt's `success` is true only if every verdict produced (check, judge) passed, and is
unknown if neither produced a verdict.
```

with:

```
An attempt's `success` is the check's verdict when the check produced one, else the judge's
verdict, and unknown if neither produced one. With a check, the judge is advisory: the report's
`Judge≠check` column counts attempts where the two disagree. Reports recompute `success` from
each record, so older records follow this rule too.
```

Replace the paragraph under `## Report` that starts `Options: --batch` (lines 178-181):

```
Options: `--batch`, `--json`, and for the cross-task report `--kind`, `--min-attempts`
(default 3) and `--threshold` (default 0.8). With fewer than `--min-attempts` attempts per
config the report says "no config meets the bar" and lists the best candidates instead.
Reports re-price every attempt from the recorded tokens and the current `pricing.yaml`.
```

with:

```
Options: `--batch`, `--json`, and for the cross-task report `--kind`, `--min-attempts`
(default 3) and `--threshold` (default 0.8). With fewer than `--min-attempts` attempts per
config the report says "no config meets the bar" and lists the best candidates instead.
Reports re-price every attempt from the recorded tokens and the current `pricing.yaml`.

Reading the table: `Success rate` shows a 95% Wilson interval (`100% (44–100%)` for 3 of 3),
`Stopped` counts attempts ended by a timeout, turn cap or budget cap (still failures), and
`Judge≠check` counts judge/check disagreements. A warning follows the table when one config
label pooled several model versions. The recommendation ranks configs that clear the bar by
cost per success (failed attempts included), and the cross-task report compares configs only
on the tasks every config attempted (`Compared on N of M tasks ...`); with no such task it
makes no recommendation.
```

- [ ] **Step 4: Update `skills/report/SKILL.md` step 3**

Replace:

```
3. Summarise in plain language, not by pasting the table alone:
   - the recommendation and the evidence behind it (success rate, median cost, median time);
   - the runner-up, and the trade-off against it;
   - caveats: few attempts, `Unknown` results, unpriced attempts, or a warning that the task
     changed between batches.
   If no config meets the bar, say so and name the best candidates instead of recommending one.
```

with:

```
3. Summarise in plain language, not by pasting the table alone:
   - the recommendation and the evidence behind it (success rate with its 95% interval, cost
     per success, median time), and on how many tasks it was compared
     (`Compared on N of M tasks attempted by every config`);
   - the runner-up, and the trade-off against it;
   - caveats: few attempts or a wide interval, `Unknown` results, `Stopped` attempts (a limit
     ended them), `Judge≠check` disagreements, unpriced attempts, a warning that a config pooled
     several models, or a warning that the task changed between batches.
   If no config meets the bar, or no task was attempted by every config, say so and name the
   best candidates instead of recommending one.
```

- [ ] **Step 5: Run the whole suite and lint**

Run: `uv run pytest -q && uv run ruff check .`
Expected: all tests pass (including `tests/test_plugin.py` frontmatter checks), `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add docs/design.md README.md skills/report/SKILL.md
git commit -m "$(cat <<'EOF'
docs: document isolation versus fidelity and the new report fields

design.md records that --safe-mode trades the project's CLAUDE.md for reproducibility.
README and the report skill explain intervals, cost-per-success ranking, common-task
comparison, stopped attempts, judge disagreements and pooled-model warnings.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
EOF
)"
```

---

## Self-review

**Spec coverage.**
- D1 → Task 1 (rule, nested bullets, blank line, outside-bullets error, design.md, no rerun).
- D2 → Task 2 (rule order, reports recompute via `compute_success`, `judge_disagreements` + `Judge≠check`, `judge.apply`/`build_record` untouched, run skill text).
- D3 → Task 3 (`success_ci`, Wilson formula, cell format, evidence line, stdlib).
- D4 → Task 4 (`_cheapest_key` by `cost_per_success`, wall, label; qualifier condition; docstring; design.md).
- D5 → Task 7 (`common_tasks`, `comparable` with `tasks`, recommendation from `comparable`, `no_common_tasks`, `Compared on` line, JSON).
- D6 → Task 5 (`resolved_models`, warning text, grouping unchanged incl. errored attempts).
- D7 → Task 6 (`stopped`, `Stopped` after `Unknown`, still failures, task `max_budget_usd`, flag wins, default 5.0, templates, dry run).
- D8 → Task 8 (`regression_command`, no placeholders, runs after tests in same cwd/env, exit collected, suggestions, commented default with the required note, prove unchanged and tested).
- D9 → Task 9 (`source_lines` front matter, `Task.source_lines`, `task_source_lines` record field).
- D10 → Task 10.
- Global constraints: `schema_version` stays 1; new record field is additive; old records load (fields read with `.get`); no `results/` edits; design.md updated in each owning task.

**Placeholder scan.** No TBD/TODO; every code step has code; every test step has full tests.

**Type consistency.** `compute_success`/`judge_disagrees` (Task 2) used by `summarize_config`; `wilson_interval` → `success_ci` (Task 3) consumed by `_fmt_ci`/`_fmt_rate`/`_evidence`; `cost_per_success` already existed; `resolved_models` (Task 5) → `_pool_warnings`; `stopped`/`is_stopped` (Task 6); `Task.max_budget_usd` (Task 6) → `_budget`; `common_tasks`/`comparable`/`no_common_tasks` (Task 7) → CLI; `Suite.regression_command`/`suggest_regression` (Task 8); `Task.source_lines` → `task_source_lines` (Task 9). `make_record` gains `check, judge` (Task 2), `resolved_model, status` (Task 5), `subtype` (Task 6) in that order; `set_front` defined in Task 6, reused in Task 9.

**Review Focus.** All five lines have a test in the owning task (Tasks 1, 2, 3, 7, 8).
