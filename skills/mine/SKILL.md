---
name: mine
description: Mine the repo's merged PRs (or local commits) into verified Crucible benchmark tasks. The CLI lists, verifies, scaffolds and proves; this skill only writes each task's prose and picks its kind. Spends usage, so it confirms first.
argument-hint: "[--limit N] [--local] [--max-lines N]"
disable-model-invocation: true
allowed-tools: Bash(crucible *)
---

# Mine tasks from merged PRs

Arguments: `$ARGUMENTS`. They are passed through to `crucible mine` and its subcommands as
`--limit`, `--local` and `--max-lines` (call them the **list flags**). Every deterministic step is a
`crucible` command (on PATH while the plugin is enabled); run them all from the repo root. Your
only jobs are the prose of each task and its kind. Never start a benchmark run and never commit.

## 0. Home and config

- No `.crucible/` directory: run `crucible init` (needs a git repo).
- No `.crucible/mine.yaml`: run `crucible mine`. It writes a commented `mine.yaml` (suites
  auto-detected from the repo, or an empty template) and exits 0. Read the file, show the
  detected suites (name, test paths, cwd, setup, test command), and **ask the user to confirm or
  edit it** before going on (it is a guess; steps 1 and 2 correct it). With no suites detected, help fill one in: `test_paths`, `cwd`,
  `setup`, and a `test_command` (`{tests}` with optional `|filters`, or no placeholder to run the whole suite; see docs/design.md, "Placeholders"). Use only dummy values in
  `env`. If `crucible mine` prints config errors (exit 1), fix the file and rerun.

## 1. Discover how this repo runs tests

The detected suites are only a guess from marker files. Before trusting them, read what the repo
really does (CI is the strongest signal) and list the files you read:

- CI: `.github/workflows/*`, `.gitlab-ci.yml`, `Jenkinsfile`, `.circleci/config.yml`,
  `azure-pipelines.yml`, `bitbucket-pipelines.yml`.
- Task runners: `Makefile`, `justfile`, `Taskfile.yml`, `package.json` `scripts`, `tox.ini`,
  `noxfile.py`.
- Build files (`pom.xml`, `build.gradle*`, `Cargo.toml`, `go.mod`, `*.csproj`, `mix.exs`,
  `Gemfile`, `composer.json`), `README` / `CONTRIBUTING`, `Dockerfile*`, `docker-compose*.yml` and
  test containers.

Compare with the suites in `.crucible/mine.yaml` and correct them from what CI runs: `cwd`,
`setup` (install and build steps), `test_command`, `test_paths`, `runnable` (which changed files
are passed to the runner), `env`, `timeout` / `setup_timeout`. If the runner can't take a file
list directly, write a wrapper under `.crucible/scripts/`; `crucible` exports it as
`$CRUCIBLE_SCRIPTS` and gives it the PR's runnable test files in `$CRUCIBLE_TEST_FILES`
(newline-separated, repo-relative; `$CRUCIBLE_TEST_FILES_CWD` is relative to the suite `cwd`):

```bash
# .crucible/scripts/run-tests.sh   (mine.yaml: test_command: bash "$CRUCIBLE_SCRIPTS/run-tests.sh")
set -euo pipefail
files=()
while IFS= read -r f; do [ -n "$f" ] && files+=("$f"); done <<< "$CRUCIBLE_TEST_FILES_CWD"
[ "${#files[@]}" -gt 0 ] || { echo "no test files" >&2; exit 2; }   # never pass an empty argument
exec ./tools/run_tests --only "${files[@]}"
```

`setup` runs before the PR's test files are copied in, so it must not need them (compile main
code only: Maven `compile`, Gradle `classes`). If the toolchain is not installed on this machine
(a JDK of another version, Go, an old Ruby), run it in a container from a wrapper script and
note that the task then needs Docker wherever it runs. Tested recipes (Go via Docker, Gradle
with JDK toolchains, Maven with `JAVA_HOME`, Ruby with `vendor/bundle`) are in
`docs/mining-recipes.md`.

Rules: use only dummy values in `env`, never real secrets; prefer the repo's own test setup (for
a database or queue, start the compose service in `setup`); keep every command non-interactive;
touch nothing but `.crucible/` (never edit tracked repo files). Show the user what you changed.

## 2. Calibrate

Run `crucible mine calibrate --json <list flags>` (add `--suite NAME` for one suite,
`--candidates N` for a bigger sample, default 3). It runs `setup` at HEAD and verifies the
newest candidates per suite, and takes minutes (use `run_in_background: true`). Output:
`{"ok", "suites": [{"name", "status", "detail", "setup": {"ok", "seconds", "log", "tail"},
"samples": [{"id", "outcome", "reason", "tail", "logs"}], "hints": [{"id", "hint"}]}]}`. Status is
`ok`, `no-candidates` (nothing to sample: not a failure, but tell the user that suite is
untested), or a failure: `setup-failed`, `no-tests-ran`, `not-selective`, `failing`, `timeout`,
`mixed`. For each failing suite read its status, `hints` (each names the `mine.yaml` field to
change) and the log `tail`s (open the log files for more), fix `.crucible/mine.yaml` or the
wrapper script, and rerun. At most **5 rounds**. If a suite still fails, stop and ask the user,
quoting the suite name, status, hint and the last 20 log lines. Continue to step 3 once every
suite is `ok` or `no-candidates`; with `no-candidates` only, calibration proved nothing yet, so
say so and offer to continue anyway.

Per-candidate progress lines go to stderr as each finishes. If the list looks short, the walk may
have been cut by `--limit` (the CLI hints when so); raise it.

## 3. List candidates

Run `crucible mine --json <list flags>`. Output is `{"candidates": [...], "excluded": [...]}`;
each candidate has `id`, `title`, `suites` (several for a full-stack PR; it is verified across all of them), `suggested_kind`, `lines_changed`, `test_files`,
`other_files`, `source`. (With a GitHub remote and a working `gh` it lists merged PRs, otherwise
local commits; a `gh source unavailable` line on stderr is normal. `--local` forces local git.)

Show a short table: id, title, suggested kind, lines changed, test files. Say how many
candidates were excluded and why in one line (run `crucible mine --json --show-excluded` only if
the user asks for the list). Let the user pick: default is all, capped at 50; they may drop ids or
change kinds. No candidates: say so and stop.

## 4. Verify

Run `crucible mine --verify <ids...> <list flags>`. It runs the repo's own tests at each
candidate's base (must fail) and merge (must pass) and may take minutes per candidate. With more
than about 5 ids use the Bash tool's `run_in_background: true` and wait for the notification
instead of polling. Add `--timeout S` only if the user says tests are slow, `--force` only to
ignore cached results. Then run `crucible mine --json <list flags>` again; each candidate's
`verified` field is `yes`, `no: <reason>`, or null. Report verified vs rejected with reasons
(for example `passes at base`, `fails at merge`, `setup failed at base`, `no runnable tests`);
each rejection prints its log path and, when a diagnostics row matches, a hint. A rejection is a
normal outcome, not an error. A verified candidate may carry `warnings` (skipped tests in the
changed test files, or a base failure that is only a compile/type error): **prefer candidates
without warnings** and tell the user which ones have them. If scaffold later says `config changed
since verification`, rerun the verify. If nothing verified, stop and say what to fix (usually `mine.yaml`).

## 5. Confirm the cost

Writing prose is the only model cost. Say roughly how many tasks you will write (the verified
count) and that each takes a few tool calls and a short write-up, then **ask whether to continue**.
Do nothing further until the user says yes; offer to do a subset.

## 6. Scaffold and write each verified candidate

For each verified id:

1. `crucible mine scaffold <id> <list flags>` (add `--task-id <slug>` or `--kind <kind>` only
   when needed; it warns when the task needs Docker on the machine that runs it, recorded as
   `requires` in `.mine.json`: tell the user). It writes `.crucible/tasks/<slug>/` with `task.md`, `check.sh`, `hidden_tests/`
   and `.mine.json`; note the printed path. It exits 1 with the reason if the candidate is not
   verified or already mined; report that and move on.
2. `crucible mine show <id> --json <list flags>`: gives `title`, `body`, `labels`, `branch`,
   `issues` (linked issue title and body), `test_files`, `other_files`, `test_diff` and
   `non_test_diff`.
3. Edit `.crucible/tasks/<slug>/task.md` and replace every `_(...)_` placeholder:
   - **What**: the problem or feature to deliver, in the PR title, body and linked issue's own
     terms.
   - **Why**: the motivation from the PR body or issue.
   - **Acceptance criteria**: one observable bullet each, covering the **behaviour** the hidden
     tests check (read `test_diff` for what they assert). Describe the symptom and the expected
     behaviour. Never describe the implementation: do not quote the fix, name new functions,
     classes, variables or files the PR introduced, or say which file to change. The same text
     goes to every model config.
   - **kind** (front matter): keep the suggested kind unless labels, branch name and title
     clearly say otherwise (`feature`, `bugfix`, `refactor`, `docs`, `test`, or an existing kind).
     Leave `repo`, `ref`, `source`, `check` and the other front matter alone.
   - No secrets, and nothing copied from `non_test_diff`.

## 7. Leak check

Run `crucible mine leakcheck .crucible/tasks/<slug>` (add `--json` to read flags programmatically).
`clean: <slug>` means done. Otherwise it exits 1 listing flags with `kind`
(`quoted-code`, `new-identifier`, `file-instruction`), `span`, `section` and reason: rewrite
the flagged text to describe behaviour instead (remove the identifier or file instruction, put
the requirement in user-visible terms) and rerun. At most 3 tries; if still flagged, delete the
task directory (`rm -r .crucible/tasks/<slug>`, only that path), leave it out and say so.

## 8. Validate and prove

Both must pass for each task:

- `crucible task validate .crucible/tasks/<slug>` prints `ok: <slug>`.
- `crucible mine prove .crucible/tasks/<slug>` runs the task's `check.sh` at `ref` (must fail)
  and at the merge commit (must pass) and checks for leaks; prints `proven: <slug>`. Use
  `--timeout S` if the user raised it in step 4. Never use `--allow-leaks`.

If either fails, show the error, remove that task directory (`rm -r .crucible/tasks/<slug>`) and
report why. Do not retry beyond the leak rewrites above.

## 9. Summary

Report: tasks written, grouped by kind (ids and slugs); candidates rejected at verification,
left out after leak rewrites, or removed after validate/prove, each with its reason. Remind the
user the new tasks are uncommitted files in `.crucible/tasks/` (you did not commit). Give the next
command, with a real slug: `/crucible:run <slug> --matrix default --repeat 3` (it asks before
spending anything). Never start a run yourself.
