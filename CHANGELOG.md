# Changelog

All notable changes to Crucible are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow
[Semantic Versioning](https://semver.org/); the rules are in
[CONTRIBUTING.md](CONTRIBUTING.md#releases-and-versioning). Every pull request adds a section for
its own version, newest first.

## [1.0.0] - 2026-10-10

First stable release. The CLI commands and flags, the task, config and `record.json` formats and
the plugin skills are now the public interface, and changes to them follow Semantic Versioning.

### Added

- Versioning process: one version in `pyproject.toml`, this changelog, and a CI check that every
  pull request raises the version and records it here.

### Changed

- Mining works in any language and framework (#4).
- Benchmark validity fixes: criteria parsing, the success rule, intervals, paired comparison and
  regression checks (#3).
- `crucible.__version__` is read from the installed package metadata instead of a second
  hardcoded copy.
