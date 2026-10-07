# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately using GitHub private vulnerability reporting: open the
repository's **Security** tab and choose **Report a vulnerability**. Do not open a public issue
for a security problem. You can expect an acknowledgement within a few days.

## Supported versions

Security fixes are made on the latest `main` and released in the latest release. Older releases
are not patched.

## Security model

Crucible runs code and a coding agent on your machine on purpose. It is a benchmarking tool, not
a sandbox. Read this before running it on anything you do not trust.

- **Attempts run with `--permission-mode bypassPermissions`.** Each benchmark attempt runs
  `claude -p` (see `src/crucible/executor.py`) with all permission prompts disabled, so the agent
  can run any command as your user and read or write any absolute path. The per-attempt workspace
  is a copy of the repo that keeps attempts apart from each other; it is not an isolation
  boundary. Hidden tests and checks live outside the workspace and are reachable by absolute
  path, so an agent that goes looking can find them. The subprocess inherits your environment,
  including any API keys.
- **Checks run arbitrary shell.** A task's `check` script (`check.sh`, run by
  `src/crucible/checker.py`) runs as your user inside the finished workspace, with your
  environment variables (`CRUCIBLE_TASK_DIR` is added). Mining does the same: each suite's
  `setup` and `test_command` from `.crucible/mine.yaml` are run with `bash -c`
  (`src/crucible/mine/verify.py`), and the generated `check.sh` of a mined task re-runs them
  (`src/crucible/mine/scaffold.py`), all with your environment.
- **Mining installs the target repository's dependencies.** The `setup` command (for example
  `pip install` or `npm ci`) executes install scripts from the repository being mined.
- Values of secret-looking environment variables are redacted from records Crucible writes under
  `results/`, but that does not stop an agent or a check from reading or exfiltrating them while it runs.

### Recommendations

- Only run Crucible on repositories, tasks and `mine.yaml` files you trust.
- Prefer a VM, container or disposable dev box, especially for third-party code.
- Unset sensitive environment variables (cloud credentials, tokens) before running, and avoid
  running with credentials that can reach production.
- Always set `--max-budget-usd` to cap the spend of each attempt.
- Review `check.sh`, `mine.yaml` `setup` and `test_command` lines before the first run.
