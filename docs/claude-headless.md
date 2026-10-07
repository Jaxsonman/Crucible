# Claude Code headless reference (for the executor)

Researched 2026-10-01 from https://code.claude.com/docs/en/cli-reference,
/headless, /model-config, /agent-sdk/typescript and /agent-sdk/cost-tracking.
Field shapes were confirmed against real runs; correct this file if a Claude Code release changes them.

## Command line Crucible uses

```
claude -p "<prompt>" \
  --model <model> --effort <effort> \
  --output-format json \
  --permission-mode bypassPermissions \
  --max-turns <max_turns> \
  --max-budget-usd <budget> \
  --no-session-persistence \
  --strict-mcp-config \
  <isolation flag>
```

- Isolation flag: `--safe-mode` by default (disables CLAUDE.md, skills, plugins, hooks, MCP,
  auto memory; keeps the user's login). `--bare` when `CRUCIBLE_ISOLATION=bare`; it is stricter
  but never reads OAuth/keychain, so it needs `ANTHROPIC_API_KEY`.
- `--max-budget-usd` caps one attempt's spend, subagents included; exceeding it ends the run
  with subtype `error_max_budget_usd`. Crucible always passes it (default 5).
- Never pass `--fallback-model`; a run must not silently switch models.
- Always pass `--effort` explicitly. Model defaults differ (medium for Opus 5.5 and Sonnet 5.5,
  high for others), so omitting it would make configs incomparable.
- Effort values: `low`, `medium`, `high`, `xhigh`, `max`. Not every model supports `xhigh`.
  Behavior on an unsupported level is undocumented: validate against `pricing.yaml`'s
  per-model `efforts` list before running.
- `--model` accepts aliases (`opus`, `sonnet`, `haiku`) and full IDs. Aliases float to the
  latest model, so the record stores the resolved model ID (see below).

**Safety:** `bypassPermissions` lets the agent run any shell command as you. The workspace
isolates files Crucible tracks, not your machine. Only benchmark tasks you trust.

## Output (`--output-format json`)

One result object on stdout. Fields Crucible reads:

| Field | Use |
|-------|-----|
| `result` | the assistant's final reply text (success only); the judge parses its verdict from this |
| `subtype` | `success`, `error_max_turns`, `error_during_execution`, `error_max_budget_usd`, ... |
| `is_error` | error flag |
| `num_turns` | turn count |
| `duration_ms`, `duration_api_ms` | stored as reported; Crucible measures its own wall time |
| `total_cost_usd` | stored as `cost_usd.reported` (client-side estimate, includes subagents) |
| `modelUsage` | map of model ID → `{inputTokens, outputTokens, cacheReadInputTokens, cacheCreationInputTokens, costUSD, ...}` |
| `session_id` | stored for traceability |

- **Tokens come from `modelUsage`, summed across models.** Top-level `usage` covers the main
  loop only and undercounts when subagents run. `outputTokens` already includes thinking tokens.
- The resolved model is the `modelUsage` key with the most output tokens.
- After a crash (`error_during_execution`), cost and usage may be zero; record them as reported
  and mark the attempt `error`.
- Exit code 0 on success, non-zero on failure, 143 on SIGTERM.
