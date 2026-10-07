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
   neither `--matrix` nor `--configs`, use `--matrix default`. Add `--judge` unless the user said
   not to judge. Pass `--repeat`, `--judge-config` and `--max-budget-usd` through only if the user
   gave them. Never raise `--max-budget-usd` on your own.
2. **Preview.** Run the same command plus `--dry-run` (it costs nothing) and show the planned
   attempts and their count. Each attempt is a real `claude` run, and `--judge` adds a judge run
   per attempt. State the per-attempt budget cap (`--max-budget-usd`, default 5.0) so the user
   sees the worst case.
3. **Confirm.** Ask the user whether to start. Do not run anything paid until they say yes. If
   they decline or change flags, redo step 2.
4. **Run in the background.** Run the real command (without `--dry-run`) with the Bash tool's
   `run_in_background: true`, from the repo root. Tell the user it started and that they can keep
   working; do not wait or poll in a loop.
5. **When it finishes** (you are notified), read its output. If it exited non-zero, show the
   error and stop. Otherwise run `crucible report <id>` and summarise: which configurations
   passed, their time and cost, and the recommendation if there is one. Say plainly when the
   evidence is thin (few attempts, unknown results).
