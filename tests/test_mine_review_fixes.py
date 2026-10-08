"""Fixes from the real-toolchain review of presets and detection: polyglot ownership, pytest and
JS package-manager detection, runnable rules, fallbacks, Python venv setup."""

import json
from types import SimpleNamespace

import pytest

from crucible.mine import candidates, presets, verify
from crucible.mine import config as mc


def write(root, rel, text=""):
    f = root / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text, encoding="utf-8")


def make(root, files):
    for rel, text in files.items():
        write(root, rel, text)


def pkg(*deps, scripts=None):
    d = {"devDependencies": {x: "^1.0.0" for x in deps}}
    if scripts:
        d["scripts"] = scripts
    return json.dumps(d)


def by_name(root):
    return {s.name: s for s in mc.detect_suites(root)}


def one(root):
    [s] = mc.detect_suites(root)
    return s


# --- 1. ownership in polyglot repos ---

OWNERS = {
    "cargo+pytest": (
        {"Cargo.toml": "[package]\n", "tests/it.rs": "", "pytest.ini": "[pytest]\n",
         "tests/test_a.py": ""},
        {"tests/test_a.py": "pytest", "tests/it.rs": "cargo"}),
    "phpunit+pytest": (
        {"phpunit.xml": "<phpunit/>", "tests/FooTest.php": "", "pytest.ini": "[pytest]\n",
         "tests/test_a.py": ""},
        {"tests/test_a.py": "pytest", "tests/FooTest.php": "phpunit"}),
    "minitest+mocha": (
        {"test/test_helper.rb": "", "test/a_test.rb": "", "package.json": pkg("mocha"),
         "test/a.test.js": ""},
        {"test/a.test.js": "mocha", "test/a_test.rb": "minitest"}),
    "dotnet+jest": (
        {"App.sln": "", "tests/FooTests.cs": "", "package.json": pkg("jest"),
         "tests/a.test.ts": ""},
        {"tests/a.test.ts": "jest", "tests/FooTests.cs": "dotnet"}),
}


@pytest.mark.parametrize("case", OWNERS)
def test_a_broad_glob_suite_does_not_steal_another_suites_tests(tmp_path, case):
    files, expected = OWNERS[case]
    make(tmp_path, files)
    suites = mc.detect_suites(tmp_path)
    for path, name in expected.items():
        assert candidates.owning_suite(path, suites) == name, path
        plan = verify.suite_plan_for(suites, [path], only_runnable=True)
        assert [(s.name, f) for s, f in plan] == [(name, [path])], path


def test_ownership_prefers_deepest_then_runnable_then_config_order():
    def suite(name, cwd, paths, runnable=None):
        return mc.Suite(name=name, cwd=cwd, test_paths=paths, test_command="x", runnable=runnable)

    wide = suite("wide", ".", ["**/*"], runnable=["*.nothing"])
    deep = suite("deep", "e2e", ["e2e/**"], runnable=["*.spec.ts"])
    run = suite("run", ".", ["**/*"], runnable=["*.py"])
    assert candidates.owning_suite("e2e/a.spec.ts", [wide, deep]) == "deep"
    assert candidates.owning_suite("e2e/a.spec.ts", [deep, wide]) == "deep"
    assert candidates.owning_suite("x/a.py", [wide, run]) == "run"
    # nothing runnable: the first matching suite (config order) keeps the helper
    assert candidates.owning_suite("x/data.json", [wide, run]) == "wide"
    assert candidates.owning_suite("x/data.json", [run, wide]) == "run"
    assert candidates.owning_suite("nothing.md", [deep]) is None


# --- 2. pytest detection ---

PYTEST_REPOS = {
    "pyproject-deps": {"pyproject.toml": '[project]\nname="x"\ndependencies = [\n  "pytest>=7",\n]\n'},
    "pyproject-optional": {"pyproject.toml": '[project.optional-dependencies]\ntest = ["pytest"]\n'},
    "pyproject-group": {"pyproject.toml": '[dependency-groups]\ndev = ["pytest", "ruff"]\n'},
    "pyproject-poetry": {"pyproject.toml": '[tool.poetry.group.dev.dependencies]\npytest = "^7"\n'},
    "requirements": {"requirements.txt": "requests\npytest==7.0\n"},
    "requirements-dev": {"requirements-dev.txt": "pytest\n"},
    "requirements-dir": {"requirements/test.txt": "pytest>=7\n"},
    "setup-cfg": {"setup.cfg": "[options]\ntests_require =\n    pytest\n"},
    "setup-py": {"setup.py": "setup(tests_require=['pytest'])\n"},
    "tox": {"tox.ini": "[testenv]\ncommands = pytest {posargs}\n"},
    "pipfile": {"Pipfile": '[dev-packages]\npytest = "*"\n'},
    "evidence-tests": {"pyproject.toml": '[project]\nname="x"\n', "tests/test_a.py": ""},
    "evidence-test": {"setup.py": "", "test/a_test.py": ""},
}


@pytest.mark.parametrize("case", PYTEST_REPOS)
def test_plain_pytest_repos_are_detected(tmp_path, case):
    make(tmp_path, PYTEST_REPOS[case])
    s = one(tmp_path)
    assert s.name == "pytest" and s.cwd == "."
    for path in ("tests/data/x.json", "test/conftest.py", "tests/helpers.py"):
        assert mc.path_matches(path, s.test_paths), path
    assert mc.path_matches("pkg/sub/test_x.py", s.test_paths)
    assert s.is_runnable("tests/test_a.py") and s.is_runnable("pkg/x_test.py")
    assert not s.is_runnable("tests/helpers.py")


@pytest.mark.parametrize("files", [
    {"pyproject.toml": '[project]\nname="x"\n'},
    {"requirements.txt": "# pytest later\nrequests\n"},
    {"tests/test_a.py": ""},  # no python project file
    {"pyproject.toml": '[project]\nname="x"\n', "src/test_a.py": ""},  # not under tests/
    {"tox.ini": "[testenv]\ncommands = nosetests\n"},
])
def test_no_pytest_evidence_no_suite(tmp_path, files):
    make(tmp_path, files)
    assert mc.detect_suites(tmp_path) == []


def test_django_with_pytest_listed_only_in_requirements_stays_django(tmp_path):
    make(tmp_path, {"manage.py": "", "requirements.txt": "django\npytest\n"})
    assert one(tmp_path).name == "django"


# --- 3/4. JS package managers and jest ---

LOCKS = {
    "pnpm": (["pnpm-lock.yaml"], "pnpm install --frozen-lockfile", "pnpm exec "),
    "yarn": (["yarn.lock"], "yarn install --frozen-lockfile", "yarn "),
    "yarn-berry": (["yarn.lock", ".yarnrc.yml"], "yarn install --immutable", "yarn "),
    "bun-lockb": (["bun.lockb"], "bun install --frozen-lockfile", "bunx "),
    "bun-lock": (["bun.lock"], "bun install --frozen-lockfile", "bunx "),
    "npm": (["package-lock.json"], "npm ci --silent", "npx "),
    "none": ([], "npm install --no-package-lock --silent", "npx "),
}
RUNNERS = {
    "jest": ("jest", "jest --runTestsByPath {tests}", ""),
    "vitest": ("vitest", "vitest run {tests}", ""),
    "mocha": ("mocha", "mocha {tests}", ""),
    "playwright": ("@playwright/test", "playwright test {tests}", " && {exec}playwright install"),
}


def js_repo(runner, locks, sub=""):
    dep = RUNNERS[runner][0]
    files = {f"{sub}package.json": pkg(dep)}
    if runner == "playwright":
        files[f"{sub}playwright.config.ts"] = ""
    for lock in locks:
        files[f"{sub}{lock}"] = ""
    return files


@pytest.mark.parametrize("lock", LOCKS)
@pytest.mark.parametrize("runner", RUNNERS)
def test_js_setup_and_exec_follow_the_lockfile(tmp_path, runner, lock):
    locks, setup, ex = LOCKS[lock]
    make(tmp_path, js_repo(runner, locks))
    s = one(tmp_path)
    assert s.name == runner
    _, cmd, extra = RUNNERS[runner]
    assert s.test_command == ex + cmd
    assert s.setup == setup + extra.replace("{exec}", ex)


def test_no_lockfile_setup_does_not_write_one():
    assert "--no-package-lock" in presets.shipped()["jest"].setup


def test_lockfile_at_the_workspace_root_runs_install_there(tmp_path):
    make(tmp_path, {"package.json": '{"workspaces": ["packages/*"]}', "pnpm-lock.yaml": "",
                    "packages/a/package.json": pkg("jest")})
    [s] = mc.detect_suites(tmp_path)
    assert (s.name, s.cwd) == ("a", "packages/a")
    assert s.setup == "cd ../.. && pnpm install --frozen-lockfile"
    assert s.test_command == "pnpm exec jest --runTestsByPath {tests}"


def test_lockfile_two_levels_up_and_yarn_berry_at_the_root(tmp_path):
    make(tmp_path, {"yarn.lock": "", ".yarnrc.yml": "", "apps/web/package.json": pkg("vitest")})
    [s] = mc.detect_suites(tmp_path)
    assert s.setup == "cd ../.. && yarn install --immutable"
    assert s.test_command == "yarn vitest run {tests}"


def test_nearest_lockfile_wins_over_an_ancestor_one(tmp_path):
    make(tmp_path, {"pnpm-lock.yaml": "", "web/package.json": pkg("jest"),
                    "web/package-lock.json": "{}"})
    [s] = mc.detect_suites(tmp_path)
    assert s.setup == "npm ci --silent" and s.test_command.startswith("npx jest")


def test_the_lockfile_search_stops_at_the_repo_root(tmp_path):
    make(tmp_path / "repo", {"web/package.json": pkg("jest")})
    write(tmp_path, "pnpm-lock.yaml")  # above the repo
    [s] = mc.detect_suites(tmp_path / "repo")
    assert s.setup == "npm install --no-package-lock --silent"


def test_jest_runs_by_literal_path():
    assert presets.shipped()["jest"].test_command == "npx jest --runTestsByPath {tests}"


# --- 5/6. snapshots, helpers, mocha ---

@pytest.mark.parametrize("runner", ["jest", "vitest"])
def test_js_runnable_excludes_snapshots_and_non_code(tmp_path, runner):
    make(tmp_path, {"package.json": pkg(runner)})
    s = one(tmp_path)
    for ok in ("src/a.test.ts", "src/b.spec.mjs", "src/__tests__/c.js", "src/__tests__/d/e.tsx",
               "a.test.cts"):
        assert s.is_runnable(ok), ok
    for bad in ("src/__tests__/__snapshots__/a.test.ts.snap", "src/__snapshots__/a.test.js.snap",
                "src/__tests__/data.json", "src/__tests__/README.md", "src/__tests__/a.png",
                "src/__tests__/__snapshots__/x.js", "src/a.test.js.snap", "src/a.ts"):
        assert not s.is_runnable(bad), bad
    assert mc.path_matches("src/__snapshots__/a.snap", s.test_paths)  # still a test file


def test_default_runnable_rule_rejects_non_code_under_tests_dirs():
    assert mc.default_is_runnable("src/__tests__/a.js")
    assert mc.default_is_runnable("a/test_b.py")
    for bad in ("x/__tests__/__snapshots__/a.snap", "x/__tests__/a.snap", "x/__tests__/a.json",
                "x/__tests__/a.md", "x/__tests__/a.png", "x/__tests__/__snapshots__/a.js"):
        assert not mc.default_is_runnable(bad), bad


def test_negated_globs():
    pats = ["**/*.test.js", "!**/skip/**", "!*.snap"]
    assert mc.path_matches("a/b.test.js", pats)
    assert not mc.path_matches("a/skip/b.test.js", pats)
    assert not mc.path_matches("a/b.snap", ["**/*", "!*.snap"])
    assert not mc.path_matches("a.js", ["!a.js"])  # only negations match nothing
    assert mc.path_matches("x/a.ts", ["**/*.{js,ts}", "!**/{gen,out}/**"])
    assert not mc.path_matches("x/out/a.ts", ["**/*.{js,ts}", "!**/{gen,out}/**"])


def test_a_snapshot_only_candidate_has_no_runnable_tests(tmp_path):
    make(tmp_path, {"package.json": pkg("jest")})
    suites = mc.detect_suites(tmp_path)
    snap = "src/__tests__/__snapshots__/a.test.ts.snap"
    c = SimpleNamespace(test_files=[SimpleNamespace(path=snap, status="modified")])
    assert candidates.owning_suite(snap, suites) == "jest"
    assert verify.suite_plan(c, suites) == []
    assert verify.runnable_files(c, suites) == []
    c2 = SimpleNamespace(test_files=[SimpleNamespace(path=snap, status="modified"),
                                     SimpleNamespace(path="src/a.test.ts", status="modified")])
    assert verify.runnable_files(c2, suites) == ["src/a.test.ts"]


def test_mocha_runnable_is_test_named_files_and_documented(tmp_path):
    make(tmp_path, {"package.json": pkg("mocha")})
    s = one(tmp_path)
    for ok in ("test/a.test.js", "src/b.spec.ts", "test/unit/test-foo.js", "test/index.js",
               "test/foo-test.mjs"):
        assert s.is_runnable(ok), ok
    for bad in ("test/data.json", "test/fixtures/a.js", "test/helpers/setup.js", "test/helper.js",
                "src/a.js", "test/a.md", "test/unit/index.js"):
        assert not s.is_runnable(bad), bad
    notes = presets.shipped()["mocha"].notes
    assert ".mocharc" in notes and "spec" in notes


# --- 7. scanning depth and the evidence walk ---

def test_scan_depth_is_three_and_evidence_skips_only_vendored_dirs(tmp_path):
    tgo = {"go.mod": "module x\n", "a_test.go": ""}
    make(tmp_path / "a" / "b" / "c", tgo)         # depth 3: found
    make(tmp_path / "a" / "b" / "c" / "d", tgo)   # depth 4: not scanned
    assert [s.cwd for s in mc.detect_suites(tmp_path)] == ["a/b/c"]


@pytest.mark.parametrize("where, found", [
    ("internal/deps/x_test.go", True), ("tools/bin/x_test.go", True), ("out/x_test.go", True),
    ("build/x_test.go", True), ("env/x_test.go", True), ("dist/x_test.go", True),
    ("node_modules/x_test.go", False), ("vendor/x_test.go", False), ("target/x_test.go", False),
    (".git/x_test.go", False),
])
def test_evidence_walk_skips_only_vendored_dependency_dirs(tmp_path, where, found):
    make(tmp_path, {"go.mod": "module x\n", where: ""})
    assert bool(mc.detect_suites(tmp_path)) is found


def test_cargo_package_copies_under_target_are_not_evidence(tmp_path):
    make(tmp_path, {"Cargo.toml": "[package]\n", "target/package/x-1.0/tests/it.rs": ""})
    assert mc.detect_suites(tmp_path) == []


# --- 8. new presets and fallbacks ---

def test_playwright_beats_a_root_jest_for_e2e_specs(tmp_path):
    make(tmp_path, {"package.json": pkg("jest"), "src/a.test.ts": "",
                    "e2e/package.json": pkg("@playwright/test"), "e2e/playwright.config.ts": "",
                    "e2e/login.spec.ts": ""})
    suites = mc.detect_suites(tmp_path)
    assert [s.name for s in suites] == ["e2e", "jest"]
    assert candidates.owning_suite("e2e/login.spec.ts", suites) == "e2e"
    assert candidates.owning_suite("src/a.test.ts", suites) == "jest"
    e2e = suites[0]
    assert verify.build_command(e2e, ["e2e/login.spec.ts"]) == "npx playwright test login.spec.ts"
    assert e2e.is_runnable("e2e/login.spec.ts") and e2e.is_runnable("e2e/x/a.spec.js")
    assert not e2e.is_runnable("e2e/helpers.ts")


def test_node_test_preset(tmp_path):
    make(tmp_path, {"package.json": pkg(scripts={"test": "node --test test/"})})
    s = one(tmp_path)
    assert (s.name, s.test_command) == ("node-test", "node --test {tests}")
    assert s.is_runnable("test/a.test.js") and s.is_runnable("test/sub/b.mjs")
    assert not s.is_runnable("test/data.json")


def test_npm_test_fallback_only_without_a_framework(tmp_path):
    make(tmp_path / "plain", {"package.json": pkg(scripts={"test": "tap"}), "pnpm-lock.yaml": ""})
    make(tmp_path / "jestdir", {"package.json": pkg("jest", scripts={"test": "jest"})})
    make(tmp_path / "placeholder", {"package.json": pkg(
        scripts={"test": 'echo "Error: no test specified" && exit 1'})})
    make(tmp_path / "noscript", {"package.json": pkg()})
    suites = by_name(tmp_path)
    assert set(suites) == {"plain", "jestdir"}
    assert suites["plain"].test_command == "pnpm test"
    assert suites["plain"].setup == "pnpm install --frozen-lockfile"
    assert "{tests}" not in suites["plain"].test_command
    assert suites["jestdir"].test_command.startswith("npx jest")
    n = presets.shipped()["npm-test"]
    assert "fidelity" in n.notes and n.fallback


@pytest.mark.parametrize("lock, cmd", [
    ("yarn.lock", "yarn test"), ("bun.lockb", "bun run test"), ("package-lock.json", "npm test"),
    (None, "npm test"),
])
def test_npm_test_commands_by_lockfile(tmp_path, lock, cmd):
    make(tmp_path, {"package.json": pkg(scripts={"test": "tap"})})
    if lock:
        write(tmp_path, lock)
    assert one(tmp_path).test_command == cmd


def test_make_test_and_just_fallback(tmp_path):
    make(tmp_path / "m", {"Makefile": ".PHONY: test\ntest:\n\techo hi\n"})
    make(tmp_path / "j", {"justfile": "build:\n  x\ntest:\n  echo hi\n"})
    make(tmp_path / "nomake", {"Makefile": "build:\n\techo\n"})
    make(tmp_path / "go", {"Makefile": "test:\n\tgo test\n", "go.mod": "module x\n",
                           "a_test.go": ""})
    suites = by_name(tmp_path)
    assert set(suites) == {"m", "j", "go"}
    assert suites["m"].test_command == "make test"
    assert suites["j"].test_command == "just test"
    assert suites["go"].test_command.startswith("go test")  # a framework preset matched
    assert suites["m"].is_runnable("m/tests/test_x.py") and suites["m"].is_runnable("m/a/b_test.go")
    assert "fidelity" in presets.shipped()["make-test"].notes


# --- 9. Python venv setup ---

VENV = "python3 -m venv .venv && .venv/bin/python -m pip install -q -U pip"
PIP = ".venv/bin/python -m pip install -q"


def test_pytest_setup_uses_a_venv_with_requirements_and_the_project(tmp_path):
    make(tmp_path, {"requirements.txt": "pytest\n", "requirements-dev.txt": "black\n",
                    "pyproject.toml": '[project]\nname="x"\n[project.optional-dependencies]\n'
                                      'test = ["pytest"]\n'})
    s = one(tmp_path)
    assert s.setup == (f'{VENV} && {PIP} -r requirements.txt && {PIP} -r requirements-dev.txt '
                       f'&& {PIP} -e ".[test]" && {PIP} pytest')
    assert s.test_command == ".venv/bin/python -m pytest {tests}"


def test_pytest_setup_variants(tmp_path):
    make(tmp_path / "plain", {"requirements/test.txt": "pytest\n"})
    make(tmp_path / "dev", {"pyproject.toml": '[project]\nname="x"\n[project.optional-dependencies]\n'
                                              'dev = ["pytest"]\n'})
    make(tmp_path / "bare", {"setup.py": "setup(tests_require=['pytest'])\n"})
    make(tmp_path / "poetry", {"poetry.lock": "", "pyproject.toml": "[tool.poetry]\n"
                               "[tool.pytest.ini_options]\n"})
    make(tmp_path / "uv", {"uv.lock": "", "pyproject.toml": "[tool.pytest.ini_options]\n"})
    make(tmp_path / "pipenv", {"Pipfile.lock": "{}", "Pipfile": '[dev-packages]\npytest="*"\n'})
    s = by_name(tmp_path)
    assert s["plain"].setup == f"{VENV} && {PIP} -r requirements/test.txt && {PIP} pytest"
    assert s["dev"].setup == f'{VENV} && {PIP} -e ".[dev]" && {PIP} pytest'
    assert s["bare"].setup == f"{VENV} && {PIP} -e . && {PIP} pytest"
    assert s["poetry"].setup == "poetry install --no-interaction"
    assert s["poetry"].test_command == "poetry run pytest {tests}"
    assert s["uv"].setup == "uv sync --frozen"
    assert s["uv"].test_command == "uv run pytest {tests}"
    assert s["pipenv"].setup == "pipenv install --dev --deploy"
    assert s["pipenv"].test_command == "pipenv run python -m pytest {tests}"


def test_django_uses_the_venv_interpreter(tmp_path):
    make(tmp_path, {"manage.py": "", "requirements.txt": "django\n", "setup.py": ""})
    s = one(tmp_path)
    assert s.setup == f"{VENV} && {PIP} -r requirements.txt && {PIP} -e ."
    assert s.test_command == ".venv/bin/python manage.py test {modules}"


def test_python_setup_survives_the_mine_yaml_round_trip(tmp_path):
    make(tmp_path, {"requirements.txt": "pytest\n",
                    "pyproject.toml": '[project]\nname="x"\n[project.optional-dependencies]\n'
                                      'test = ["pytest"]\n'})
    s = one(tmp_path)
    out = tmp_path / "mine.yaml"
    out.write_text(mc.render_default([s]), encoding="utf-8")
    assert mc.load_config(out).suites == [s]


# --- 10. low-severity fixes ---

def test_mix_setup_and_env(tmp_path):
    make(tmp_path, {"mix.exs": "defmodule X do\nend\n", "test/a_test.exs": ""})
    s = one(tmp_path)
    assert s.setup == ("mix local.hex --force --if-missing && mix local.rebar --force "
                       "--if-missing && mix deps.get")
    assert s.env == {"MIX_ENV": "test"}


def test_dotnet_msbuild_default_test_file_is_evidence(tmp_path):
    make(tmp_path, {"A.csproj": "<Project>Microsoft.NET.Test.Sdk</Project>", "UnitTest1.cs": ""})
    s = one(tmp_path)
    assert s.name == "dotnet" and s.is_runnable("UnitTest1.cs")


def test_commented_gemfile_lines_are_not_markers(tmp_path):
    make(tmp_path, {"Gemfile": "# gem 'rspec'\n#gem \"minitest\"\n", "spec/a_spec.rb": "",
                    "test/a_test.rb": ""})
    assert mc.detect_suites(tmp_path) == []
    write(tmp_path, "Gemfile", "source 'x'\n  gem \"rspec\", '~> 3'\n")
    assert one(tmp_path).name == "rspec"


def test_vitest_notes_mention_substring_matching():
    assert "substring" in presets.shipped()["vitest"].notes


# --- new preset fields are validated ---

GOOD = """\
name: good
markers: [{glob: good.cfg}]
test_paths: ["{suite_dir}/t/**"]
test_command: run {tests}
notes: a note
"""


@pytest.mark.parametrize("old, new, needle", [
    ("notes: a note", "notes: a note\nsetup: [{run: a, bogus: 1}]", "setup[0].bogus: unknown field"),
    ("notes: a note", "notes: a note\nsetup: [{when: [a]}]", "needs exactly one of run, first"),
    ("notes: a note", "notes: a note\nsetup: [{run: a, contains: x}]", "contains: needs when"),
    ("notes: a note", "notes: a note\nsetup: [{run: a, when: [a], contains: '['}]",
     "not a valid regular expression"),
    ("notes: a note", "notes: a note\nsetup: [{first: [{first: [b]}]}]", "not nested"),
    ("notes: a note", "notes: a note\nsetup: [\"x/{suite_dir}\"]", "{suite_dir} is not available"),
    ("notes: a note", "notes: a note\nfallback: 3", "fallback: must be true or false"),
    ("markers: [{glob: good.cfg}]", "markers: [{glob: good.cfg, evidence: ['/abs']}]",
     "markers[0].evidence[0]"),
    ("notes: a note", "notes: a note\nvariants: [{when: [a], ancestors: 1, setup: x}]",
     "ancestors: must be true or false"),
])
def test_new_fields_are_validated(tmp_path, old, new, needle):
    write(tmp_path, "good.yaml", GOOD.replace(old, new))
    with pytest.raises(presets.PresetError) as e:
        presets.load_dir(tmp_path)
    assert any(needle in x and "good.yaml" in x for x in e.value.errors), e.value.errors


def test_setup_steps_compose_conditionally(tmp_path):
    write(tmp_path, "good.yaml", GOOD.replace("notes: a note", """notes: a note
setup:
  - one
  - {when: [a.txt], run: two}
  - {when: [b.txt], run: three}
  - {when: [a.txt], unless: [c.txt], run: four}
  - first:
      - {when: [b.txt], run: five}
      - {when: [a.txt], contains: 'ZZ', run: six}
      - last
"""))
    [p] = presets.load_dir(tmp_path).values()
    write(tmp_path, "a.txt", "ZZ")
    assert presets.compose_setup(p.setup, tmp_path) == "one && two && four && six"
    write(tmp_path, "c.txt")
    write(tmp_path, "b.txt")
    assert presets.compose_setup(p.setup, tmp_path) == "one && two && three && five"
