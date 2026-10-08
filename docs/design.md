# Crucible V1 design

Shared decisions the codebase relies on. Change it deliberately: a pull request that alters one of
these decisions should update this file in the same change and explain why.

## Stack

- Python 3.12+, managed with `uv`. Package at `src/crucible/`, CLI entry point `crucible`
  (`uv run crucible ...`).
- Dependencies: `pyyaml` only at runtime. `pytest` and `ruff` for dev.
- CLI built on stdlib `argparse`. No network calls except through the `claude` CLI.

## Repo layout

```
src/crucible/        package
tests/               pytest suite
tasks/<task-id>/     task.md, optional check.sh, optional fixture/
configs/             named config matrices (YAML)
pricing.yaml         per-model token prices
results/             run records (committed; append-only)
docs/                design.md, plugin.md, claude-headless.md, end-to-end logs
```

### Locating the Crucible home

All of `tasks/`, `configs/`, `pricing.yaml` and `results/` are resolved relative to the
**Crucible home**: `$CRUCIBLE_HOME` if set, else the nearest ancestor of the cwd containing
`pricing.yaml`.

Workspaces live **outside** the home, under `$CRUCIBLE_WORK_DIR` if set, else
`<system temp>/crucible-work/`, so an agent exploring `..` from its workspace can't stumble on
hidden check scripts or other configs' results. This is not a sandbox: with
`bypassPermissions` an agent could still read any absolute path. Full sandboxing is out of
scope for V1. One module (`src/crucible/paths.py`) owns this. **Tests always
set `CRUCIBLE_HOME` to a pytest `tmp_path` copy and never write into the real `tasks/`,
`results/` or `.crucible/`.**

## Vocabulary

- **Task**: one unit of work, defined in `tasks/<task-id>/task.md`.
- **Config**: a model + effort level, written `model:effort`, e.g. `opus:high`, `sonnet:medium`.
- **Batch**: one `crucible run` invocation. Id: `YYYYMMDDTHHMMSSZ-<4 hex>` (UTC).
- **Attempt**: one config running the task once inside a batch. `--repeat N` gives N attempts per config.

## Task document

```markdown
---
id: example-fizzbuzz          # must equal the directory name
kind: feature                 # free-form tag used to group results (feature, bugfix, refactor, ...)
repo: fixture                 # "fixture" = tasks/<id>/fixture/, else a path to a git repo
ref: main                     # git ref; required when repo is not "fixture"
check: ./check.sh             # optional; run from the workspace root, relative to the task dir
timeout_minutes: 30           # optional, default 30
max_turns: 100                # optional, default 100
---

## What
...
## Why
...
## Acceptance criteria
- ...
```

The prompt given to every config is a fixed preamble (`src/crucible/prompt.py`) plus the
What, Why and Acceptance criteria sections, verbatim. The prompt text and its SHA-256 are
stored in every attempt record so identical input can be proven.

## Results layout

```
results/<task-id>/<batch-id>/<config-slug>-<n>/
  record.json        attempt record (schema below)
  output.json        raw JSON printed by claude (secrets never present: no env dumped)
  diff.patch         workspace changes vs. baseline commit
  check.log          check command output (if any)
  judge.json         judge verdict (if judged)
```

`config-slug` is `model-effort`, e.g. `opus-high`; `n` is the 1-based repeat index.
Nothing under `results/` is ever modified or deleted by Crucible after it is written, with one
exception: `crucible judge` may add `judge.json` and update the `judge` and `success` fields
of `record.json` for an attempt that has no `judge.json` yet.

Every text file written under `results/` passes through `redact()` first: any value of an
environment variable whose name contains `KEY`, `TOKEN`, `SECRET` or `PASSWORD` (and is at
least 8 characters) is replaced with `[REDACTED:<NAME>]`.

## Attempt record (`record.json`, schema_version 1)

```json
{
  "schema_version": 1,
  "task_id": "example-fizzbuzz",
  "task_kind": "feature",
  "batch_id": "20261001T143000Z-a1b2",
  "attempt": 1,
  "config": {"model": "sonnet", "effort": "medium"},
  "resolved_model": "claude-sonnet-5-5",
  "prompt_sha256": "...",
  "baseline_sha": "...",
  "started_at": "2026-10-01T14:30:00Z",
  "wall_seconds": 312.4,
  "status": "completed | error | timeout",
  "error": null,
  "num_turns": 41,
  "session_id": "...",
  "subtype": "success",
  "reported_duration_ms": 305000,
  "tokens": {"input": 0, "output": 0, "cache_creation": 0, "cache_read": 0},
  "tokens_by_model": {"claude-sonnet-5-5": {"input": 0, "output": 0, "cache_creation": 0, "cache_read": 0}},
  "cost_usd": {"computed": 0.0, "reported": 0.0},
  "check": {"ran": true, "passed": true, "exit_code": 0},
  "judge": {"ran": true, "passed": true, "criteria_passed": 4, "criteria_total": 4, "judge_config": "sonnet:medium", "judge_cost_usd": 0.0},
  "success": true
}
```

`success` (`records.compute_success`), evaluated in this order:
1. `status` is not `completed` → `false`.
2. Neither check nor judge produced a verdict (`check.ran` false and `judge.passed` null or
   judge not run) → `null` (unknown), never `true`.
3. Otherwise `true` only if every verdict produced is a pass.

## Tokens and cost

`tokens` is the sum of `modelUsage` across every model the run used (see
`docs/claude-headless.md`), so subagent work is counted. `tokens_by_model` keeps the per-model
breakdown in the record. `cost_usd.computed` is the sum over models of tokens × that model's
prices in `pricing.yaml`, stored as the price at run time (or `null` if a model could not be
priced; the attempt is still recorded).

**Reports never trust the stored `computed` value.** They re-price every attempt from
`tokens_by_model` and the current `pricing.yaml`, so editing prices re-prices all history
without touching `results/`.

Model matching (`pricing.match_model(model_id)`): exact ID, else the `pricing.yaml` entry
whose ID is the longest prefix of `model_id` (handles date or variant suffixes), else
unpriced. `cost_usd.reported` is whatever
the `claude` CLI reported, kept for cross-checking.

## Mining

PR mining turns merged PRs into tasks.

- Code lives in the `src/crucible/mine/` package: `config.py` (mine.yaml, stack detection, path
  globs), `candidates.py` (model, filters, local-git source), `gh.py`, `verify.py`,
  `scaffold.py`, `leak.py`.
- A candidate's **base** is the first parent of its merge commit (works for merge, squash and
  single-commit rebase merges). Its **merge** is the merge commit SHA. Both are full SHAs.
- A file is a **test file** if it matches a suite's `test_paths` globs; everything else is
  non-test. Globs: `**` crosses directories (and may match none), `*` stays inside one path
  segment, and a pattern without `/` (like `*.md`) matches the file name at any depth
  (`config.path_matches`). A directory pattern, meaning a trailing `/` or no wildcard at all,
  also matches every file under that directory: `src/utils` and `src/utils/` are anchored at the
  repo root, while a name without `/` (`vendor`, `vendor/`) matches a directory (or file) of that
  name at any depth.
- Mining state lives under `.crucible/mine/` and is git-ignored (`crucible init` adds `mine/`
  to `.crucible/.gitignore`; an existing home gets it on the next `crucible mine`).
- Task front matter gains an optional `source:` (PR URL, or `commit:<merge sha>` without `gh`).
- Mining never writes outside `.crucible/` and never modifies refs, branches or PRs.

### `.crucible/mine.yaml`

```yaml
suites:                         # one per test stack; a repo may have several
  - name: backend
    test_paths: ["backend/**/tests/**", "backend/**/test_*.py"]
    cwd: backend                # where setup and test commands run, relative to repo root
    setup: pip install -q -r requirements.txt   # optional
    test_command: python manage.py test {modules}
    module_root: backend        # optional; {modules} = {tests|module:backend} (legacy form)
    env: {DJANGO_SECRET_KEY: dummy-not-secret}   # names + dummy values only
  - name: frontend
    test_paths: ["frontend/src/**/*.test.*", "frontend/src/**/__tests__/**"]
    cwd: frontend
    setup: npm ci --silent
    test_command: npx vitest run {tests}   # {tests}: space-separated, relative to cwd
    runnable: ["**/*.test.*"]   # optional: which changed test files are passed to the runner
    timeout: 600                # optional: seconds for the test step (default 600)
    setup_timeout: 1800         # optional: seconds for the setup step (default 1800)
exclude_paths: ["**/package-lock.json", "**/*.lock", "**/migrations/**", "docs/**", "*.md"]
max_changed_lines: 800
```

Loaded and validated by `mine/config.py` (`load_config`, raising `MineConfigError` with one
message per problem, each naming its field, e.g. `suites[0].test_command: required`).
`test_command` is parsed with the placeholder grammar below at load time: a malformed
placeholder, an unknown name or filter is an error naming `suites[i].test_command`, and
`{modules}` needs `module_root`. A command with no placeholder is allowed and runs the whole
suite. `runnable` (optional, a non-empty list of globs, matched like `test_paths`) says which
of a suite's changed test files are passed to the runner; omitted, the built-in rule applies
(see Verification). `timeout` (test step, default 600) and `setup_timeout` (setup step,
default 1800) are positive numbers of seconds. `cwd` and `module_root` must stay inside the repo. `env`
values longer than 7 characters whose names contain `KEY`, `TOKEN`, `SECRET`, `PASSWORD`,
`PASSWD`, `PRIVATE` or `CREDENTIAL` (or a whole `_`-separated word `PAT`, so `PATH` is fine) must
start with `dummy`; and any value that is a URL with embedded credentials
(`scheme://user:pass@host`) is rejected unless the password starts with `dummy` (guard against
pasting real secrets). Unknown fields are errors (non-string keys are reported too).
`exclude_paths` and `max_changed_lines` default to the values above when omitted.

`detect_suites(repo_root)` looks in the repo root and one level down for pytest (`pytest.ini`,
`pyproject.toml [tool.pytest*]`, or `conftest.py` without `manage.py`; a directory holding only
a `conftest.py` is not its own project when the repo root has a python marker (`pytest.ini`,
`pyproject.toml`, `setup.cfg`, `setup.py`, `requirements*.txt`, `manage.py`): the root becomes the
pytest suite instead), Django (`manage.py`),
Vitest and Jest (`package.json` dependencies), and returns suites with sensible commands. The
first `crucible mine` writes a commented `mine.yaml` saying it was auto-detected and must be
checked (or, with nothing detected, a commented template with empty `suites`), prints the path
and exits 0 without overwriting an existing file; an invalid file prints its errors and exits 1.

#### Placeholders

```
placeholder := "{" source ( "|" filter )* "}"
source      := "tests" | "modules"
filter      := name | name ":" argument        # argument runs to the next unescaped "|" or "}"
```

`{tests}` is the suite's runnable changed test files, repo-relative paths made relative to the
suite `cwd`, in diff order. Filters apply left to right to every item:

| filter | effect |
| --- | --- |
| `module:<root>` | path relative to `<root>` (repo-relative), final extension stripped, `/` turned into `.`; a file outside `<root>` rejects the candidate (`test outside module_root`) |
| `stem` | file name without its final extension |
| `dir` | parent directory (`.` for none) |
| `unique` | drop duplicates, keeping the first |
| `prefix:<s>` / `suffix:<s>` | text put before / after each item (stacked outward) |
| `join:<sep>` | join the items with `<sep>` into one word; must be the last filter |

`{modules}` is an alias for `{tests|module:<module_root>}` that keeps its original behaviour
(only a `.py` extension is stripped) and accepts more filters (`{modules|unique}`). Quoting:
without `join` (or with `join: `, a single space) every item is its own shell word, its value
quoted with `shlex.quote`; `prefix`/`suffix` text is the configuration's own and stays
unquoted, so `{tests|module:src/test/java|prefix:--tests }` gives `--tests com.A --tests com.B`.
With any other separator the joined string, prefixes and suffixes included, is quoted as one
word (`-Dtest={tests|stem|join:,}` gives `-Dtest=A,B`). `prefix`/`suffix` wrap the finished
item, so `module`, `stem` and `dir` cannot follow them. File names with spaces, quotes,
`$(...)`, backticks, `;` or newlines reach the program verbatim.

Only braces shaped like a placeholder are parsed: `{` not directly preceded by `$`, then an
identifier, then `|` or `}`. `${VAR}`, `${VAR:-x}`, `{}`, `{a,b}`, `{1..3}` and
`awk '{print $1}'` pass through untouched. An identifier other than `tests`/`modules` in that
position (`{foo}`, `{Tests}`) is an error, as is an unclosed `{tests`; for a literal such brace
group use a wrapper script. Inside an argument `\|`, `\}` and `\\` are literals and `\n`/`\t`
a newline/tab. Examples: pytest `{tests}`; Django `{tests|module:backend}`; Maven
`-Dtest={tests|stem|join:,}`; Gradle `--tests` per class as above; Go
`{tests|dir|unique|prefix:./}`.

Every setup and test step (verify, and `check.sh`) also gets `CRUCIBLE_TEST_FILES` (the
suite's runnable files, repo-relative, newline-separated) and `CRUCIBLE_TEST_FILES_CWD` (the
same relative to the suite `cwd`) for wrapper scripts.

#### Wrapper scripts

Files in `<home>/scripts/` (`.crucible/scripts/`) are made available at `$CRUCIBLE_SCRIPTS`:
`verify` exports the host directory; `scaffold` copies the whole tree to `<task>/scripts/`
(modes and symlinks kept) and `check.sh` exports `CRUCIBLE_SCRIPTS="$TASK_DIR/scripts"`, so
`test_command: bash "$CRUCIBLE_SCRIPTS/run-tests.sh"` behaves the same in verify, `prove` and
`crucible run`. Without a scripts dir nothing is copied or exported. The workspace never
contains `.crucible/`.

### Candidates (`mine/candidates.py`)

`Candidate` carries `id`, `title`, `base`, `merge`, `test_files`, `other_files` (each a
`FileChange`: `path`, `status` added|modified|deleted, `added`, `deleted`), `lines_changed`
(added + deleted over every changed file), `counted_lines` (the same, skipping non-test files
that match `exclude_paths`, so lockfiles, docs and generated files do not count; test files
always count; this is what `max_changed_lines`/`--max-lines` is compared with, and the
"N lines changed (max M)" exclusion reason reports it), `suites` (names of the suites owning the test files, config order; each test file is owned by the first suite whose `test_paths` match it; JSON also keeps `suite`, the first entry), `suggested_kind`, `source`, and optional `url`, `labels`, `branch`, `issues`, `body`.
File lists come from `git diff --name-status/--numstat -z --no-renames base merge` (a rename is a
delete plus an add; binary files count 0 lines). Sources build candidates with `make_candidate`;
`filter_candidates` applies the exclusions. `crucible mine --json` prints
`{"candidates": [...], "excluded": [...]}` (`excluded` filled only with `--show-excluded`; each
entry is a candidate plus `reason`). A root commit is listed as excluded ("root commit").

### GitHub source (`mine/gh.py`)

With a remote whose URL is github.com (any remote name; `origin` preferred) and a working `gh`,
`crucible mine` lists merged PRs (`gh pr list --repo owner/name --state merged --json ...`;
unsupported fields are dropped and retried). The merge commit must exist locally
(`git cat-file -e`; mining never fetches): otherwise excluded "merge commit not fetched; run
`git fetch`". base = first parent of the merge commit; if its `git diff --name-only` differs
from the PR's file list the PR is excluded "rebase merge: diff doesn't match PR". `gh` cuts a
PR's `files` at 100 entries, so a list of 100 or more only has to be contained in the local diff
(not equal to it). PRs are ordered by `mergedAt`, newest first (those without it keep `gh`
order, last). `--branch <name>` keeps only PRs whose `baseRefName` is `<name>` (no filter when
omitted; the local source walks `--branch`, default HEAD). File split and
line counts always come from the local diff; labels, branch, body and url from `gh`; `source` is
the PR URL. Linked issue title and body (`gh issue view`) are fetched only for candidates that
pass the filters; an issue that cannot be fetched (deleted, transferred) stays as a bare
reference with an `error` note and the rest of the `gh` result is kept. A candidate whose merge
commit equals the `merge` in a task's `.mine.json` counts as already mined, whichever source
scaffolded that task. No GitHub remote, `gh` missing, unauthenticated or any other `gh` failure
prints one line on stderr (`gh source unavailable (<why>); using local git history`) and uses the
local source; `--local` forces it.

`crucible mine show <id> [--json]` prints one candidate (listed or excluded, either source) plus
`test_diff` and `non_test_diff` (the diffs base->merge of its test and non-test files) and
`excluded` (reason or null). `--max-lines` must be at least 1. `mine` takes an optional subcommand (`show`, `scaffold`,
`prove`, `leakcheck`) and shares `--branch --limit --max-lines --local --json` with it.

### Verification (`mine/verify.py`)

`crucible mine --verify <id>... [--force] [--keep] [--timeout S]` proves each candidate's tests
fail at base and pass at merge using the repo's own tests (no `claude`). `--verify` with no ids
is an error (exit 2); ids not in the candidate list, or excluded, print why on stderr and are
skipped (exit 1 after the others ran). A rejection is a normal outcome (exit 0).

- **Runnable test files.** Of the candidate's changed test files, only non-deleted ones that are
  *runnable* are passed to the test command. A suite with `runnable` globs uses them (each file
  follows the rule of the suite owning it). Otherwise the built-in rule applies: file name
  `test_*.py`, `*_test.py`, `tests.py`, `*.test.<ext>` or `*.spec.<ext>`, or any file under a
  `__tests__` directory (`config.default_is_runnable`). Other files under `test_paths`
  (conftest, fixtures, helpers) are overlaid but not passed. No runnable file means rejected
  `no runnable tests`, also for a command without placeholders (which then runs the whole suite).
- **Several suites.** A candidate whose test files fall in more than one suite is a normal
  candidate (`suites: [a, b]`; the listing shows `a+b`). Per side the tree is exported and all
  test files overlaid once; then for each suite in order: setup (failure rejects as
  `setup failed at <side> (<suite>)`), then its test command with only that suite's runnable
  files (a suite with none is skipped; none at all: `no runnable tests`). Base is verified if
  at least one suite fails (all pass: `passes at base`); merge needs every suite to pass
  (`fails at merge (<suite>)`). `{modules}`/`module_root` apply per suite
  (`test outside module_root (<suite>)`). Single-suite results keep the plain reasons without
  the suffix.
- **Steps.** Export base (`workspace.export_tree`, which drops the top-level `.crucible/`) into a
  fresh dir under `paths.work_dir()/mine/`, overlay the PR's test files at their merge versions
  (added and modified are written with their git mode, so `100755` stays executable and `120000`
  becomes a symlink; deleted are removed; a write whose parent directory is a symlink, or that
  resolves outside the workspace, is refused and rejects with `unsafe path (symlink)`), run `setup` (if any) in the suite `cwd`,
  then the test command, which must exit non-zero. Then export merge to another fresh dir, setup,
  same command, which must exit zero. Setup and tests run via `bash -c` in their own process
  group, with `os.environ`, the suite `env` and Crucible's own variables (`CRUCIBLE_TEST_FILES`,
  `CRUCIBLE_TEST_FILES_CWD`, and `CRUCIBLE_SCRIPTS` when the home has a scripts dir). A setup
  step is killed after the suite's `setup_timeout` (default 1800 s), a test step after its
  `timeout` (default 600 s) or, when given, `--timeout S`, which overrides the test step of
  every suite. Dirs are removed afterwards unless `--keep`, which prints their paths.
- **Placeholders.** See "Placeholders" above; one builder (`verify.build_command`) fills them
  for verify, `check.sh` and therefore `prove`. A suite with `module_root`, or a command using a
  `module:` filter, rejects a candidate whose runnable test file lies outside the root
  (`test outside module_root`, deterministic, nothing is run).
- **Outcomes.** `verified`, or `rejected` with `passes at base`, `fails at merge`,
  `setup failed at base|merge` (setup failing is never counted as "fails before"), `timeout`,
  `test outside module_root` or `no runnable tests`.
- **Cache.** `<home>/mine/verify/<merge sha>.json` (outcome, reason, suite, `suite_hash`,
  command, timings, log paths) with the step logs beside it (`<merge>.<base|merge>-<setup|test>.log`; with several suites `<merge>.<side>-<suite>-<setup|test>.log`).
  An entry is reused only while the involved suites' settings are unchanged (one suite: its
  `settings_hash()`, so older entries stay valid; several: a hash over every suite's hash). The
  hash covers name, `test_paths`, `cwd`, `setup`, `test_command`, `module_root`, `env` and
  `runnable`, plus a digest of the scripts dir (names, exec bits, contents) when there is one.
  `timeout` and `setup_timeout` are deliberately left out: a run that hit a limit is never
  cached, and a result obtained within a limit stays valid under a longer one. `--force` reruns. `timeout` and `setup failed at ...` are transient and never cached (the logs
  are still written), so a rerun with a longer `--timeout` or fixed setup really runs.
- The listing gets a `verified` column (`yes`, `no: <reason>`, blank) and a `verified` JSON field
  (the same string, or null) read from the cache.

### Scaffold and prove (`mine/scaffold.py`)

`crucible mine scaffold <id> [--task-id <slug>] [--kind <kind>]` (shares `--branch --limit
--max-lines --local --json` with `show`) writes `<home>/tasks/<slug>/` for a candidate whose
cached verification is `verified`. Otherwise it exits 1 saying why (excluded with reason, not in
the list, not verified, or rejected with the reason); an existing slug exits 1 and is not touched.
An already-scaffolded candidate is excluded as "already mined", so it cannot be scaffolded twice.

- **Slug.** Default `pr-<n>-<kebab title>`, or `commit-<sha8>-<kebab title>` when the candidate
  has no PR number (a `commit:` source whose id is the short merge sha, so an all-digit sha still
  gets `commit-`), cut to 64 chars and the task id rules.
- **`task.md`.** Rendered by `task.repo_task_md` (the template `crucible task new` uses in repo
  homes; `repo` from `task.repo_relative`, here the relative path from the task dir to the repo
  root, so it also works when the home is not inside the repo). Front matter: `id`, `kind`
  (given or suggested), `repo`, `ref` = base SHA, `source` (PR URL or `commit:<merge>`),
  `timeout_minutes: 30`, `max_turns: 100`, `check: ./check.sh`, and `_(...)_` placeholders in the
  three sections, so an unedited task fails `crucible task validate`.
- **`hidden_tests/`.** The PR's test files (all changed test files, runnable or not) at their
  merge versions at repo-relative paths (git modes kept: exec bit, symlinks; `check.sh`'s `tar`
  copy preserves both), plus `hidden_tests/DELETED` (one path per line) for test
  files the PR removed.
- **Several suites.** `check.sh` installs once, then runs each suite's setup and test command
  in its own subshell (own `env` and `cwd`), printing `== suite <name> ==` before each. A setup
  failure exits 97 immediately; test failures are collected so every suite runs, and the
  script exits with the first failing code. `prove` is unchanged.
- **`check.sh`** (mode 755), generated from the suite, run by `checker.run_check` with the
  workspace as cwd and `CRUCIBLE_TASK_DIR` set (falls back to the script's own directory). It
  copies `hidden_tests/` (minus `DELETED`) over the workspace with `tar`, removes the `DELETED`
  paths, exports the suite `env`, `CRUCIBLE_TEST_FILES` and `CRUCIBLE_TEST_FILES_CWD` (and
  `CRUCIBLE_SCRIPTS` when `<task>/scripts/` was copied), `cd`s to the suite `cwd`, runs `setup`
  and then exec's the test command, each via `bash -c '<quoted command>'`, with the
  placeholders filled by `verify.build_command` from `verify.runnable_files`, exactly what
  `--verify` ran. A failing
  setup prints `crucible-check: setup failed` and exits 97 (so `prove` never counts a broken
  install as "fails before"); a failed copy exits 98.
- **`.mine.json`** in the task dir (`id`, `base`, `merge`, `suite`, `suites`, `source`,
  `check_timeout_s`) records the merge SHA deterministically; `check_timeout_s` is the sum of
  the suites' `setup_timeout` and `timeout`, the default for `prove`'s per-run limit. The workspace never contains `.crucible/`, so a hidden file in the task
  dir does not leak. `task.md` only needs `source`; `mined_tasks` reads only `task.md`.

`crucible mine prove <task-dir> [--timeout S]` (default: `check_timeout_s` from `.mine.json`,
else 600; one limit covers setup and tests of a run) loads the finished task (so placeholders must be
filled), exports `ref` with `workspace.export_tree` into a fresh dir under `paths.work_dir()/mine/`
and runs the task's own `check.sh` through `checker.run_check`: it must fail. It then does the
same on the merge commit, where it must pass. The merge SHA comes from `.mine.json`, else a
`commit:` source, else (tasks without `.mine.json` and a PR-URL source) the verify cache entry
whose `id` is the PR number in the URL and whose `base` equals `ref`. Prints `proven: <id>` and
exits 0; otherwise prints `not proven: <id>: <reason>` and the last 40 log lines on stderr and
exits 1. Reasons: `passes at base`, `fails at merge`, `setup failed at base|merge`,
`check did not finish at base` (timeout). `task.py` accepts an optional `source:` string and
exposes it as `Task.source`.

### Leak check (`mine/leak.py`)

`crucible mine leakcheck <task-dir> [--json]` checks the task's What, Why and each Acceptance
criterion against the non-test diff base->merge (base = `ref`, merge from `.mine.json`/`source`;
test files per all `mine.yaml` suites and `exclude_paths` files are left out). Exit 0 prints
`clean: <id>`; exit 1 lists flags on stderr. `--json` prints a list of `{kind, span, section,
reason}` (exit code unchanged). Tuned to avoid false positives:

- **quoted-code**: an added line, whitespace-normalised, of at least 20 characters (also tried
  without a trailing `:;,{`), with some alphanumerics, not a trivial `return`/`raise`/`pass`...
  line, not already present in the base version of its file, and not also an added line of the
  PR's test diff, appears as a substring of the whitespace-normalised text (backticks, inline or
  fence, are stripped from the text first, so they do not hide it).
- **new-identifier**: tokens that look like code (snake_case, camelCase/PascalCase with an inner
  capital, parts of `dotted.names` that are not file names, `name()`, anything in backticks) that
  occur in the added lines and are absent from the base tree (`git grep -w -F -q <token> <base>`,
  only run for tokens in both the text and the added lines). Keywords and literals are ignored.
  Identifiers are Unicode-aware (`naïve_fn`). A token that appears on an added line of the PR's
  test diff is interface, not a leak (hidden tests call it, so the task must name it). Plain
  words (no underscore or inner capital) must occur in the code portion of an added line, with
  string literals, comments and docstrings blanked out; code-shaped tokens match the whole
  line. Which text counts as a comment depends on the file extension of the added line's file
  (`leak.SYNTAX_BY_EXT`): `#` for py/rb/sh/yaml/toml/pl/r; `//` and `/* */` for java, kt,
  scala, go, rs, js, ts, cs, swift, dart, c/cpp and similar (`#` is not a comment there, nor is
  `--`, so `i--` stays code); `--` for sql (plus `/* */`), hs, lua, elm (in hs/elm `-->` is an
  operator); `<!-- -->` for html/xml/vue/jsp/svelte; php takes `//`, `#` and `/* */`; css only
  `/* */`; an unknown extension keeps the permissive union of `#`, `//` and `/* */`. String
  and identifier handling is the same for every language. URL-path-like backticked text (`/api/x/`, `POST /api/x`) is one token, flagged only if
  the exact path is on an added non-test line, absent from the base tree and not in the test diff.
- **file-instruction**: a sentence naming a changed non-test file (path or basename) together
  with an edit verb (add, change, modify, update, edit, rewrite, replace, remove, create,
  implement, patch) or `in <file>`.

`crucible mine prove` runs it after the check proves out and exits 1 (`not proven: <id>: task
text leaks the solution`) on flags unless `--allow-leaks`, which still prints them.
