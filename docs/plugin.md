# Crucible as a Claude Code plugin

Researched 2026-10-05 from https://code.claude.com/docs/en/plugins (manifest-reference,
marketplace-reference, cli-reference, install), /skills, /headless and /tools-reference.

## Layout (this repo is both the marketplace and the plugin)

```
.claude-plugin/plugin.json        {"name": "crucible", "version": ..., "description": ...}
.claude-plugin/marketplace.json   {"name": "crucible", "owner": {...},
                                   "plugins": [{"name": "crucible", "source": "./"}]}
bin/crucible                      shim; plugin bin/ dirs are on Claude's Bash PATH
skills/<name>/SKILL.md            namespaced as /crucible:<name>
src/crucible/data/                bundled pricing.yaml and configs/default.yaml
```

- Install: `/plugin marketplace add Jaxsonman/Crucible` then `/plugin install crucible@crucible`
  (or the `claude plugin marketplace add` / `claude plugin install` shell commands).
  Private repos clone with the user's existing git credentials.
- Validate: `claude plugin validate .` must pass.
- Session-only testing without installing: `claude --plugin-dir <repo> ...`; the plugin id is
  then `crucible@inline`. Headless: `claude -p "/crucible:init" --plugin-dir <repo>` expands
  the skill. Check the stream-json `system/init` event's `plugin_errors` is empty.

## Paths and environment

- Installed plugins are copied to `~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/`.
  That root changes on every version, so **never write state there** (no `.venv`).
- `${CLAUDE_PLUGIN_ROOT}` and `${CLAUDE_PLUGIN_DATA}` are substituted inline in SKILL.md text,
  but they are **not** exported to commands Claude runs with the Bash tool.
- `bin/crucible` therefore derives the plugin root from its own path and keeps its venv outside
  the plugin root: `UV_PROJECT_ENVIRONMENT` = `${CRUCIBLE_VENV_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/crucible/venvs}/<hash of plugin root>`.
  It runs `uv run --frozen --no-editable --project <root> crucible "$@"`. `uv` must be on PATH.
- Outside Claude Code the CLI still runs as before: `uv run crucible ...` in this repo, or the
  shim by its full path.

## Skills

| Skill | Invocation | Notes |
|-------|-----------|-------|
| `create-task` | user or model | from spec/ticket/PR; may use the user's MCP connections |
| `init` | user only (`disable-model-invocation: true`) | runs `crucible init` |
| `run` | user only | spends usage: confirms the plan via `--dry-run`, then runs `crucible run` with Bash `run_in_background`, and reports when it finishes |
| `report` | user or model | runs `crucible report [task]` and summarises |
| `mine` | user only | spends usage on prose only: lists merged PRs (`crucible mine --json`), verifies them (`--verify`), then per candidate `scaffold`, writes What/Why/criteria from the PR, `leakcheck`, `task validate`, `prove`. Never runs a benchmark or commits |

Skills call the `crucible` command (the shim on PATH). `allowed-tools` frontmatter only
pre-approves tools for the invoking turn.

## Background runs

Claude can run Bash with `run_in_background` and keep working; interactive sessions have no
limit on background commands. In `-p` mode they're capped (30 min default) and killed ~5 s
after the final result, so headless tests run `crucible run` in the foreground.
