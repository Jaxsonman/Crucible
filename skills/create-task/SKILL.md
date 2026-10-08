---
name: create-task
description: Turn a spec, ticket or PR into a Crucible benchmark task (.crucible/tasks/<id>/ with task.md, check.sh and hidden tests) that points at a real repo. Use when the user wants to create, import or write a benchmark task, or says "make a task from this ticket/spec".
---

# Create a Crucible task

If the repo has merged PRs and the user wants many tasks from them, suggest `/crucible:mine`
(it lists, verifies and scaffolds tasks from merged PRs) instead of writing tasks one by one.

Produce a task in `.crucible/tasks/<id>/` of the current repo that validates and that every model
config can run unchanged. Use the `crucible` command (on PATH while the plugin is enabled).

## 0. Make sure there is a Crucible home

If the repo has no `.crucible/` directory, run `crucible init` first (it needs a git repo). Run
every later `crucible` command from inside the repo. The task format is a `task.md` with
`## What`, `## Why` and `## Acceptance criteria` sections; the scaffold from step 2 shows the
exact front matter.

## 1. Gather inputs (ask only for what's missing, one question at a time)

- **Source material:** the spec, ticket text, PR, or context files. If the user names a
  ticket or PR and you have a tool or MCP connection that can read it, fetch it yourself.
- **Target repo:** defaults to the current repo (`crucible task new` fills in its root). Ask only
  if the user wants a different local clone. Fixture tasks (`repo: fixture`) are for small
  self-contained exercises only.
- **Starting ref:** the commit the agent starts from. For a merged PR, use the PR's base commit
  (the parent of the fix), never the fix itself. Otherwise the default is the repo's current `HEAD`
  (`crucible task new` pins its SHA); it warns if the working tree is dirty: uncommitted
  changes are **not** included.
- **Kind:** `feature`, `bugfix`, `refactor`, `docs`, `test`, or the user's own label. Reports
  group recommendations by kind, so reuse existing kinds
  (`grep -h '^kind:' .crucible/tasks/*/task.md`).
- **Task id:** kebab-case, `^[a-z0-9][a-z0-9-]{0,63}$`, and not already in `.crucible/tasks/`.

## 2. Scaffold and write `task.md`

Run `crucible task new <id>` (add `--ref <sha>` to start from another commit), then fill in
`.crucible/tasks/<id>/task.md`:

- Frontmatter: `repo:` and `ref:` are prefilled for the host repo (a full commit SHA, never a
  branch name, so the task never drifts); only change them for a PR base commit or another
  clone. Set `kind:`,
  `check: ./check.sh`, and `timeout_minutes`/`max_turns` sized to the work (defaults 30 / 100).
- Delete any scaffolded `fixture/` directory when the task uses a real repo.
- **What:** the change to make, in the ticket's own terms. Name the files or areas involved if
  the source does.
- **Why:** the motivation from the source.
- **Acceptance criteria:** one observable, testable bullet each. They are what the judge grades.
- Do **not** include the solution, hints from the real fix, or anything a model could copy.
  The same text goes to every config.
- No `_(…)_` placeholders may remain.

## 3. Write the check (the most important part)

The check is the deterministic success signal. Put tests the agent must not see in
`.crucible/tasks/<id>/hidden_tests/`. The workspace never contains the task directory.

`check.sh` runs with the workspace as cwd. It installs dependencies (they are not in the
workspace; only tracked files are), copies the hidden tests in, and runs them:

```bash
#!/usr/bin/env bash
set -euo pipefail
T="${CRUCIBLE_TASK_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
# install deps, e.g.: npm ci --silent | uv sync -q | pip install -q -e .
# cp -R "$T/hidden_tests/." <where the repo's tests live>/
# run only the hidden tests, e.g.: npx vitest run <paths> | uv run pytest -q <paths>
```

- From a merged PR: copy the tests the PR added or changed into `hidden_tests/`.
- Without one: write tests from the acceptance criteria. Keep them strict but not tied to one
  implementation.
- If a criterion can't be tested automatically, leave it to the judge and say so in your summary.
- Never put secrets in the task. If tests need env vars, use their names and tell the user.

## 4. Prove the check works

Export the starting tree and run the check against it:

```bash
T="$PWD/.crucible/tasks/<id>"
W=$(mktemp -d) && git -C <repo> archive <ref> | tar -x -C "$W"
(cd "$W" && CRUCIBLE_TASK_DIR="$T" bash "$T/check.sh"); echo "exit=$?"
```

- It **must fail** on the starting ref. If it passes, the task is already solved; fix the tests.
- If a known-good solution exists (the merged fix commit), export that and confirm the check
  **passes**. If it fails there, the check is wrong, not the solution.
- Delete the temp dirs afterwards.

## 5. Validate and hand off

- `crucible task validate .crucible/tasks/<id>` must print `ok: <id>`.
- `crucible run .crucible/tasks/<id> --matrix default --dry-run` shows the planned attempts.
- Give the user a short summary: the task id, kind, repo and ref, how many criteria, which
  criteria the check covers and which only the judge covers, and whether step 4 confirmed
  fail-before and pass-after. Then give the run command
  (`/crucible:run <id> --matrix default --repeat 3`, or
  `crucible run .crucible/tasks/<id> --matrix default --repeat 3`). `--judge` is optional when
  the task has a `check.sh` (the check decides success and the judge is only advisory); add it
  only for a task with no check or when the user wants the extra opinion.
- Do not start a real run yourself; it spends the user's usage. Do not commit unless asked
  (`.crucible/results/` is git-ignored; tasks are yours to commit).
