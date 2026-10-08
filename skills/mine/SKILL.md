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
  edit it** before going on. With no suites detected, help fill one in: `test_paths`, `cwd`,
  `setup`, and a `test_command` (`{tests}` with optional `|filters`, or no placeholder to run the whole suite; see docs/design.md, "Placeholders"). Use only dummy values in
  `env`. If `crucible mine` prints config errors (exit 1), fix the file and rerun.

## 1. List candidates

Run `crucible mine --json <list flags>`. Output is `{"candidates": [...], "excluded": [...]}`;
each candidate has `id`, `title`, `suites` (several for a full-stack PR; it is verified across all of them), `suggested_kind`, `lines_changed`, `test_files`,
`other_files`, `source`. (With a GitHub remote and a working `gh` it lists merged PRs, otherwise
local commits; a `gh source unavailable` line on stderr is normal. `--local` forces local git.)

Show a short table: id, title, suggested kind, lines changed, test files. Say how many
candidates were excluded and why in one line (run `crucible mine --json --show-excluded` only if
the user asks for the list). Let the user pick: default is all, capped at 50; they may drop ids or
change kinds. No candidates: say so and stop.

## 2. Verify

Run `crucible mine --verify <ids...> <list flags>`. It runs the repo's own tests at each
candidate's base (must fail) and merge (must pass) and may take minutes per candidate. With more
than about 5 ids use the Bash tool's `run_in_background: true` and wait for the notification
instead of polling. Add `--timeout S` only if the user says tests are slow, `--force` only to
ignore cached results. Then run `crucible mine --json <list flags>` again; each candidate's
`verified` field is `yes`, `no: <reason>`, or null. Report verified vs rejected with reasons
(for example `passes at base`, `fails at merge`, `setup failed at base`, `no runnable tests`). A rejection is a normal
outcome, not an error. If nothing verified, stop and say what to fix (usually `mine.yaml`).

## 3. Confirm the cost

Writing prose is the only model cost. Say roughly how many tasks you will write (the verified
count) and that each takes a few tool calls and a short write-up, then **ask whether to continue**.
Do nothing further until the user says yes; offer to do a subset.

## 4. Scaffold and write each verified candidate

For each verified id:

1. `crucible mine scaffold <id> <list flags>` (add `--task-id <slug>` or `--kind <kind>` only
   when needed). It writes `.crucible/tasks/<slug>/` with `task.md`, `check.sh`, `hidden_tests/`
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

## 5. Leak check

Run `crucible mine leakcheck .crucible/tasks/<slug>` (add `--json` to read flags programmatically).
`clean: <slug>` means done. Otherwise it exits 1 listing flags with `kind`
(`quoted-code`, `new-identifier`, `file-instruction`), `span`, `section` and reason: rewrite
the flagged text to describe behaviour instead (remove the identifier or file instruction, put
the requirement in user-visible terms) and rerun. At most 3 tries; if still flagged, delete the
task directory (`rm -r .crucible/tasks/<slug>`, only that path), leave it out and say so.

## 6. Validate and prove

Both must pass for each task:

- `crucible task validate .crucible/tasks/<slug>` prints `ok: <slug>`.
- `crucible mine prove .crucible/tasks/<slug>` runs the task's `check.sh` at `ref` (must fail)
  and at the merge commit (must pass) and checks for leaks; prints `proven: <slug>`. Use
  `--timeout S` if the user raised it in step 2. Never use `--allow-leaks`.

If either fails, show the error, remove that task directory (`rm -r .crucible/tasks/<slug>`) and
report why. Do not retry beyond the leak rewrites above.

## 7. Summary

Report: tasks written, grouped by kind (ids and slugs); candidates rejected at verification,
left out after leak rewrites, or removed after validate/prove, each with its reason. Remind the
user the new tasks are uncommitted files in `.crucible/tasks/` (you did not commit). Give the next
command, with a real slug: `/crucible:run <slug> --matrix default --repeat 3` (it asks before
spending anything). Never start a run yourself.
