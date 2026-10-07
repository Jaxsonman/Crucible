# Contributing to Crucible

Thanks for helping. Crucible is a Python CLI and Claude Code plugin; this page covers the
development loop and the pull request process. Please also read the
[Code of Conduct](CODE_OF_CONDUCT.md).

## Setup

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/Jaxsonman/Crucible && cd Crucible
uv sync
uv run pytest
uv run ruff check .
uv run crucible --help
```

## Tests

- Work test-first: write a failing test, then the code that makes it pass.
- Tests must use the temp `CRUCIBLE_HOME` fixture (autouse in `tests/conftest.py`) and must never
  write into the repo's real `tasks/`, `results/` or `.crucible/`.
- Tests must never call the real `claude` or `gh` (they cost money and need credentials). Use the
  fakes in `tests/fake_claude.py` and `tests/fake_gh.py`.
- Design decisions shared across the codebase live in `docs/design.md`. If your change alters one,
  update that file in the same pull request.

## Testing the plugin locally

```bash
claude --plugin-dir .        # load this checkout as the plugin
claude plugin validate .     # validate the manifests and skills
```

The plugin shim in `bin/crucible` runs the CLI through `uv`; see `docs/plugin.md`.

## Pull requests

1. Fork the repository and create a branch (`feat/...`, `fix/...`, `docs/...`, `chore/...`).
2. Make focused changes with tests; keep unrelated cleanups in separate pull requests.
3. Run `uv run pytest -q` and `uv run ruff check .` locally.
4. Open a pull request against `main`. CI must pass, and a maintainer review is required to merge.

Commit messages: a short imperative summary line (for example `Fix cost rounding in report`),
optionally followed by a body explaining why.

## Do not commit secrets or personal data

Tasks, results, logs and docs must not contain API keys, tokens, personal paths, email addresses
or details of private repositories. Check `results/` records and task fixtures before committing.

## Security issues

Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md), not in public issues.
