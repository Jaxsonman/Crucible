---
name: run
description: Run a Crucible benchmark for a task against a matrix of model configurations, in the background, then summarise the results. Spends usage, so it confirms first.
argument-hint: "<task-id> [--matrix default | --configs ...] [--repeat N]"
disable-model-invocation: true
allowed-tools: Bash(crucible *)
---

# Run a benchmark

Arguments: `$ARGUMENTS`. The first word is the task id (a directory under `.crucible/tasks/`);
the rest are `crucible run` flags. The CLI is the only thing that runs attempts; never run
attempts yourself.

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
3. **Confirm.** Ask the user whether to start. Do not run anything paid until they say yes. If
   they decline or change flags, redo step 2.
4. **Run in the background.** Run the real command (without `--dry-run`) with the Bash tool's
   `run_in_background: true`, from the repo root. Tell the user it started and that they can keep
   working; do not wait or poll in a loop.
5. **When it finishes** (you are notified), read its output. If it exited non-zero, show the
   error and stop. Otherwise run `crucible report <id>` and summarise: which configurations
   passed, their time and cost, and the recommendation if there is one. Say plainly when the
   evidence is thin (few attempts, unknown results).
