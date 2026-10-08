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
check_timeout_minutes: 45     # optional positive integer; how long `crucible run` lets `check` take
---

## What
...
## Why
...
## Acceptance criteria
- ...
```

`crucible run` kills a check after `--check-timeout` seconds if given, else after the task's
`check_timeout_minutes`, else 300 s. Mined tasks get `check_timeout_minutes` from `scaffold`
(the suites' `setup_timeout` plus `timeout`, rounded up to whole minutes), so a slow build is
not killed by the default; `crucible mine prove` uses it too unless `--timeout` is given.
`crucible run <task>` (and `task validate`, `mine prove`, `mine leakcheck`) take a task directory;
a bare task id that is not an existing path resolves to `<home>/tasks/<id>`.

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

- Code lives in the `src/crucible/mine/` package: `config.py` (mine.yaml, path globs), `presets.py`
  and `detect.py` (framework presets, stack detection), `candidates.py` (model, filters, local-git source), `gh.py`, `verify.py`,
  `calibrate.py` and `diagnostics.py` (setup check, status and hints), `jdk.py` (Java version
  a Maven/Gradle build needs, local JDK lookup), `scaffold.py`, `leak.py`.
- A candidate's **base** is the first parent of its merge commit (works for merge, squash and
  single-commit rebase merges). Its **merge** is the merge commit SHA. Both are full SHAs.
- A file is a **test file** if it matches a suite's `test_paths` globs; everything else is
  non-test. Globs: `**` crosses directories (and may match none), `*` stays inside one path
  segment, and a pattern without `/` (like `*.md`) matches the file name at any depth
  (`config.path_matches`). `{a,b}` is alternation (`*.{test,spec}.{ts,js}`, `{docs,vendor}/**`;
  nesting works; a brace group without a top-level comma, like `{}` or `{x}`, is literal), in
  `test_paths`, `runnable` and `exclude_paths`. A directory pattern, meaning a trailing `/` or no wildcard at all,
  also matches every file under that directory: `src/utils` and `src/utils/` are anchored at the
  repo root, while a name without `/` (`vendor`, `vendor/`) matches a directory (or file) of that
  name at any depth. A pattern starting with `!` excludes: a path matches a list when it matches
  some positive pattern and no `!` pattern (`**/__tests__/**/*.js` with `!**/__snapshots__/**`);
  a list of only `!` patterns matches nothing.
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
(see Verification; `runnable` and `test_paths` accept `!` negations). `timeout` (test step, default 600) and `setup_timeout` (setup step,
default 1800) are positive numbers of seconds. `cwd` and `module_root` must stay inside the repo. `env`
values longer than 7 characters whose names contain `KEY`, `TOKEN`, `SECRET`, `PASSWORD`,
`PASSWD`, `PRIVATE` or `CREDENTIAL` (or a whole `_`-separated word `PAT`, so `PATH` is fine) must
start with `dummy`; and any value that is a URL with embedded credentials
(`scheme://user:pass@host`) is rejected unless the password starts with `dummy` (guard against
pasting real secrets). Unknown fields are errors (non-string keys are reported too).
`exclude_paths` and `max_changed_lines` default to the values above when omitted.

#### Presets and detection

Language knowledge lives in data, not code: `src/crucible/data/presets/<name>.yaml` (one file
per test runner; shipped in the wheel and read with `importlib.resources`, so it works from an
installed plugin). Shipped: pytest, django, jest, vitest, mocha, playwright, node-test, maven,
gradle (Java and Kotlin), go, cargo, rspec, minitest, phpunit, dotnet, mix, and two generic
fallbacks, npm-test and make-test. `mine/presets.py` loads and validates
them (a bad file is an error naming `<file>: <field>`; a test loads every shipped preset).

A preset has `name` (= file name), `markers`, `test_paths`, `test_command`, `notes` (why the
command is right) and optionally `runnable`, `setup`, `module_root`, `env`, `timeout`,
`setup_timeout`, `evidence`, `supersedes`, `fallback`, `project_files`, `variants` and
`notes_when` (`[{when: [globs], note}]`: extra notes written only when a file matching a glob
exists in the suite directory, e.g. rspec's Rails database note for `config/application.rb`):

- `markers`: `{glob, contains?, evidence?, weak?, aggregator?, wins_over?}`. `glob` names files in
  the directory itself (`pom.xml`, `*.sln`, `test/test_helper.rb`; `{a,b}` alternation works);
  `contains` is a regex searched in the file (multi-line, so `^` anchors a line; commented-out
  lines such as `# gem 'rspec'` must be excluded by the regex); a marker's own `evidence` globs
  must also find a file below the directory (pytest: `test_*.py` under `tests/` next to any
  Python project file). A directory matches the preset when any marker matches.
- `{suite_dir}` (in `test_paths`, `runnable`, `module_root`, `test_command`) is the suite's
  directory relative to the repo root, substituted at detection time; for the root it is `.`
  and a following `/` vanishes (`{suite_dir}/src/test/**` becomes `src/test/**`). This is how a
  preset yields correct repo-relative globs and `module:` roots for a suite in `backend/`.
  Inside a `test_command` placeholder argument the directory is escaped (`\`, `|`, `}`).
- `variants`: `[{when: [file globs], ancestors?, setup?, test_command?, module_root?}]`; the first
  whose file exists in the directory overrides those fields (`mvnw`/`gradlew` wrappers, the JS
  lockfiles, `Gemfile` for `bundle exec`, `uv.lock`/`poetry.lock`/`Pipfile.lock`). With
  `ancestors: true` the file is also looked for in the directories above the suite up to the repo
  root (a workspace root); the suite directory is tried first with every variant, then each parent,
  so the nearest lockfile wins. `{lock_cd}` in such a `setup` is `cd <that directory> && ` (empty
  when it is the suite directory) and `{suite_cd}` the `cd` back.
- `setup` is a string or a list of steps, joined with ` && ` at detection time so `mine.yaml`
  holds one plain command. A step is a string, or `{run, when?, contains?, unless?}` (included
  when a `when` file exists, optionally containing `contains`, and no `unless` file exists), or
  `{first: [steps]}` (the first included alternative wins). The Python presets use this to
  create `.venv`, install the requirements files that exist, then the project with a
  `test`/`tests`/`testing`/`dev` extra when one is declared (else plain `-e .`).
- `fallback: true` marks a generic whole-suite preset (`npm-test`, `make-test`): it counts only
  in a directory where no non-fallback preset matched.
- `runnable` and `test_paths` accept `!pattern` negations (see Mining, globs).
- `evidence`: globs (relative to the suite dir, searched recursively) of which one must match
  some file, so a `pom.xml` or `go.mod` without any test source is not a suite.
- `supersedes` / marker `wins_over` / `weak`: see precedence below. `project_files` lists what
  makes a directory a project of the ecosystem. `aggregator`: see below.

`detect_suites(repo_root)` (`mine/detect.py`) scans the repo root and up to three levels below
it, skipping dot directories, `node_modules`, `vendor`, `target`, `build`, `dist`, `out`, `bin`,
`obj`, `deps`, `_build`, virtualenvs (`venv`, `env`, any directory with `pyvenv.cfg`), `testdata`,
`fixtures`, and directory names containing control characters or `*`/`?`. Looking for test files
(`evidence`) skips less: only `node_modules`, `vendor`, `target`, `site-packages`, `__pycache__`,
`venv`, `Pods`, `_build`, dot directories and virtualenvs, so test sources under `build/`, `out/`,
`deps/` or `bin/` count (`target/` is skipped because `cargo package` copies `tests/` there).
It makes one suite per
(directory, preset). Suite names are the directory name (the preset name for the root),
sanitised to `[A-Za-z0-9_.-]`; a clash appends `-<preset>` (then a counter). Suites are listed
deepest directory first.

Ownership of a test file (`candidates.owning_suite`, also used for the verification plan): among
the suites whose `test_paths` match, the deepest `cwd` wins; within that depth, a suite whose
`runnable` rule accepts the file; then config order. So a root Cargo suite's `**/tests/**` does
not take `tests/test_a.py` from a pytest suite in the same directory, and an `e2e/` Playwright
suite owns its specs over a root Jest suite. Files nobody can run (helpers, fixtures) go to the
first of the deepest matching suites.

Precedence in one directory: a strong (`pytest.ini`, `[tool.pytest]`, `[tool:pytest]`, tox
`[pytest]`) pytest marker beats Django, since a Django project with an explicit pytest config
runs pytest (pytest-django); a bare `conftest.py` is a weak marker and Django wins; Vitest
supersedes Jest (a repo listing both is usually mid-migration); Gradle supersedes Maven
(a leftover `pom.xml` beside a build that migrated is likelier than the reverse); RSpec
supersedes Minitest. A weak-only match in a directory without the ecosystem's project files
(a `tests/` holding only `conftest.py`) folds into the nearest enclosing directory that has
them (the python project at the repo root), as it always did. Nothing else is exclusive, so a Django
backend next to a Vitest frontend are two suites; Jest and Mocha in one `package.json` are two.

Multi-module builds: a marker with `aggregator: true` (Maven parent pom containing
`<modules>`, Gradle `settings.gradle[.kts]` with `include`, a Cargo `[workspace]`, a `.sln`,
an Elixir umbrella `apps_path`) means the directory builds everything below it, so same-preset
matches in sub-directories are not separate suites; the one suite at the aggregator runs from
the root and its `test_paths` (`**/src/test/**`) cover every module. A Maven or Gradle child
module is a suite of its own only when no such parent exists.

How each preset selects tests (exact commands and the reasoning are in the preset files):

| preset | selection |
| --- | --- |
| pytest, rspec, minitest, mocha, vitest, playwright, node-test, mix | the changed file paths (`.venv/bin/python -m pytest`, `rspec`, `ruby -Itest ...`, `npx mocha`, `npx vitest run`, `npx playwright test`, `node --test`, `mix test`) |
| jest | `npx jest --runTestsByPath <files>` (literal paths, so `app/[id]/page.test.tsx` works) |
| npm-test, make-test | whole suite (`<pm> test`, `make test`, `just test`); lowest fidelity, refined by calibration or discovery |
| django | dotted module labels for `manage.py test`, from `module_root: {suite_dir}` |
| maven | Surefire `-Dtest=` with comma-joined class simple names plus `-Dsurefire.failIfNoSpecifiedTests=false` |
| gradle | one `--tests \*.<Class>` per changed class, with a temporary init script (`-I`) setting `filter.failOnNoMatchingTests = false` so multi-project builds do not fail sub-projects without matches |
| go | one `go test -count=1 ./<dir>` per package directory (unique) |
| cargo | `cargo test --workspace --test <stem>` per changed `tests/*.rs` file (inline unit tests are not selectable) |
| phpunit | `--filter 'A\|B'`, a class-name regex, because the CLI takes a single path |
| dotnet | `--filter 'FullyQualifiedName~A\|FullyQualifiedName~B'` |

The JS presets (jest, vitest, mocha, playwright, node-test, npm-test) pick the package manager
from the lockfile: `pnpm-lock.yaml` runs `pnpm install --frozen-lockfile` and `pnpm exec <runner>`,
`yarn.lock` `yarn install --frozen-lockfile` (`--immutable` with `.yarnrc.yml`) and `yarn <runner>`,
`bun.lockb`/`bun.lock` `bun install --frozen-lockfile` and `bunx <runner>`, `package-lock.json`
`npm ci` and `npx <runner>`; without a lockfile `npm install --no-package-lock` (the tree gets no
lockfile). The lockfile may sit in an ancestor directory (workspace root): install then runs there
(`cd ../.. && pnpm install ...`). Python presets use a `.venv` in the suite directory, or
`uv run`/`poetry run`/`pipenv run` with a lockfile. pytest is detected from a config file, from
being listed as a dependency (pyproject, requirements, setup.cfg/py, tox, Pipfile) or from
`test_*.py` files under `tests/` or `test/` next to a Python project file. Mocha's `.mocharc`
`spec` entries are merged with the positional files, which degrades selection to whole-suite.

Gradle matches by simple name with a wildcard instead of deriving a fully qualified class name:
that works for Java and Kotlin source sets and multi-project builds without a package root.
`./mvnw`, `./gradlew` replace `mvn`/`gradle` when the wrapper is in the suite directory.

The first `crucible mine` writes a commented `mine.yaml`: a header saying it was auto-detected,
and above every suite a comment naming the preset and marker that produced it, saying it is a
guess to be checked with `crucible mine calibrate`, plus the preset's notes (or, with nothing
detected, a commented template with empty `suites`). It prints the path and exits 0 without
overwriting an existing file; an invalid file prints its errors and exits 1.

**Setup compiles main code only.** `setup` runs before the PR's test files are copied in (see
Verification), so build-tool presets compile just the main sources: Maven `compile`
(`./mvnw ... compile`), Gradle `classes` (`./gradlew classes`), never `test-compile` /
`testClasses` (the PR's tests reference classes the base does not have).

**JDK detection** (`mine/jdk.py`). For `maven` and `gradle` suites detection reads the Java
version the build needs: the pom's compiler plugin `<release>`, `maven.compiler.release`,
`java.version`, `maven.compiler.source`/`target` (`1.8` is 8, `${prop}` is followed); for Gradle
`JavaLanguageVersion.of(N)`, `jvmToolchain(N)`, `languageVersion = N`, `JavaVersion.VERSION_N`,
`sourceCompatibility` in the suite's build files (root, subprojects two levels down, `buildSrc`)
and `jdk = "N"` style keys of `gradle/libs.versions.toml` (the highest wins); else the first
literal `java-version:` of a `.github/workflows` `actions/setup-java` step. The version and its
source file are written as a comment above the suite. If a matching JDK is installed
(`JAVA_HOME_<N>_*` variables, macOS `/usr/libexec/java_home -v N`, `/usr/lib/jvm/*N*`; the lookup
function is injectable: `detect(..., jdk_lookup=)`), the suite gets `env: {JAVA_HOME: <path>}`
(`JAVA_HOME` is not secret-like, so the secret guard allows it); otherwise the comment suggests
installing one or a container wrapper script (`docs/mining-recipes.md`).

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
item, so `module`, `stem` and `dir` cannot follow them. A path item (not changed by `module` or
`stem`) that starts with `-` and has no `prefix` in front is rendered as `./-name`, so the
runner does not read it as an option. File names with spaces, quotes,
`$(...)`, backticks, `;` or newlines reach the program verbatim.

Only `tests` and `modules` are placeholder sources. A `{` not directly preceded by `$` followed
by `tests`/`modules` and then `|` or `}` is a placeholder; every other brace group passes
through literally: `${VAR}`, `${VAR:-x}`, `{}`, `{a,b}`, `{1..3}`, `awk '{print}'`,
`awk '{print $1}'`, Groovy `{ ... }`. Near-misses that are almost certainly typos are still
errors naming the field: a name equal to `tests`/`modules` ignoring case (`{Tests}`), `{test}`,
`{module}`, any word followed by `|` (filter syntax, `{foo|stem}`), whitespace around the
source name (`{ tests }`, `{tests }`, `{ tests}`), and an unclosed `{tests`. Inside an argument `\|`, `\}` and `\\` are literals and `\n`/`\t`
a newline/tab. Examples: pytest `{tests}`; Django `{tests|module:backend}`; Maven
`-Dtest={tests|stem|join:,}`; Gradle `--tests` per class as above; Go
`{tests|dir|unique|prefix:./}`. `dir` of a file at the `cwd` is `.`; behind a `prefix` ending in
`/` that item is just the prefix (`./`, not `./.`), so a root-level Go test file yields
`go test ./pkg ./`, which runs the package in the current directory.

Every setup and test step (verify, and `check.sh`) also gets `CRUCIBLE_TEST_FILES` (the
suite's runnable files, repo-relative, newline-separated) and `CRUCIBLE_TEST_FILES_CWD` (the
same relative to the suite `cwd`) for wrapper scripts.

#### Wrapper scripts

Files in `<home>/scripts/` (`.crucible/scripts/`) are made available at `$CRUCIBLE_SCRIPTS`:
`verify` exports the host directory; `scaffold` copies the whole tree to `<task>/scripts/`
(modes kept; symlinks are dereferenced, so the task holds the content) and `check.sh` exports `CRUCIBLE_SCRIPTS="$TASK_DIR/scripts"`, so
`test_command: bash "$CRUCIBLE_SCRIPTS/run-tests.sh"` behaves the same in verify, `prove` and
`crucible run`. A symlink whose target lies outside the Crucible home (or is missing) is
refused: `scaffold` fails with a `ScaffoldError` and `verify` exits 1 naming the link. The
cache digest hashes the resolved contents, so editing a link target invalidates cached results.
Without a scripts dir nothing is copied or exported. The workspace never
contains `.crucible/`.

### Candidates (`mine/candidates.py`)

`Candidate` carries `id`, `title`, `base`, `merge`, `test_files`, `other_files` (each a
`FileChange`: `path`, `status` added|modified|deleted, `added`, `deleted`), `lines_changed`
(added + deleted over every changed file), `counted_lines` (the same, skipping non-test files
that match `exclude_paths`, so lockfiles, docs and generated files do not count; test files
always count; this is what `max_changed_lines`/`--max-lines` is compared with, and the
"N lines changed (max M)" exclusion reason reports it), `suites` (names of the suites owning the test files, config order; each test file is owned by the suite `owning_suite` picks: deepest `cwd`, then one whose `runnable` accepts it, then config order; JSON also keeps `suite`, the first entry), `suggested_kind`, `source`, and optional `url`, `labels`, `branch`, `issues`, `body`.
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
`excluded` (reason or null). `--max-lines` must be at least 1. `mine` takes an optional subcommand (`calibrate`, `show`, `scaffold`,
`prove`, `leakcheck`) and shares `--branch --limit --max-lines --local --json` with it.
`--limit` caps the PRs (gh) or commits (local) scanned, newest first (default 300); when the walk
hit it and few candidates remain among many exclusions, the listing prints a stderr hint to
raise it.

### Verification (`mine/verify.py`)

`crucible mine --verify <id>... [--force] [--keep] [--timeout S]` proves each candidate's tests
fail at base and pass at merge using the repo's own tests (no `claude`). `--verify` with no ids
is an error (exit 2); ids not in the candidate list, or excluded, print why on stderr and are
skipped (exit 1 after the others ran). A rejection is a normal outcome (exit 0).

- **Runnable test files.** Of the candidate's changed test files, only non-deleted ones that are
  *runnable* are passed to the test command. A suite with `runnable` globs uses them (each file
  follows the rule of the suite owning it). Otherwise the built-in rule applies: file name
  `test_*.py`, `*_test.py`, `tests.py`, `*.test.<ext>` or `*.spec.<ext>`, or any file under a
  `__tests__` directory (`config.default_is_runnable`), but never a file under `__snapshots__` or
  with a non-code suffix (`.snap`, `.json`, `.md`, images...), so a candidate whose only test
  change is a snapshot has no runnable tests. Other files under `test_paths`
  (conftest, fixtures, helpers) are overlaid but not passed. No runnable file means rejected
  `no runnable tests`, also for a command without placeholders (which then runs the whole suite).
- **Several suites.** A candidate whose test files fall in more than one suite is a normal
  candidate (`suites: [a, b]`; the listing shows `a+b`). Per side the tree is exported and every
  suite's setup runs (failure rejects as `setup failed at <side> (<suite>)`); then, on the base
  side, all test files are overlaid once; then each suite in order runs its test command with
  only that suite's runnable files (a suite with none is skipped; none at all: `no runnable tests`). Base is verified if
  at least one suite fails (all pass: `passes at base`); merge needs every suite to pass
  (`fails at merge (<suite>)`). `{modules}`/`module_root` apply per suite
  (`test outside module_root (<suite>)`). Single-suite results keep the plain reasons without
  the suffix.
- **Steps.** Export base (`workspace.export_tree`, which drops the top-level `.crucible/`) into a
  fresh dir under `paths.work_dir()/mine/`, run `setup` (if any) in the suite `cwd` **before
  anything else**, then overlay the PR's test files at their merge versions (added and modified
  are written with their git mode, so `100755` stays executable and `120000` becomes a symlink;
  deleted are removed; a write whose parent directory is a symlink, or that resolves outside the
  workspace, is refused and rejects with `unsafe path (symlink)`), then run the test command,
  which must exit non-zero. Then export merge to another fresh dir, setup, same command, which
  must exit zero. Setup comes first because build-tool setups that compile (Maven, Gradle) would
  otherwise fail on the PR's tests, which reference classes the base lacks; the dependency
  manifests setup needs are not test files, so they are present. Setup still gets
  `CRUCIBLE_TEST_FILES` (the files it may not find on disk yet). `check.sh` and `prove` use the
  same order. Setup and tests run via `bash -c` in their own process
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
- **Progress and rejections.** While verifying, one line per candidate goes to stderr as it
  finishes (`[2/5] 123: verified in 41s`). A rejection also prints the log path and, when a
  diagnostics row matches that log, its hint (JSON: `hints`). `scaffold` of a candidate whose
  cache entry exists but was produced with other settings says `config changed since
  verification; re-run --verify` (not "not verified").
- **Weak-task warnings.** A verified candidate may carry `warnings` (cache and JSON, printed
  under its line; never a rejection): a changed runnable test file contains a skip marker
  (`@Disabled`, `@Ignore`, `.skip(`, `xit(`, `xdescribe(`, `pytest.mark.skip`, `t.Skip(`, rspec
  `skip "..."` / `pending`, `[Fact(Skip=`), or every failing base step died with a diagnostics
  row of category `compile` and printed no test counts (the tests fail only because they use code
  the PR adds). The skill prefers candidates without warnings.
- The listing gets a `verified` column (`yes`, `no: <reason>`, blank) and a `verified` JSON field
  (the same string, or null) read from the cache.

### Calibration (`mine/calibrate.py`, `mine/diagnostics.py`)

Detection only guesses. `crucible mine calibrate [--candidates N] [--suite NAME]... [--json]
[--timeout S] [--force]` (plus `--branch --limit --max-lines --local`) proves, per suite, that
the config really runs the repo's tests:

1. **Setup at HEAD.** `setup` runs in a fresh export of HEAD (`workspace.export_tree`, same step
   runner and env as verify, limit `setup_timeout`); log `<home>/mine/calibrate/<suite>.setup.log`.
   A suite without `setup` skips this. Failure: `setup-failed` (`timeout` if it hit the limit),
   and no candidates are run.
2. **Samples.** The N (default 3) newest candidates owned by the suite that carry a runnable file
   for it are verified with `verify_candidate`. A multi-suite candidate's failure is attributed to
   the failing suite only (the `(<suite>)` reason suffix; a timeout by its last step). For another
   suite the sample counts only if that suite's own base and merge test steps both ran (it
   passed at merge): then it is verified, or `passes at base`, from its own logs; otherwise the
   candidate is skipped for that suite and the next one is sampled (a suite left with none is
   `no-candidates`). Verifications: results already in the verify cache count toward
   the sample without running (`--force` reruns), others run and are cached under verify's own
   rules, so `timeout` and `setup failed ...` are never cached. `--timeout S` overrides the test
   step as in verify.
3. **Status** per suite: `ok` (at least one sample verified), `setup-failed`, `no-tests-ran`
   (every sample's log matches a diagnostics row with `status: no-tests-ran`, for example pytest
   exit 5), `not-selective` (every sample "passes at base"), `failing` (every sample fails at
   merge), `timeout`, `no-candidates` (nothing to sample; not a failure, but the table warns that
   the suite is untested) and `mixed` (samples fail in different ways; `detail` counts them).
   Exit code 0 only when every selected suite is `ok` or `no-candidates`.

**Diagnostics** are data: `src/crucible/data/diagnostics.yaml` (shipped in the wheel). A rule has
`id`, `category`, `hint` (names the mine.yaml field to change), `pattern` (regex over the log
body, header line excluded, last 64 KiB) and/or `exit` (both given: both must match), optional
`stage` (`setup`/`test`), `status: no-tests-ran` and `unless` (a regex: the row does not apply to
a log it matches, e.g. go's `[no test files]` while other packages ran tests, Maven's per-module
"No tests to run"; set on the go, maven, gradle and jest rows). A top-level `ran_tests` list of
patterns (pass/fail counts) also switches every `status` row off for a log it matches. The step runner appends `exit code: N` to
every log, which is where the exit code is read from. Rows cover missing runner (127, "command
not found", missing script), permission, "no tests" messages of pytest, jest, vitest, mocha,
maven, gradle, go, cargo, rspec, phpunit, dotnet and mix, Python/Java/Node import errors, missing
environment variables, refused service connections and timeouts, wrapper scripts missing
(`<path>: No such file or directory`, `exec: ...: not found`), JVM toolchain trouble (Gradle "Cannot
find a Java installation", "release version N not supported", "Unsupported class file major
version", Lombok on a newer JDK: hint to set `JAVA_HOME` in `env` or use a container wrapper), Ruby
`LoadError`, Node engine mismatches, and `compile`-category rows (javac/Maven, TypeScript, Go
build, Rust/C#) used for weak-task warnings. The exit-127 hint suggests a container wrapper in
`.crucible/scripts/` when the toolchain is not installed on the host. `status_hints` gives a generic
hint per status. A test requires a sample log for every row, so contributors adding a row add its
sample to `tests/test_mine_diagnostics.py`.

**`--json`** (stable): `{"config_hash", "ok", "suites": [{"name", "status", "detail",
"setup": {"ok", "seconds", "log", "tail"}, "samples": [{"id", "outcome" (verified | rejected |
error), "reason", "cached", "logs", "tail"}], "hints": [{"id", "category", "hint"}]}]}`. `tail` is
the last 20 lines of the setup log, or of the last step of that sample.

**State.** `<home>/mine/calibration.json` holds `{"config_hash", "suites": {name: <suite
report> + "calibrated_at"}}`. The hash covers every suite's `settings_hash()` (so editing a
suite, or a wrapper script, which `scripts_digest()` is mixed into, makes the file stale) and,
unlike verify's cache key, the suites' `timeout` and `setup_timeout`: raising a limit makes a
stale `timeout` status disappear (the verify cache is unaffected). A `--suite` run merges into a file of the same hash; a different hash replaces it.
`crucible mine` (listing, also `--json`) and `--verify` print one warning line on stderr when the
current config was never calibrated, changed since the last calibration, or its last calibration
was not ok; they never fail because of it.

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
  `timeout_minutes: 30`, `max_turns: 100`, `check_timeout_minutes` (the suites'
  `setup_timeout` + `timeout`, rounded up), `check: ./check.sh`, and `_(...)_` placeholders in the
  three sections, so an unedited task fails `crucible task validate`.
- **`hidden_tests/`.** The PR's test files (all changed test files, runnable or not) at their
  merge versions at repo-relative paths (git modes kept: exec bit, symlinks; `check.sh`'s `tar`
  copy preserves both), plus `hidden_tests/DELETED` (one path per line) for test
  files the PR removed.
- **Several suites.** `check.sh` runs every suite's setup first (own subshell, `env` and `cwd`;
  a failure exits 97 immediately), installs the hidden tests once, then runs each suite's test
  command in its own subshell, printing `== suite <name> ==` before each. Test failures are
  collected so every suite runs, and the script exits with the first failing code. `prove` is
  unchanged.
- **`check.sh`** (mode 755), generated from the suite, run by `checker.run_check` with the
  workspace as cwd and `CRUCIBLE_TASK_DIR` set (falls back to the script's own directory). It
  exports the suite `env`, `CRUCIBLE_TEST_FILES` and `CRUCIBLE_TEST_FILES_CWD` (and
  `CRUCIBLE_SCRIPTS` when `<task>/scripts/` was copied), `cd`s to the suite `cwd` and runs
  `setup` (as in verify, **before** the hidden tests exist in the workspace), then copies
  `hidden_tests/` (minus `DELETED`) over the workspace with `tar`, removes the `DELETED` paths,
  and exec's the test command, each setup/test via `bash -c '<quoted command>'`, with the
  placeholders filled by `verify.build_command` from `verify.runnable_files`, exactly what
  `--verify` ran. A failing
  setup prints `crucible-check: setup failed` and exits 97 (so `prove` never counts a broken
  install as "fails before"); a failed copy exits 98.
- **Docker.** If `check.sh` or a copied script mentions `docker`, scaffold warns that the task
  needs Docker on the machine that runs it and records `"requires": ["docker"]` in `.mine.json`.
- **`.mine.json`** in the task dir (`id`, `base`, `merge`, `suite`, `suites`, `source`,
  `check_timeout_s`, optionally `requires`) records the merge SHA deterministically; `check_timeout_s` is the sum of
  the suites' `setup_timeout` and `timeout`, the default for `prove`'s per-run limit. The workspace never contains `.crucible/`, so a hidden file in the task
  dir does not leak. `task.md` only needs `source`; `mined_tasks` reads only `task.md`.

`crucible mine prove <task-dir> [--timeout S]` (default: `check_timeout_minutes` from
`task.md`, else `check_timeout_s` from `.mine.json` for older tasks, else 600; one limit covers setup and tests of a run) loads the finished task (so placeholders must be
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
  operator); `<!-- -->` plus the C-like `//` and `/* */` (embedded scripts) for html/htm/xml/vue/jsp/svelte/astro; lua also has `--[[ ]]` blocks; php takes `//`, `#` and `/* */`; css only
  `/* */`; an unknown extension keeps the permissive union of `#`, `//` and `/* */`. String
  and identifier handling is the same for every language. URL-path-like backticked text (`/api/x/`, `POST /api/x`) is one token, flagged only if
  the exact path is on an added non-test line, absent from the base tree and not in the test diff.
- **file-instruction**: a sentence naming a changed non-test file (path or basename) together
  with an edit verb (add, change, modify, update, edit, rewrite, replace, remove, create,
  implement, patch) or `in <file>`.

`crucible mine prove` runs it after the check proves out and exits 1 (`not proven: <id>: task
text leaks the solution`) on flags unless `--allow-leaks`, which still prints them.
