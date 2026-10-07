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
   - the recommendation and the evidence behind it (success rate, median cost, median time);
   - the runner-up, and the trade-off against it;
   - caveats: few attempts, `Unknown` results, unpriced attempts, or a warning that the task
     changed between batches.
   If no config meets the bar, say so and name the best candidates instead of recommending one.
