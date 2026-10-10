# Crucible

[![CI](https://github.com/Jaxsonman/Crucible/actions/workflows/ci.yml/badge.svg)](https://github.com/Jaxsonman/Crucible/actions/workflows/ci.yml)

Crucible benchmarks Claude model and effort configurations on your own real tasks. It runs the
same task against several configurations in isolated workspaces, checks whether each result
meets the acceptance criteria, and reports success, time, tokens and cost side by side with a
recommendation.

## Use Crucible in your repo (plugin)

Crucible is a Claude Code plugin: install it once and benchmark tasks from inside any git repo.
Requires [uv](https://docs.astral.sh/uv/) on your `PATH` (the bundled `crucible` command runs
through it; the first call creates a cached venv under `~/.cache/crucible/`).

```bash
claude plugin marketplace add Jaxsonman/Crucible      # or a local clone path
claude plugin install crucible@crucible
# add --scope local to either command to install for one project only
```

Then, inside your repo (in Claude Code, or headless as `claude -p "/crucible:init"`):

- `/crucible:init`: create `.crucible/` (`tasks/`, `results/`, `configs/`) and git-ignore
  `.crucible/results/`. Safe to run again.
- `/crucible:create-task <spec, ticket or PR>`: write a task in `.crucible/tasks/<id>/` pinned to
  your current commit, with a hidden-test `check.sh` proven to fail on the starting commit.
- `/crucible:run <task-id> [--matrix default | --configs ...] [--repeat N]`: preview the plan,
  ask for confirmation, then run the benchmark in the background (spends usage).
- `/crucible:mine [--limit N] [--local] [--max-lines N]`: mine your merged PRs into verified tasks
  (see below; spends usage only on writing the task text).
- `/crucible:report [task-id]`: compare configurations and explain the recommendation.

Tasks and results live in your repo's `.crucible/`. `crucible` commands find the nearest
`.crucible/` above the current directory (`CRUCIBLE_HOME` overrides). Commit `tasks/` to share
them; delete `results/` from `.crucible/.gitignore` to share results too. Defaults for pricing and
configs are bundled; `.crucible/pricing.yaml` and `.crucible/configs/*.yaml` override them.

Every attempt is capped by `--max-budget-usd` (else the task's `max_budget_usd`, else 5.0 per
attempt; judge runs add cost, so pass a lower cap for cheap trials). `--dry-run` prints the
effective cap. Only a real `crucible run` spends money, and the slash commands never start one
without you asking.

Safety: as in standalone use, every attempt runs `claude` with `--permission-mode
bypassPermissions`, so the agent can run any shell command as you. Only benchmark tasks you
trust (see [SECURITY.md](SECURITY.md)). A real end-to-end run is logged in `docs/plugin-e2e.md`.

### Mine tasks from your PRs

`/crucible:mine` turns merged PRs into tasks whose hidden tests are the PR's own tests, proven to
fail on the PR's base commit and pass on its merge commit. The `crucible` CLI does every
deterministic step; the skill only writes each task's What/Why/criteria (describing behaviour,
never the fix) and picks its kind. The first run writes `.crucible/mine.yaml` with auto-detected
test suites and asks you to confirm it:

```yaml
suites:
  - name: backend
    test_paths: ["backend/**/tests/**", "backend/**/test_*.py"]
    cwd: backend
    setup: pip install -q -r requirements.txt
    test_command: python manage.py test {tests|module:backend}   # any language: see below
    # regression_command: python manage.py test   # optional: whole suite, runs at check time
exclude_paths: ["**/*.lock", "docs/**", "*.md"]
max_changed_lines: 800
```

`test_command` takes `{tests}` (the PR's changed test files) with optional filters
(`{tests|stem|join:,}`, `{tests|module:src/test/java|prefix:--tests }`, `{tests|dir|unique}`) or no
placeholder to run the whole suite. A suite may also set `runnable` (globs of the test files to
pass to the runner), `timeout` / `setup_timeout`, and wrapper scripts in `.crucible/scripts/`
(available as `$CRUCIBLE_SCRIPTS`; the changed files arrive in `$CRUCIBLE_TEST_FILES`). Details in
`docs/design.md`.

**Works in any language.** Three layers, each proven by running the repo's own tests:

1. *Detection* guesses the suites from marker files using data-file presets (`src/crucible/data/presets/`):
   pytest, django, jest, vitest, mocha, playwright, node-test, maven, gradle (Java and Kotlin), go,
   cargo, rspec, minitest, phpunit, dotnet and mix, plus generic `npm test` / `make test`
   fallbacks. Only a first guess.
2. *Discovery*: the skill reads how the repo really runs its tests (CI configs, Makefile or other
   task runner, package scripts, README, compose files), corrects `mine.yaml` and, when a runner
   can't take file lists, writes a wrapper under `.crucible/scripts/`.
3. *Calibration*: `crucible mine calibrate [--json]` runs setup at HEAD and verifies a few recent
   candidates per suite, then reports a status (`ok`, `setup-failed`, `no-tests-ran`,
   `not-selective`, `failing`, `timeout`, `no-candidates`, `mixed`) with log tails and hints naming
   the `mine.yaml` field to change. The skill loops on it (at most 5 rounds) before mining.

A new test runner is one YAML file: see "Adding a preset" in [CONTRIBUTING.md](CONTRIBUTING.md).
Tested recipes for toolchains that are awkward on a host (Go via Docker, Gradle with JDK
toolchains, Maven with `JAVA_HOME`, Ruby with `vendor/bundle`) are in
[docs/mining-recipes.md](docs/mining-recipes.md).

The same steps work by hand (add `--local` to skip `gh`):

```bash
crucible mine [--json] [--show-excluded]          # list candidates (first run writes mine.yaml)
crucible mine calibrate [--candidates 3] [--json] # prove mine.yaml runs the tests; diagnose
crucible mine --verify 12 15 [--timeout 600]      # tests fail at base, pass at merge
crucible mine show 12 --json                      # PR text plus test and non-test diffs
crucible mine scaffold 12 [--task-id s] [--kind bugfix]   # write .crucible/tasks/<slug>/
crucible mine leakcheck .crucible/tasks/<slug>    # flag text that gives away the fix
crucible mine prove .crucible/tasks/<slug>        # check fails at ref, passes at merge
```

Then `/crucible:run <slug> --matrix default --repeat 3`. A smoke run is logged in
`docs/mine-e2e.md`.

The sections below describe the standalone CLI (cloning this repo).

## Install

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/Jaxsonman/Crucible && cd Crucible
uv sync
uv run crucible --help
```

Run `crucible` from inside the repo (the Crucible home is the nearest ancestor directory that
contains `pricing.yaml`, or `$CRUCIBLE_HOME`).

## Auth

Crucible drives the `claude` CLI (Claude Code) in headless mode.

- `claude` must be on your `PATH`, or set `CRUCIBLE_CLAUDE_BIN` to its full path.
- Default isolation (`--safe-mode`) uses your normal Claude Code login: run `claude` once
  interactively and log in.
- `CRUCIBLE_ISOLATION=bare` switches to the stricter `--bare` mode, which never reads your
  login and needs `ANTHROPIC_API_KEY` set instead.

Environment variables used: `CRUCIBLE_CLAUDE_BIN`, `CRUCIBLE_ISOLATION`, `ANTHROPIC_API_KEY`
(plus `CRUCIBLE_HOME` and `CRUCIBLE_WORK_DIR` to relocate the home and the workspaces).
Set values in your shell; never put them in files in this repo. Secrets are redacted from
anything written under `results/`.

## Safety warning

Every attempt runs `claude` with `--permission-mode bypassPermissions`, so the agent can run
any shell command as you. Each attempt gets its own workspace copy, but that isolates the files
Crucible tracks, not your machine. Only benchmark tasks you trust, ideally inside a VM or
container. See [SECURITY.md](SECURITY.md) for the full security model and how to report
vulnerabilities.

## Write a task

```bash
uv run crucible task new my-task      # scaffolds tasks/my-task/task.md
uv run crucible task validate tasks/my-task
```

A task is `tasks/<id>/task.md`: front matter (`id`, `kind`, `repo`, `check`, ...) plus
`## What`, `## Why` and `## Acceptance criteria` sections. `repo: fixture` uses
`tasks/<id>/fixture/` as the starting repo; otherwise give a git repo path and `ref`. The
optional `check` script runs in the finished workspace and its exit code is the pass/fail
verdict. The same document is given unchanged to every configuration. See
`tasks/example-fizzbuzz/` for a complete example and `docs/design.md` for the format.

### From a ticket, spec or PR

Open Claude Code in this repo and ask for a task, e.g. "create a Crucible task from GitHub
issue #123 and its PR in ~/code/myapp". The project skill `skills/create-task/` writes
`tasks/<id>/` against your repo at a pinned commit, puts the tests the agent must not see in
`hidden_tests/`, and checks that `check.sh` fails on the starting commit (and passes on the
real fix, if there is one) before handing you the run command. If your Claude Code has a
tracker connected (GitHub, Jira, ...), it reads the ticket itself; otherwise paste the text.

## Run

```bash
# preview the plan without calling claude
uv run crucible run tasks/example-fizzbuzz --configs sonnet:low,sonnet:medium --repeat 1 \
  --judge --max-budget-usd 1 --dry-run

# run for real
uv run crucible run tasks/example-fizzbuzz --configs sonnet:low,sonnet:medium --repeat 1 \
  --judge --max-budget-usd 1
```

`--configs` is a comma-separated `model:effort` list (or use `--matrix <name>` for
`configs/<name>.yaml`; `uv run crucible configs` lists them). `--repeat N` runs N attempts per
config. `--max-budget-usd` hard-caps each attempt; an attempt that exceeds it ends with
`error_max_budget_usd`. Results are appended under `results/<task>/<batch>/<config>-<n>/`
and never overwritten, so re-running adds a new batch.

## Judge

`--judge` asks a second Claude run (default `sonnet:medium`, change with `--judge-config`) to
grade each attempt's diff against the task's acceptance criteria. Judge cost is recorded
separately from the attempt's cost. To judge attempts that were recorded without it:

```bash
uv run crucible judge example-fizzbuzz [--batch <batch-id>]
```

An attempt's `success` is false if it did not complete (error or timeout); otherwise it is the
check's verdict when the check produced one, else the judge's, and unknown if neither did. When
a check exists the judge is advisory: the report counts the attempts where the two disagree in
its `Judge≠check` column.

## Report

```bash
uv run crucible report example-fizzbuzz   # one task: every config side by side
uv run crucible report                    # across tasks, by task kind, with a recommendation
```

Options: `--batch`, `--json`, and for the cross-task report `--kind`, `--min-attempts`
(default 3) and `--threshold` (default 0.8). With fewer than `--min-attempts` attempts per
config the report says "no config meets the bar" and lists the best candidates instead.
Reports re-price every attempt from the recorded tokens and the current `pricing.yaml`.

Reading the table: `Success rate` shows a 95% Wilson interval (`100% (44–100%)` for 3 of 3),
`Stopped` counts attempts ended by a timeout, turn cap or budget cap, or whose check was killed
by `--check-timeout` (default 300 s; a whole-suite `regression_command` must finish within it, or
raise the timeout); stopped attempts are still failures. `Judge≠check` counts judge/check
disagreements. A warning follows the table when one config
label pooled several model versions. The recommendation ranks configs that clear the bar by
cost per success (failed attempts included). The cross-task report compares configs only on
the tasks every config attempted and says so (`Compared on N of M tasks attempted by every
config`); when there is no such task it prints `Recommendation: none; no task was attempted by
every config` instead of a recommendation.

### Computed vs reported cost

Each record keeps `cost_usd.computed` (tokens x `pricing.yaml`) and `cost_usd.reported` (what
`claude` printed). `claude` writes prompt-cache entries with a 1-hour lifetime, billed at 2x
the input price, so `pricing.yaml`'s `cache_write` is the 1-hour rate. With it, computed cost
matches the reported cost exactly on the first real run (it was 22% low at the 5-minute
rate). Reports always re-price from tokens and the current `pricing.yaml`, so all configs are
priced on the same basis and a price change applies to every past run.

## Tests

```bash
uv run pytest
uv run ruff check .
```

## Security

Crucible runs a coding agent with `--permission-mode bypassPermissions` and executes task check
scripts and mined repos' setup commands as you. Treat untrusted tasks and repositories as
untrusted code. Read [SECURITY.md](SECURITY.md); report vulnerabilities privately as described
there.

## Contributing

Issues and pull requests are welcome; see [CONTRIBUTING.md](CONTRIBUTING.md) and the
[Code of Conduct](CODE_OF_CONDUCT.md). Licensed under the [MIT License](LICENSE).
