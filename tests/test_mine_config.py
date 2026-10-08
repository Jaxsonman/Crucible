import json
import os
import subprocess
from pathlib import Path

import pytest

from crucible import paths
from crucible.cli import main
from crucible.mine import config as mc

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
}


def write(root: Path, rel: str, text: str = "") -> None:
    f = root / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text, encoding="utf-8")


def pkg(deps: str) -> str:
    return json.dumps({"devDependencies": {deps: "^1.0.0"}})


def by_name(suites):
    return {s.name: s for s in suites}


# --- detection ---

def test_detect_django(tmp_path):
    write(tmp_path, "manage.py")
    write(tmp_path, "requirements.txt")
    [s] = mc.detect_suites(tmp_path)
    assert s.name == "django"
    assert s.test_command == "python manage.py test {modules}"
    assert s.module_root == "."
    assert s.setup == "pip install -q -r requirements.txt"
    assert s.cwd == "."
    assert mc.path_matches("app/tests/test_x.py", s.test_paths)


def test_detect_pytest_pyproject(tmp_path):
    write(tmp_path, "pyproject.toml", "[tool.pytest.ini_options]\ntestpaths = ['tests']\n")
    [s] = mc.detect_suites(tmp_path)
    assert s.name == "pytest"
    assert s.test_command == "python -m pytest {tests}"
    assert s.setup == "pip install -q -e ."
    assert mc.path_matches("tests/test_a.py", s.test_paths)
    assert not mc.path_matches("src/a.py", s.test_paths)


def test_detect_pytest_ini_and_conftest(tmp_path):
    write(tmp_path / "a", "pytest.ini", "[pytest]\n")
    write(tmp_path / "b", "conftest.py")
    assert [(s.name, s.cwd) for s in mc.detect_suites(tmp_path)] == [("a", "a"), ("b", "b")]


def test_conftest_only_subdir_is_not_its_own_project(tmp_path):
    write(tmp_path, "pytest.ini", "[pytest]\n")
    write(tmp_path, "requirements.txt")
    write(tmp_path, "tests/conftest.py")
    [s] = mc.detect_suites(tmp_path)
    assert (s.name, s.cwd, s.setup) == ("pytest", ".", "pip install -q -r requirements.txt")


def test_conftest_in_subdir_with_bare_pyproject_gives_root_suite(tmp_path):
    write(tmp_path, "pyproject.toml", "[project]\nname = 'x'\n")
    write(tmp_path, "tests/conftest.py")
    [s] = mc.detect_suites(tmp_path)
    assert (s.name, s.cwd, s.setup) == ("pytest", ".", "pip install -q -e .")
    assert mc.path_matches("tests/test_a.py", s.test_paths)


def test_detect_vitest_one_level_down(tmp_path):
    write(tmp_path, "frontend/package.json", pkg("vitest"))
    write(tmp_path, "frontend/package-lock.json", "{}")
    [s] = mc.detect_suites(tmp_path)
    assert (s.name, s.cwd) == ("frontend", "frontend")
    assert s.test_command == "npx vitest run {tests}"
    assert s.setup == "npm ci --silent"
    assert mc.path_matches("frontend/src/a.test.ts", s.test_paths)
    assert mc.path_matches("frontend/src/__tests__/a.ts", s.test_paths)


def test_detect_jest_without_lockfile(tmp_path):
    write(tmp_path, "package.json", pkg("jest"))
    [s] = mc.detect_suites(tmp_path)
    assert s.name == "jest"
    assert s.test_command == "npx jest {tests}"
    assert s.setup == "npm install --silent"


def test_detect_django_plus_vitest_gives_two_suites(tmp_path):
    write(tmp_path, "backend/manage.py")
    write(tmp_path, "backend/requirements.txt")
    write(tmp_path, "frontend/package.json", pkg("vitest"))
    suites = by_name(mc.detect_suites(tmp_path))
    assert set(suites) == {"backend", "frontend"}
    assert suites["backend"].module_root == "backend"
    assert suites["backend"].cwd == "backend"
    assert suites["frontend"].test_command.startswith("npx vitest")


def test_detect_ignores_node_modules_and_dot_dirs(tmp_path):
    write(tmp_path, "node_modules/x/package.json", pkg("jest"))
    write(tmp_path, ".venv/manage.py")
    assert mc.detect_suites(tmp_path) == []


def test_detect_nothing_writes_template(tmp_path):
    out = tmp_path / "mine.yaml"
    assert mc.write_default(out, tmp_path) == []
    text = out.read_text()
    assert text.startswith("# NOTHING DETECTED")
    with pytest.raises(mc.MineConfigError, match="suites"):
        mc.load_config(out)


def test_written_default_loads_and_roundtrips(tmp_path):
    write(tmp_path, "backend/manage.py")
    write(tmp_path, "frontend/package.json", pkg("vitest"))
    out = tmp_path / "mine.yaml"
    suites = mc.write_default(out, tmp_path)
    assert out.read_text().startswith("# AUTO-DETECTED")
    cfg = mc.load_config(out)
    assert cfg.suites == suites
    assert cfg.max_changed_lines == 800
    assert cfg.exclude_paths == mc.DEFAULT_EXCLUDE
    with pytest.raises(FileExistsError):
        mc.write_default(out, tmp_path)


# --- validation ---

GOOD = """
suites:
  - name: backend
    test_paths: ["backend/**/test_*.py"]
    cwd: backend
    setup: pip install -q -r requirements.txt
    test_command: python manage.py test {modules}
    module_root: backend
    env: {DJANGO_SECRET_KEY: dummy-not-secret}
exclude_paths: ["docs/**"]
max_changed_lines: 50
"""


def load(tmp_path, text):
    f = tmp_path / "mine.yaml"
    f.write_text(text)
    return mc.load_config(f)


def errors_of(tmp_path, text):
    with pytest.raises(mc.MineConfigError) as e:
        load(tmp_path, text)
    return e.value.errors


def test_good_config_loads(tmp_path):
    cfg = load(tmp_path, GOOD)
    assert cfg.suites[0].env == {"DJANGO_SECRET_KEY": "dummy-not-secret"}
    assert cfg.exclude_paths == ["docs/**"]
    assert cfg.max_changed_lines == 50


@pytest.mark.parametrize("old,new,needle", [
    ("    test_command: python manage.py test {modules}\n", "", "suites[0].test_command: required"),
    ("{modules}", "{Modules}", "suites[0].test_command: unknown placeholder {Modules}"),
    ("{modules}", "{ modules }", "suites[0].test_command: whitespace inside placeholder"),
    ("    module_root: backend\n", "", "{modules} needs suites[0].module_root"),
    ("dummy-not-secret", "realvalue123", "suites[0].env.DJANGO_SECRET_KEY"),
    ("    cwd: backend\n", "    cwd: ../x\n", "suites[0].cwd"),
    ("    test_paths: [\"backend/**/test_*.py\"]\n", "", "suites[0].test_paths: required"),
    ("max_changed_lines: 50", "max_changed_lines: lots", "max_changed_lines"),
    ("  - name: backend\n", "  - name: ../b\n", "suites[0].name: must match"),
    ("  - name: backend\n", "  - name: 'a\"; echo x; \"'\n", "suites[0].name: must match"),
    ("  - name: backend\n", "  - name: back+front\n", "suites[0].name: must match"),
])
def test_invalid_configs_name_the_field(tmp_path, old, new, needle):
    assert old in GOOD
    assert any(needle in e for e in errors_of(tmp_path, GOOD.replace(old, new)))


def test_unknown_field_rejected(tmp_path):
    text = GOOD.replace("    env:", "    cwdd: x\n    env:")
    assert any("suites[0].cwdd: unknown field" in e for e in errors_of(tmp_path, text))


def test_non_string_keys_are_validation_errors(tmp_path):
    text = GOOD.replace("    env:", "    5: x\n    zzz: 1\n    env:") + "7: y\nextra: 1\n"
    errs = errors_of(tmp_path, text)
    assert any("suites[0].5: unknown field" in e for e in errs)
    assert any(e.startswith("7: unknown field") for e in errs)
    # a non-string env name is stringified, not a crash
    env = GOOD.replace("DJANGO_SECRET_KEY: dummy-not-secret", "3: a")
    assert load(tmp_path, env).suites[0].env == {"3": "a"}


@pytest.mark.parametrize("name", ["DB_PASSWD", "GITHUB_PAT", "SSH_PRIVATE_KEY_X", "AWS_CREDENTIALS"])
def test_more_secret_like_names(tmp_path, name):
    text = GOOD.replace("DJANGO_SECRET_KEY: dummy-not-secret", f"{name}: realvalue123")
    assert any(f"env.{name}" in e for e in errors_of(tmp_path, text))


def test_path_like_names_are_not_secrets(tmp_path):
    text = GOOD.replace("DJANGO_SECRET_KEY: dummy-not-secret", "PATH: /usr/local/bin:/usr/bin")
    assert load(tmp_path, text).suites[0].env["PATH"].startswith("/usr")


def test_url_with_embedded_credentials(tmp_path):
    bad = GOOD.replace("DJANGO_SECRET_KEY: dummy-not-secret",
                       "DATABASE_URL: postgres://admin:hunter2@db.example.com/x")
    assert any("env.DATABASE_URL" in e for e in errors_of(tmp_path, bad))
    ok = GOOD.replace("DJANGO_SECRET_KEY: dummy-not-secret",
                      "DATABASE_URL: postgres://admin:dummypw@localhost/x, PLAIN: http://localhost:8000/x")
    assert load(tmp_path, ok).suites[0].env["PLAIN"] == "http://localhost:8000/x"


def test_short_or_dummy_secrets_allowed(tmp_path):
    text = GOOD.replace("dummy-not-secret", "abc")
    assert load(tmp_path, text).suites[0].env["DJANGO_SECRET_KEY"] == "abc"
    text = GOOD.replace("DJANGO_SECRET_KEY: dummy-not-secret", "API_TOKEN: dummy123456789, MODE: realvalue123")
    assert load(tmp_path, text).suites[0].env["MODE"] == "realvalue123"


def test_command_without_placeholder_runs_the_whole_suite(tmp_path):
    # changed on purpose: a placeholder used to be required, now it is optional
    text = GOOD.replace("test {modules}", "test")
    assert load(tmp_path, text).suites[0].test_command == "python manage.py test"


def test_duplicate_suite_names_and_not_a_mapping(tmp_path):
    block = GOOD.split("suites:\n")[1].split("exclude_paths")[0]
    text = "suites:\n" + block + block + "exclude_paths: []\n"
    assert any("duplicate name" in e for e in errors_of(tmp_path, text))
    assert any("mapping" in e for e in errors_of(tmp_path, "- a\n- b\n"))


def test_suite_settings_hash_changes_with_settings(tmp_path):
    a = load(tmp_path, GOOD).suites[0]
    b = load(tmp_path, GOOD.replace("pip install -q", "pip install")).suites[0]
    assert a.settings_hash() == load(tmp_path, GOOD).suites[0].settings_hash()
    assert a.settings_hash() != b.settings_hash()


def test_path_matches():
    assert mc.path_matches("a/b/package-lock.json", ["**/package-lock.json"])
    assert mc.path_matches("package-lock.json", ["**/package-lock.json"])
    assert mc.path_matches("x/y/README.md", ["*.md"])
    assert mc.path_matches("docs/a/b.txt", ["docs/**"])
    assert not mc.path_matches("src/docs/a.txt", ["docs/**"])
    assert not mc.path_matches("a/b.py", ["*.md"])
    assert not mc.path_matches("backend/x/y/test_a.py", ["backend/*/test_*.py"])


def test_path_matches_directory_patterns():
    assert mc.path_matches("docs/a/b.txt", ["docs/"])
    assert mc.path_matches("src/docs/a.txt", ["docs/"])  # a slash-less name is a dir at any depth
    assert mc.path_matches("src/utils/a.py", ["src/utils"])
    assert mc.path_matches("src/utils/a.py", ["src/utils/"])
    assert not mc.path_matches("src/utils2/a.py", ["src/utils"])
    assert mc.path_matches("a/vendor/b/c.py", ["vendor"])  # bare name: a directory at any depth
    assert mc.path_matches("vendor/c.py", ["vendor"])
    assert mc.path_matches("a/vendor", ["vendor"])  # or a file of that name
    assert not mc.path_matches("a/vendored/c.py", ["vendor"])


def test_ensure_gitignored_ignores_comments(tmp_path):
    (tmp_path / ".gitignore").write_text("# mine/\nresults/\n")
    assert mc.ensure_gitignored(tmp_path) is True
    assert (tmp_path / ".gitignore").read_text() == "# mine/\nresults/\nmine/\n"


def test_ensure_gitignored(tmp_path):
    assert mc.ensure_gitignored(tmp_path) is True
    assert (tmp_path / ".gitignore").read_text() == "mine/\n"
    assert mc.ensure_gitignored(tmp_path) is False
    (tmp_path / ".gitignore").write_text("results/")
    mc.ensure_gitignored(tmp_path)
    assert (tmp_path / ".gitignore").read_text() == "results/\nmine/\n"


# --- CLI ---

@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.delenv("CRUCIBLE_HOME")
    r = tmp_path / "proj"
    r.mkdir()
    env = {**os.environ, **GIT_ENV}
    subprocess.run(["git", "init", "-q"], cwd=r, check=True, env=env)
    write(r, "f.txt", "x\n")
    subprocess.run(["git", "add", "."], cwd=r, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=r, check=True, env=env)
    r = r.resolve()
    monkeypatch.chdir(r)
    return r


def test_init_gitignore_has_mine(repo):
    assert main(["init"]) == 0
    assert (repo / ".crucible/.gitignore").read_text().split() == ["results/", "mine/"]


def test_first_mine_detects_writes_and_second_keeps(repo, capsys):
    write(repo, "manage.py")
    write(repo, "frontend/package.json", pkg("vitest"))
    assert main(["init"]) == 0
    capsys.readouterr()
    assert main(["mine"]) == 0
    out = capsys.readouterr().out
    f = repo / ".crucible/mine.yaml"
    assert str(f) in out and "review it, then run `crucible mine` again" in out
    assert f.read_text().startswith("# AUTO-DETECTED")
    assert len(mc.load_config(f).suites) == 2
    before = f.read_text()
    f.write_text(before.replace("max_changed_lines: 800", "max_changed_lines: 7"))
    assert main(["mine"]) == 0
    assert "max_changed_lines: 7" in f.read_text()
    assert "2 suite(s)" in capsys.readouterr().out


def test_mine_nothing_detected_says_so(repo, capsys):
    main(["init"])
    capsys.readouterr()
    assert main(["mine"]) == 0
    assert "no test stack detected" in capsys.readouterr().out
    assert main(["mine"]) == 1  # empty template is not a valid config yet
    assert "suites" in capsys.readouterr().err


def test_mine_invalid_config_exits_1_naming_field(repo, capsys):
    main(["init"])
    (repo / ".crucible/mine.yaml").write_text(
        "suites:\n  - name: a\n    test_paths: ['t/**']\n"
        "    test_command: run {Tests}\n"
    )
    capsys.readouterr()
    assert main(["mine"]) == 1
    assert "suites[0].test_command: unknown placeholder {Tests}" in capsys.readouterr().err


def test_mine_adds_mine_to_existing_home_gitignore(repo):
    main(["init"])
    (repo / ".crucible/.gitignore").write_text("results/\n")
    main(["mine"])
    assert (repo / ".crucible/.gitignore").read_text() == "results/\nmine/\n"


def test_mine_with_crucible_home_env_uses_tmp_home(tmp_path, monkeypatch, crucible_home, capsys):
    # CRUCIBLE_HOME (autouse fixture) points at a temp dir: mine.yaml goes there.
    r = tmp_path / "proj2"
    r.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=r, check=True)
    write(r, "manage.py")
    monkeypatch.chdir(r)
    assert main(["mine"]) == 0
    assert (crucible_home / "mine.yaml").is_file()
    assert not (r / ".crucible").exists()
    assert paths.home() == crucible_home


def test_detected_suite_names_are_safe(tmp_path):
    d = tmp_path / "my app (web)"
    d.mkdir()
    (d / "package.json").write_text('{"devDependencies": {"vitest": "1"}}')
    names = [s.name for s in mc.detect_suites(tmp_path)]
    assert names and all(mc.SUITE_NAME_RE.match(n) for n in names)


# --- brace alternation in globs ---

@pytest.mark.parametrize("path,patterns,expected", [
    ("a/x.test.ts", ["a/*.{test,spec}.{ts,js}"], True),
    ("a/x.spec.js", ["a/*.{test,spec}.{ts,js}"], True),
    ("a/x.spec.py", ["a/*.{test,spec}.{ts,js}"], False),
    ("src/Foo.vue", ["**/*.{vue,svelte}"], True),
    ("web/src/b.svelte", ["**/*.{vue,svelte}"], True),
    ("docs/a.txt", ["{docs,vendor}/**"], True),
    ("vendor/a.txt", ["{docs,vendor}/**"], True),
    ("x/a.txt", ["{docs,vendor}/**"], False),
    ("a.b", ["a.{b,{c,d}}"], True),
    ("a.d", ["a.{b,{c,d}}"], True),
    ("a.e", ["a.{b,{c,d}}"], False),
    ("a{b}.py", ["a{b}.py"], True),          # no comma: literal braces
    ("a{b.py", ["a{b.py"], True),            # unclosed: literal
    ("deep/vendor/x", ["{vendor,node_modules}"], True),  # directory pattern still matches
])
def test_path_matches_brace_alternation(path, patterns, expected):
    assert mc.path_matches(path, patterns) is expected
