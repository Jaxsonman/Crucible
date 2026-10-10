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

## Adding a preset

A preset teaches `crucible mine` how to start for one test runner. It is one YAML file plus one
test case; no Python changes.

1. Create `src/crucible/data/presets/<name>.yaml` (the file name is the preset name). Copy the
   closest existing preset. Fields are documented in `src/crucible/mine/presets.py` and
   `docs/design.md` ("Presets and detection"):
   - `markers`: how to recognise the runner (`glob` for files in the directory itself, optional
     `contains` regex). Add `aggregator: true` to the marker of a multi-module root.
   - `evidence`: add it when the marker alone does not prove tests exist (build files); a
     marker can carry its own `evidence` globs. `contains` regexes must ignore commented lines
     (`(?m)^\s*gem\s+['"]rspec`).
   - `test_paths` (include fixtures and resources), `runnable` (the real file naming of that
     ecosystem; omit it only for the built-in Python/JS rule), `setup`, `test_command`.
   - Write the command from the runner's documented CLI and justify it in a comment at the top
     of the file; `notes` says what to watch for. Use `{suite_dir}` so globs and `module:` roots
     are right for a suite in a sub-directory. `variants` switch on wrapper scripts or lockfiles
     (`ancestors: true` also looks in parent directories, for a workspace-root lockfile).
   - `runnable` and `test_paths` accept `!pattern` exclusions (a snapshot directory, helpers).
     `setup` may be a list of conditional steps (`{run, when, contains, unless}` or `{first: [...]}`)
     joined into one command at detection time. Set `fallback: true` only for a generic
     whole-suite runner that must apply when no framework preset matched.
2. Add a case to `CASES` in `tests/test_mine_presets.py`: a tiny fixture layout, the expected
   suite name, `cwd` and `test_command`, which paths are runnable, and the command rendered for
   sample test files. `test_shipped_presets_load_and_validate` already loads every preset, and a
   malformed one fails with `<file>: <field>: ...`.
3. Run `uv run pytest -q tests/test_mine_presets.py` and `uv run ruff check .`.

## Adding a calibration hint

`crucible mine calibrate` explains failures from `src/crucible/data/diagnostics.yaml`. To teach it
a new log pattern, add a row (`id`, `category`, `pattern` and/or `exit`, `hint` naming the
`mine.yaml` field to change; `status: no-tests-ran` for a "runner found no tests" message) and a
sample log for its `id` in `SAMPLES` in `tests/test_mine_diagnostics.py`. A test fails for a row
without a sample.

## Pull requests

1. Fork the repository and create a branch (`feat/...`, `fix/...`, `docs/...`, `chore/...`).
2. Make focused changes with tests; keep unrelated cleanups in separate pull requests.
3. Bump the version (see below).
4. Run `uv run pytest -q` and `uv run ruff check .` locally.
5. Open a pull request against `main`. CI must pass, and a maintainer review is required to merge.

Commit messages: a short imperative summary line (for example `Fix cost rounding in report`),
optionally followed by a body explaining why.

## Releases and versioning

Crucible follows [Semantic Versioning](https://semver.org/). **Every pull request raises the
version**, and CI (the `version` job) fails when it does not. Merging to `main` is the release.

The public interface is the CLI commands and flags and their output, the formats of tasks,
configs, `mine.yaml` and `record.json` (`schema_version`), and the plugin skills. Choose the bump
by the most significant change in the pull request:

| Bump  | When                                                                        |
| ----- | --------------------------------------------------------------------------- |
| major | A breaking change to the public interface: removing or renaming a command, flag or field, changing a file format or the meaning of a result so existing tasks, configs or records no longer work or compare. |
| minor | A backwards-compatible addition: a command, flag, preset, diagnostic or field. |
| patch | Everything else: bug fixes, performance, docs, tests, CI and refactors.      |

To bump (replace `X.Y.Z`):

1. Set `version = "X.Y.Z"` in `pyproject.toml`. This is the only source; `crucible --version` and
   `crucible.__version__` read it from the installed package.
2. Set the same version in `.claude-plugin/plugin.json`.
3. Run `uv lock` so `uv.lock` records it.
4. Add a `## [X.Y.Z] - YYYY-MM-DD` section at the top of `CHANGELOG.md` (below the intro, above
   older releases) listing what changed under `### Added`, `### Changed`, `### Fixed` or
   `### Removed`. Say what a user sees, and mark breaking changes.

`python scripts/check_version.py [BASE_REF]` runs the same check as CI: the four places agree, the
changelog has the section, and with `BASE_REF` (for example `origin/main`) the version is higher
than that branch's. When another pull request merges first and takes your version, rebase, pick
the next version and move your changelog section.

The changelog section must be the newest one in the file, dated, and not empty.

Pull requests opened by Dependabot are exempt from the check, so they merge without a release. A
maintainer who wants one pushes a bump commit to the Dependabot branch, or bumps in the next
pull request. After merging, a maintainer may tag the release (`git tag vX.Y.Z`).

## Do not commit secrets or personal data

Tasks, results, logs and docs must not contain API keys, tokens, personal paths, email addresses
or details of private repositories. Check `results/` records and task fixtures before committing.

## Security issues

Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md), not in public issues.
