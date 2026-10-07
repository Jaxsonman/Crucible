# Crucible: contributor and agent instructions

- Shared decisions live in `docs/design.md`; update it when a change alters one. See
  `CONTRIBUTING.md` for the pull request process.
- Commands:
  - `uv sync`
  - `uv run pytest`
  - `uv run ruff check .`
  - `uv run crucible --help`
- Work test-first: write a failing test, then the code. Tests must use the temp
  `CRUCIBLE_HOME` fixture (autouse in `tests/conftest.py`) and never write into the real
  `tasks/`, `results/` or `.crucible/`. Tests never call the real `claude` or `gh`; use
  `tests/fake_claude.py` and `tests/fake_gh.py`.
- Keep secrets, personal paths and private repository details out of tasks, results and docs.
