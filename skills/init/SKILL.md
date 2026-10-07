---
name: init
description: Create the .crucible/ home for Crucible benchmarks in the current git repo. Run once per repo before creating tasks.
disable-model-invocation: true
allowed-tools: Bash(crucible *)
---

# Initialise Crucible in this repo

1. Run `crucible init` from inside the git repo. It creates `.crucible/` (`tasks/`, `results/`,
   `configs/`) and git-ignores `.crucible/results/`. It is safe to run again; it changes nothing
   that already exists.
2. If it fails because this is not a git repo, say so and stop. Do not create anything by hand.
3. Report what the command printed, then explain the next steps in a few lines:
   - `/crucible:create-task` turns a ticket, spec or PR into a benchmark task.
   - `/crucible:run <task-id>` previews the plan, asks you to confirm, then runs it in the
     background (this spends usage).
   - `/crucible:report [task-id]` compares configurations and recommends one.
   - To share results with the team, delete `results/` from `.crucible/.gitignore`.
