---
name: report
description: Show and explain Crucible benchmark results for one task or across tasks, with the recommended model configuration. Use when the user asks which model or effort to use, or how a benchmark went.
argument-hint: "[task-id] [--batch ID | --kind KIND | --json]"
allowed-tools: Bash(crucible *)
---

# Report benchmark results

Arguments: `$ARGUMENTS`.

1. Run `crucible report $ARGUMENTS` from the repo. With a task id it compares every
   configuration for that task; without one it reports across tasks, grouped by task kind, with
   a recommendation per kind. Flags: `--batch`, `--json`, `--kind`, `--min-attempts`,
   `--threshold`.
2. If it says there are no results, tell the user to run `/crucible:run <task-id>` first. Do
   not start a run yourself; it spends usage.
3. Summarise in plain language, not by pasting the table alone:
   - the recommendation and the evidence behind it (success rate with its 95% interval, cost
     per success, median time), and on how many tasks it was compared (the cross-task report
     prints `Compared on N of M tasks attempted by every config`);
   - the runner-up, and the trade-off against it;
   - caveats: few attempts or a wide interval, `Unknown` results, `Stopped` attempts (a limit
     ended them), `Judge≠check` disagreements, unpriced attempts, a warning that a config
     pooled several models, or a warning that the task changed between batches. When the
     report says "compared on N of M tasks" with N below M, or "no common task", relay it.
   If no config meets the bar, say so and name the best candidates instead of recommending one.
   If it prints `Recommendation: none; no task was attempted by every config`, say there is no
   common task to compare on and suggest running the same task with every config; do not
   pick a winner.
