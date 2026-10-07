# Mining skill smoke test

Scratch repo `/tmp/mine-smoke` (outside the Crucible checkout): `calc.py` plus `tests/`, three
commits: initial `add`, then "Add divide with zero check" and "Add clamp helper", each adding code
and a new test file (the test fails on the parent commit). No GitHub remote, so the local source
is used (`--local`). Claude Code 2.1.289, plugin loaded with `--plugin-dir` (`crucible@inline`).

`claude plugin validate .`: passed (one warning: root `CLAUDE.md` is not loaded as plugin context). The `system/init` event listed `crucible:create-task`, `crucible:init`,
`crucible:mine`, `crucible:report`, `crucible:run`, and `plugin_errors` was empty.

## Command

```bash
cd /tmp/mine-smoke
claude -p '/crucible:mine --local
Answers to every confirmation so you do not need to ask: (1) mine.yaml: confirmed, but first remove the setup line (pytest is already installed, no setup needed) and keep everything else; (2) pick all candidates; (3) proceed with writing tasks. Do not ask any questions.' \
  --plugin-dir <worktree> --model sonnet --max-budget-usd 2 --output-format stream-json --verbose \
  --allowedTools "Bash(crucible:*)" "Bash(rm:*)" "Bash(git:*)" "Bash(ls:*)" "Bash(cat:*)" "Read" "Write" "Edit"
```

## Result

- Outcome `success`, 13 turns, 58 s wall time, **cost $0.245** (`total_cost_usd`; the only attempt).
- Flow the model followed: `crucible init`, `crucible mine` (wrote `mine.yaml`, detected `pytest`),
  edit `mine.yaml`, `crucible mine --json --local`, `crucible mine --verify 1abbdb51 8548f5cd --local`,
  `scaffold` and `show --json` for both, filled the `task.md` prose, `leakcheck`, `task validate`,
  `prove`. Both candidates verified.
- The divide task was flagged once by `leakcheck` (a criterion named `ValueError`, absent from the
  base tree); the model reworded it and the recheck was clean.
- Independent recheck afterwards, from the shell with the plugin shim, on both tasks:

```
ok: commit-1abbdb51-add-clamp-helper        clean: commit-1abbdb51-add-clamp-helper        proven: commit-1abbdb51-add-clamp-helper
ok: commit-8548f5cd-add-divide-with-zero-check   clean: commit-8548f5cd-add-divide-with-zero-check   proven: commit-8548f5cd-add-divide-with-zero-check
```

- Nothing was committed and no benchmark run started; the tasks are untracked files in `.crucible/`.

## Notes

- `bin/crucible` uses `uv run --no-editable`, and uv caches the built wheel of a local project by
  its `pyproject.toml`, not its sources. In a dev worktree the shim can therefore run stale code
  (here `crucible mine` lacked its newer flags). `uv cache clean crucible` plus deleting the
  venv under `~/.cache/crucible/venvs/` fixed it. Installed plugins are unaffected: each version
  has its own root.
