import importlib.util
import json
import subprocess

import pytest
from conftest import REPO_ROOT

import crucible

_spec = importlib.util.spec_from_file_location(
    "check_version", REPO_ROOT / "scripts" / "check_version.py"
)
check_version = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_version)


def make_tree(root, version="1.2.3", *, plugin=None, lock=None, changelog=None):
    (root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(f'[project]\nname = "crucible"\nversion = "{version}"\n')
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "crucible", "version": plugin or version})
    )
    (root / "uv.lock").write_text(
        'version = 1\n\n[[package]]\nname = "pyyaml"\nversion = "6.0"\n\n'
        f'[[package]]\nname = "crucible"\nversion = "{lock or version}"\nsource = {{ editable = "." }}\n'
    )
    (root / "CHANGELOG.md").write_text(
        changelog or f"# Changelog\n\n## [{version}] - 2026-10-10\n\n- Something.\n"
    )
    return root


def test_repo_version_is_consistent():
    # pyproject.toml is the source; the package, plugin manifest, lockfile and changelog follow it.
    assert check_version.problems(REPO_ROOT) == []
    assert crucible.__version__ == check_version.project_version(REPO_ROOT)


def test_repo_is_at_least_1_0_0():
    assert check_version.parse(crucible.__version__) >= (1, 0, 0)


@pytest.mark.parametrize(
    "text, expected",
    [("1.2.3", (1, 2, 3)), ("10.0.21", (10, 0, 21))],
)
def test_parse_accepts_plain_semver(text, expected):
    assert check_version.parse(text) == expected


@pytest.mark.parametrize("text", ["1.2", "1.2.3.4", "v1.2.3", "1.2.3-rc1", "01.2.3", "1.2.x", ""])
def test_parse_rejects_everything_else(text):
    with pytest.raises(ValueError):
        check_version.parse(text)


def test_consistent_tree_has_no_problems(tmp_path):
    assert check_version.problems(make_tree(tmp_path)) == []


def test_plugin_manifest_mismatch(tmp_path):
    out = check_version.problems(make_tree(tmp_path, plugin="1.2.0"))
    assert len(out) == 1 and "plugin.json" in out[0] and "1.2.0" in out[0]


def test_lockfile_mismatch(tmp_path):
    out = check_version.problems(make_tree(tmp_path, lock="1.2.0"))
    assert len(out) == 1 and "uv.lock" in out[0] and "uv lock" in out[0]


def test_changelog_needs_a_dated_section_for_the_version(tmp_path):
    out = check_version.problems(
        make_tree(tmp_path, changelog="# Changelog\n\n## [1.2.2] - 2026-10-09\n")
    )
    assert len(out) == 1 and "CHANGELOG.md" in out[0] and "1.2.3" in out[0]
    out = check_version.problems(make_tree(tmp_path, changelog="# Changelog\n\n## [1.2.3]\n"))
    assert len(out) == 1 and "CHANGELOG.md" in out[0]


def test_invalid_version_is_reported_once(tmp_path):
    out = check_version.problems(make_tree(tmp_path, version="1.2"))
    assert len(out) == 1 and "MAJOR.MINOR.PATCH" in out[0]


@pytest.mark.parametrize("base", ["1.2.2", "1.1.9", "0.9.9"])
def test_bump_over_base_passes(tmp_path, base):
    assert check_version.problems(make_tree(tmp_path), base=base) == []


@pytest.mark.parametrize("base", ["1.2.3", "1.2.4", "2.0.0"])
def test_unchanged_or_lower_version_fails(tmp_path, base):
    out = check_version.problems(make_tree(tmp_path), base=base)
    assert len(out) == 1 and "bump" in out[0] and base in out[0]


def test_ten_is_greater_than_nine(tmp_path):
    # Versions compare as numbers, not strings.
    assert check_version.problems(make_tree(tmp_path, version="1.10.0"), base="1.9.0") == []


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_main_compares_against_the_base_ref(tmp_path, capsys):
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.name", "t")
    git(tmp_path, "config", "user.email", "t@example.com")
    make_tree(tmp_path, version="1.0.0")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "base")

    assert check_version.main(["main"], root=tmp_path) == 1
    assert "bump" in capsys.readouterr().err

    make_tree(tmp_path, version="1.0.1")
    assert check_version.main(["main"], root=tmp_path) == 0

    assert check_version.main([], root=tmp_path) == 0  # no base: consistency only


def test_main_fails_cleanly_on_unknown_base(tmp_path, capsys):
    git(tmp_path, "init", "-q", "-b", "main")
    make_tree(tmp_path)
    assert check_version.main(["no-such-ref"], root=tmp_path) == 1
    assert "no-such-ref" in capsys.readouterr().err


def changelog_problems(tmp_path, text):
    return check_version.problems(make_tree(tmp_path, changelog=text))


@pytest.mark.parametrize(
    "text",
    [
        # the version's section must be the newest one, not buried under another
        "# Changelog\n\n## [1.2.2] - 2026-10-09\n\n- Old.\n\n## [1.2.3] - 2026-10-10\n\n- New.\n",
        # a heading inside a code fence is not a section
        "# Changelog\n\n## [1.2.2] - 2026-10-09\n\n- Old.\n\n```\n## [1.2.3] - 2026-10-10\n```\n",
        # impossible date
        "# Changelog\n\n## [1.2.3] - 9999-99-99\n\n- New.\n",
        # empty section
        "# Changelog\n\n## [1.2.3] - 2026-10-10\n\n## [1.2.2] - 2026-10-09\n\n- Old.\n",
    ],
)
def test_changelog_top_section_must_be_the_versions_and_filled_in(tmp_path, text):
    out = changelog_problems(tmp_path, text)
    assert len(out) == 1 and "CHANGELOG.md" in out[0]


def test_changelog_tolerates_trailing_whitespace_and_crlf(tmp_path):
    text = "# Changelog\r\n\r\n## [1.2.3] - 2026-10-10 \r\n\r\n- New.\r\n"
    assert changelog_problems(tmp_path, text) == []


def test_missing_or_malformed_files_are_reported_not_raised(tmp_path, capsys):
    make_tree(tmp_path)
    (tmp_path / ".claude-plugin" / "plugin.json").write_text("{")
    assert check_version.main([], root=tmp_path) == 1
    assert "plugin.json" in capsys.readouterr().err

    (tmp_path / ".claude-plugin" / "plugin.json").write_text("{}")
    assert check_version.main([], root=tmp_path) == 1
    assert "plugin.json" in capsys.readouterr().err

    make_tree(tmp_path)
    (tmp_path / "CHANGELOG.md").unlink()
    assert check_version.main([], root=tmp_path) == 1
    assert "CHANGELOG.md" in capsys.readouterr().err
