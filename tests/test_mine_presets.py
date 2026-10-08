"""Framework presets (data files) and preset-driven suite detection."""

import json
from importlib import resources

import pytest

from crucible.mine import config as mc
from crucible.mine import presets, verify


def gradle_cmd(gradle: str) -> str:
    """The gradle preset's test_command: a temporary init script lets `--tests` match nothing
    in some sub-projects of a multi-project build."""
    return ("i=$(mktemp) && trap 'rm -f \"$i\"' EXIT && printf '%s\\n' 'allprojects { "
            "tasks.withType(Test).configureEach { filter.failOnNoMatchingTests = false } }' "
            f"> \"$i\" && {gradle} -I \"$i\" test --console=plain "
            "{tests|stem|prefix:--tests \\*.}")


SHIPPED = {
    "pytest", "django", "jest", "vitest", "mocha", "maven", "gradle", "go", "cargo", "rspec",
    "minitest", "phpunit", "dotnet", "mix", "playwright", "node-test", "npm-test", "make-test",
}


def write(root, rel, text=""):
    f = root / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text, encoding="utf-8")


def pkg(*deps):
    return json.dumps({"devDependencies": {d: "^1.0.0" for d in deps}})


def make(root, files):
    for rel, text in files.items():
        write(root, rel, text)


# --- the shipped presets ---

def test_shipped_presets_load_and_validate():
    loaded = presets.shipped()
    assert set(loaded) == SHIPPED
    for name, p in loaded.items():
        assert p.name == name
        assert p.markers, name
        assert p.test_paths, name
        assert p.notes, f"{name}: say why this command is right (notes)"


def test_presets_are_found_through_importlib_resources():
    root = resources.files("crucible").joinpath("data", "presets")
    names = {e.name for e in root.iterdir() if e.name.endswith(".yaml")}
    assert names == {f"{n}.yaml" for n in SHIPPED}


def bad(tmp_path, text, name="bad"):
    write(tmp_path, f"{name}.yaml", text)
    with pytest.raises(presets.PresetError) as e:
        presets.load_dir(tmp_path)
    return e.value.errors


GOOD = """\
name: good
markers: [{glob: good.cfg}]
test_paths: ["{suite_dir}/t/**"]
test_command: run {tests}
notes: a note
"""


def test_good_preset_loads(tmp_path):
    write(tmp_path, "good.yaml", GOOD)
    [p] = presets.load_dir(tmp_path).values()
    assert (p.name, p.test_command, p.setup, p.runnable) == ("good", "run {tests}", "", None)


@pytest.mark.parametrize("old,new,needle", [
    ("test_command: run {tests}\n", "", "good.yaml: test_command: required"),
    ("test_command: run {tests}", "test_command: run {Tests}", "good.yaml: test_command:"),
    ("markers: [{glob: good.cfg}]", "markers: []", "good.yaml: markers: must not be empty"),
    ("markers: [{glob: good.cfg}]", "markers: [{glob: ../x}]", "good.yaml: markers[0].glob"),
    ("markers: [{glob: good.cfg}]", "markers: [{glob: a, contains: '('}]",
     "good.yaml: markers[0].contains"),
    ("markers: [{glob: good.cfg}]", "markers: [{glob: a, bogus: 1}]",
     "good.yaml: markers[0].bogus: unknown field"),
    ("notes: a note", "notes: a note\nextra: 1", "good.yaml: extra: unknown field"),
    ("name: good", "name: other", "good.yaml: name: must equal the file name"),
    ("notes: a note", "notes: a note\nsetup: pip {suite_dir}", "good.yaml: setup:"),
    ("notes: a note", "notes: a note\ntimeout: -3", "good.yaml: timeout"),
    ("notes: a note", "notes: a note\nsupersedes: [ghost]", "good.yaml: supersedes"),
    ("notes: a note", "notes: a note\nvariants: [{when: [x]}]", "good.yaml: variants[0]"),
    ("notes: a note", "notes: a note\nrunnable: []", "good.yaml: runnable"),
    ("test_paths: [\"{suite_dir}/t/**\"]", "test_paths: []", "good.yaml: test_paths"),
])
def test_bad_preset_names_file_and_field(tmp_path, old, new, needle):
    errors = bad(tmp_path, GOOD.replace(old, new), name="good")
    assert any(needle in e for e in errors), errors


def test_non_mapping_and_bad_yaml_presets(tmp_path):
    assert any("bad.yaml" in e for e in bad(tmp_path, "- a\n- b\n"))
    assert any("bad.yaml" in e for e in bad(tmp_path, "a: [unclosed\n"))


# --- table-driven: one tiny repo per preset ---

# id: (files, name, cwd, test_command, runnable (yes, no), sample tests, rendered command)
CASES = {
    "pytest": (
        {"pytest.ini": "[pytest]\n"},
        "pytest", ".", ".venv/bin/python -m pytest {tests}",
        (["tests/test_a.py"], ["tests/conftest.py"]),
        ["tests/test_a.py", "tests/test_b.py"],
        ".venv/bin/python -m pytest tests/test_a.py tests/test_b.py"),
    "django": (
        {"manage.py": ""},
        "django", ".", ".venv/bin/python manage.py test {modules}",
        (["app/tests.py"], ["app/tests/x.txt"]),
        ["app/tests/test_x.py"], ".venv/bin/python manage.py test app.tests.test_x"),
    "jest": (
        {"package.json": pkg("jest")},
        "jest", ".", "npx jest --runTestsByPath {tests}",
        (["src/a.test.ts"], ["src/a.test.js.snap", "src/__tests__/__snapshots__/a.snap"]),
        ["src/a.test.ts"], "npx jest --runTestsByPath src/a.test.ts"),
    "vitest": (
        {"package.json": pkg("vitest")},
        "vitest", ".", "npx vitest run {tests}", (["src/a.spec.ts"], ["src/a.ts"]),
        ["src/a.spec.ts"], "npx vitest run src/a.spec.ts"),
    "mocha": (
        {"package.json": pkg("mocha")},
        "mocha", ".", "npx mocha {tests}",
        (["test/a.test.js", "test/b.js"], ["src/a.js", "test/data.json", "test/unit/b.js"]),
        ["test/a.test.js"], "npx mocha test/a.test.js"),
    "maven": (
        {"pom.xml": "<project/>", "src/test/java/com/x/FooTest.java": ""},
        "maven", ".",
        ("mvn -B test -Dtest={tests|stem|join:,} -Dsurefire.failIfNoSpecifiedTests=false "
        "-DfailIfNoTests=false"),
        (["src/test/java/FooTest.java", "src/test/java/BarTests.java", "src/test/java/FooIT.java",
          "src/test/java/TestFoo.java", "src/test/kotlin/FooTest.kt"],
         ["src/test/java/Foo.java", "src/test/resources/a.json"]),
        ["src/test/java/com/x/FooTest.java", "src/test/java/com/y/BarTests.java"],
        ("mvn -B test -Dtest=FooTest,BarTests -Dsurefire.failIfNoSpecifiedTests=false "
        "-DfailIfNoTests=false")),
    "maven-wrapper": (
        {"pom.xml": "<project/>", "mvnw": "", "src/test/java/FooTest.java": ""},
        "maven", ".",
        ("./mvnw -B test -Dtest={tests|stem|join:,} -Dsurefire.failIfNoSpecifiedTests=false "
        "-DfailIfNoTests=false"),
        (["src/test/java/FooTest.java"], []),
        ["src/test/java/FooTest.java"],
        ("./mvnw -B test -Dtest=FooTest -Dsurefire.failIfNoSpecifiedTests=false "
        "-DfailIfNoTests=false")),
    "gradle": (
        {"build.gradle.kts": "", "src/test/kotlin/com/x/FooTest.kt": ""},
        "gradle", ".", gradle_cmd("gradle"),
        (["src/test/kotlin/FooTest.kt", "src/test/java/BarTests.java"], ["src/test/resources/x.yml"]),
        ["src/test/kotlin/com/x/FooTest.kt", "src/test/java/com/y/BarTest.java"],
        gradle_cmd("gradle").replace("{tests|stem|prefix:--tests \\*.}",
                                     "--tests \\*.FooTest --tests \\*.BarTest")),
    "gradle-wrapper": (
        {"build.gradle": "", "gradlew": "", "src/test/java/FooTest.java": ""},
        "gradle", ".", gradle_cmd("./gradlew"),
        (["src/test/java/FooTest.java"], []),
        ["src/test/java/FooTest.java"],
        gradle_cmd("./gradlew").replace("{tests|stem|prefix:--tests \\*.}", "--tests \\*.FooTest")),
    "go": (
        {"go.mod": "module x\n", "pkg/a_test.go": "", "a_test.go": ""},
        "go", ".", "go test -count=1 {tests|dir|unique|prefix:./}",
        (["pkg/a_test.go"], ["pkg/a.go", "pkg/testdata/x.json"]),
        ["pkg/a_test.go", "pkg/b_test.go", "a_test.go"], "go test -count=1 ./pkg ./."),
    "cargo": (
        {"Cargo.toml": "[package]\n", "tests/it.rs": ""},
        "cargo", ".", "cargo test --workspace {tests|stem|prefix:--test }",
        (["tests/it.rs", "crates/a/tests/b.rs"], ["tests/common/mod.rs", "src/lib.rs"]),
        ["tests/it.rs", "crates/a/tests/b.rs"], "cargo test --workspace --test it --test b"),
    "rspec": (
        {".rspec": "", "spec/a_spec.rb": ""},
        "rspec", ".", "rspec {tests}", (["spec/a_spec.rb"], ["spec/spec_helper.rb"]),
        ["spec/a_spec.rb", "spec/b_spec.rb"], "rspec spec/a_spec.rb spec/b_spec.rb"),
    "rspec-bundler": (
        {"Gemfile": "gem 'rspec'\n", "spec/a_spec.rb": ""},
        "rspec", ".", "bundle exec rspec {tests}", (["spec/a_spec.rb"], []),
        ["spec/a_spec.rb"], "bundle exec rspec spec/a_spec.rb"),
    "minitest": (
        {"test/test_helper.rb": "", "test/a_test.rb": ""},
        "minitest", ".",
        "ruby -Itest -Ilib -e 'ARGV.each { |f| require File.expand_path(f) }' {tests}",
        (["test/a_test.rb", "test/test_b.rb"], ["test/support/helper.rb"]),
        ["test/a_test.rb", "test/models/b_test.rb"],
        ("ruby -Itest -Ilib -e 'ARGV.each { |f| require File.expand_path(f) }' "
        "test/a_test.rb test/models/b_test.rb")),
    "phpunit": (
        {"phpunit.xml": "<phpunit/>", "tests/FooTest.php": ""},
        "phpunit", ".", "vendor/bin/phpunit --filter {tests|stem|join:\\|}",
        (["tests/FooTest.php"], ["tests/bootstrap.php"]),
        ["tests/FooTest.php"], "vendor/bin/phpunit --filter FooTest"),
    "phpunit-two": (
        {"phpunit.xml.dist": "<phpunit/>", "tests/FooTest.php": ""},
        "phpunit", ".", "vendor/bin/phpunit --filter {tests|stem|join:\\|}",
        (["tests/FooTest.php"], []),
        ["tests/FooTest.php", "tests/Sub/BarTest.php"],
        "vendor/bin/phpunit --filter 'FooTest|BarTest'"),
    "dotnet": (
        {"App.sln": "", "Tests/FooTests.cs": ""},
        "dotnet", ".", "dotnet test --filter {tests|stem|prefix:FullyQualifiedName~|join:\\|}",
        (["Tests/FooTests.cs", "Tests/BarTest.cs"], ["Tests/Helpers.cs"]),
        ["Tests/FooTests.cs"], "dotnet test --filter 'FullyQualifiedName~FooTests'"),
    "dotnet-two": (
        {"App.sln": "", "Tests/FooTests.cs": ""},
        "dotnet", ".", "dotnet test --filter {tests|stem|prefix:FullyQualifiedName~|join:\\|}",
        (["Tests/FooTests.cs"], []),
        ["Tests/FooTests.cs", "Tests/BarTest.cs"],
        "dotnet test --filter 'FullyQualifiedName~FooTests|FullyQualifiedName~BarTest'"),
    "playwright": (
        {"package.json": pkg("@playwright/test"), "playwright.config.ts": ""},
        "playwright", ".", "npx playwright test {tests}",
        (["e2e/a.spec.ts", "a.spec.js"], ["e2e/helpers.ts", "e2e/a.test.ts"]),
        ["e2e/a.spec.ts"], "npx playwright test e2e/a.spec.ts"),
    "node-test": (
        {"package.json": json.dumps({"scripts": {"test": "node --test"}})},
        "node-test", ".", "node --test {tests}",
        (["test/a.js", "src/b.test.mjs"], ["test/data.json", "src/b.js"]),
        ["test/a.js"], "node --test test/a.js"),
    "npm-test": (
        {"package.json": json.dumps({"scripts": {"test": "tap"}})},
        "npm-test", ".", "npm test",
        (["test/a.test.js", "src/b_test.go"], ["src/b.js"]),
        ["test/a.test.js"], "npm test"),
    "make-test": (
        {"Makefile": "test:\n\techo\n"},
        "make-test", ".", "make test",
        (["tests/test_a.py", "src/FooTest.java"], ["src/a.py"]),
        ["tests/test_a.py"], "make test"),
    "mix": (
        {"mix.exs": "defmodule X do\nend\n", "test/a_test.exs": ""},
        "mix", ".", "mix test {tests}",
        (["test/a_test.exs", "apps/x/test/b_test.exs"], ["test/test_helper.exs"]),
        ["test/a_test.exs"], "mix test test/a_test.exs"),
}


@pytest.mark.parametrize("case", CASES)
def test_one_fixture_per_preset(tmp_path, case):
    files, name, cwd, command, (yes, no), sample, rendered = CASES[case]
    make(tmp_path, files)
    [s] = mc.detect_suites(tmp_path)
    assert (s.name, s.cwd, s.test_command) == (name, cwd, command)
    for path in yes:
        assert s.is_runnable(path), path
    for path in no:
        assert not s.is_runnable(path), path
    assert verify.build_command(s, sample) == rendered
    out = tmp_path / "mine.yaml"
    out.write_text(mc.render_default([s]), encoding="utf-8")
    assert mc.load_config(out).suites == [s]


def test_every_shipped_preset_has_a_detection_case():
    assert {CASES[c][1] for c in CASES} >= SHIPPED


def test_test_paths_cover_fixtures_and_resources(tmp_path):
    make(tmp_path, CASES["maven"][0])
    [s] = mc.detect_suites(tmp_path)
    assert mc.path_matches("src/test/resources/data.json", s.test_paths)
    assert mc.path_matches("mod-a/src/test/java/FooTest.java", s.test_paths)
    assert not mc.path_matches("src/main/java/Foo.java", s.test_paths)


# --- layouts ---

def test_spring_boot_backend_plus_react_frontend(tmp_path):
    make(tmp_path, {
        "backend/pom.xml": "<project/>",
        "backend/mvnw": "",
        "backend/src/test/java/com/x/FooTest.java": "",
        "frontend/package.json": pkg("jest"),
        "frontend/package-lock.json": "{}",
        "frontend/src/App.test.tsx": "",
    })
    suites = {s.name: s for s in mc.detect_suites(tmp_path)}
    assert set(suites) == {"backend", "frontend"}
    b, f = suites["backend"], suites["frontend"]
    assert (b.cwd, f.cwd) == ("backend", "frontend")
    assert b.test_command.startswith("./mvnw ") and b.setup.startswith("./mvnw ")
    assert f.test_command == "npx jest --runTestsByPath {tests}" and f.setup == "npm ci --silent"
    assert mc.path_matches("backend/src/test/java/com/x/FooTest.java", b.test_paths)
    assert mc.path_matches("backend/src/test/resources/a.yml", b.test_paths)
    assert not mc.path_matches("frontend/src/App.test.tsx", b.test_paths)
    assert mc.path_matches("frontend/src/App.test.tsx", f.test_paths)
    assert not mc.path_matches("backend/src/test/java/com/x/FooTest.java", f.test_paths)
    assert b.is_runnable("backend/src/test/java/com/x/FooTest.java")


def test_go_plus_typescript(tmp_path):
    make(tmp_path, {
        "go.mod": "module x\n", "pkg/a_test.go": "",
        "web/package.json": pkg("vitest"), "web/src/a.test.ts": "",
    })
    suites = {s.name: s for s in mc.detect_suites(tmp_path)}
    assert set(suites) == {"go", "web"}
    assert suites["go"].cwd == "." and suites["web"].cwd == "web"
    assert mc.path_matches("pkg/a_test.go", suites["go"].test_paths)
    assert not mc.path_matches("web/src/a.test.ts", suites["go"].test_paths)
    assert verify.build_command(suites["web"], ["web/src/a.test.ts"]) == "npx vitest run src/a.test.ts"


def test_django_in_subdir_gets_a_repo_relative_module_root(tmp_path):
    make(tmp_path, {"backend/manage.py": "", "backend/app/tests.py": ""})
    [s] = mc.detect_suites(tmp_path)
    assert (s.cwd, s.module_root) == ("backend", "backend")
    assert verify.build_command(s, ["backend/app/tests.py"]) == (
        ".venv/bin/python manage.py test app.tests")
    assert mc.path_matches("backend/app/tests/test_x.py", s.test_paths)
    assert not mc.path_matches("other/app/tests.py", s.test_paths)


def test_maven_multimodule_is_one_suite_at_the_root(tmp_path):
    make(tmp_path, {
        "pom.xml": "<project><modules><module>a</module></modules></project>",
        "a/pom.xml": "<project/>", "a/src/test/java/FooTest.java": "",
        "b/pom.xml": "<project/>", "b/src/test/java/BarTest.java": "",
    })
    [s] = mc.detect_suites(tmp_path)
    assert (s.name, s.cwd) == ("maven", ".")
    assert mc.path_matches("a/src/test/java/FooTest.java", s.test_paths)
    assert mc.path_matches("b/src/test/java/BarTest.java", s.test_paths)


def test_gradle_multiproject_is_one_suite_at_the_root(tmp_path):
    make(tmp_path, {
        "settings.gradle": "rootProject.name = 'x'\ninclude 'a', 'b'\n",
        "build.gradle": "", "a/build.gradle": "", "a/src/test/java/FooTest.java": "",
        "b/build.gradle.kts": "",
    })
    [s] = mc.detect_suites(tmp_path)
    assert (s.name, s.cwd) == ("gradle", ".")


def test_nested_pom_without_modules_is_its_own_suite_listed_first(tmp_path):
    make(tmp_path, {
        "pom.xml": "<project/>", "src/test/java/ATest.java": "",
        "tools/pom.xml": "<project/>", "tools/src/test/java/BTest.java": "",
    })
    suites = mc.detect_suites(tmp_path)
    assert [(s.name, s.cwd) for s in suites] == [("tools", "tools"), ("maven", ".")]


def test_deeper_suites_are_listed_first(tmp_path):
    make(tmp_path, {"package.json": pkg("jest"), "packages/x/package.json": pkg("jest")})
    assert [s.cwd for s in mc.detect_suites(tmp_path)] == ["packages/x", "."]


def test_gradle_beats_maven_and_vitest_beats_jest_in_one_dir(tmp_path):
    make(tmp_path, {
        "pom.xml": "<project/>", "build.gradle": "", "src/test/java/ATest.java": "",
        "web/package.json": pkg("jest", "vitest"),
    })
    assert sorted(s.name for s in mc.detect_suites(tmp_path)) == ["gradle", "web"]
    [web] = [s for s in mc.detect_suites(tmp_path) if s.name == "web"]
    assert web.test_command.startswith("npx vitest")


def test_pytest_config_beats_django_but_conftest_alone_does_not(tmp_path):
    make(tmp_path / "a", {"manage.py": "", "pytest.ini": "[pytest]\n"})
    make(tmp_path / "b", {"manage.py": "", "conftest.py": ""})
    assert [(s.name, s.cwd) for s in mc.detect_suites(tmp_path)] == [("a", "a"), ("b", "b")]
    assert [s.test_command for s in mc.detect_suites(tmp_path)] == [
        ".venv/bin/python -m pytest {tests}", ".venv/bin/python manage.py test {modules}"]


def test_two_presets_in_one_dir_get_unique_names(tmp_path):
    make(tmp_path, {"web/package.json": pkg("jest", "mocha")})
    assert sorted(s.name for s in mc.detect_suites(tmp_path)) == ["web", "web-mocha"]


def test_scan_depth_and_skipped_dirs(tmp_path):
    test_go = {"go.mod": "module x\n", "a_test.go": ""}
    make(tmp_path / "services" / "api", test_go)            # depth 2: found
    make(tmp_path / "a" / "b" / "c" / "d", test_go)         # depth 4: not scanned
    for skipped in ("vendor", "target", "build", "node_modules", "testdata", ".hidden",
                    "venv", "fixtures"):
        make(tmp_path / skipped, test_go)
    make(tmp_path / "pyenv", {"pyvenv.cfg": "", **test_go})  # a virtualenv
    assert [s.cwd for s in mc.detect_suites(tmp_path)] == ["services/api"]


def test_no_tests_no_suite_for_evidence_presets(tmp_path):
    make(tmp_path, {"pom.xml": "<project/>", "go.mod": "module x\n", "mix.exs": ""})
    assert mc.detect_suites(tmp_path) == []


def test_unsafe_directory_names_are_not_scanned(tmp_path):
    make(tmp_path / "we*ird", {"go.mod": "module x\n", "a_test.go": ""})
    make(tmp_path / "bad\nname", {"go.mod": "module x\n", "a_test.go": ""})
    make(tmp_path / "my app", {"go.mod": "module x\n", "a_test.go": ""})
    [s] = mc.detect_suites(tmp_path)
    assert (s.name, s.cwd) == ("my-app", "my app")


def test_conftest_only_dir_folds_into_nearest_python_project(tmp_path):
    make(tmp_path / "svc", {"pyproject.toml": "[project]\nname='x'\n", "tests/conftest.py": ""})
    [s] = mc.detect_suites(tmp_path)
    assert (s.name, s.cwd) == ("svc", "svc")


# --- the written mine.yaml ---

def test_written_default_says_which_preset_made_each_suite(tmp_path):
    make(tmp_path, {
        "backend/pom.xml": "<project/>", "backend/src/test/java/FooTest.java": "",
        "frontend/package.json": pkg("vitest"),
    })
    out = tmp_path / "mine.yaml"
    suites = mc.write_default(out, tmp_path)
    text = out.read_text()
    lines = text.splitlines()
    for s, preset, marker in ((suites[0], "maven", "pom.xml"), (suites[1], "vitest", "package.json")):
        i = next(n for n, ln in enumerate(lines) if ln.startswith(f"- name: {s.name}"))
        above = "\n".join(lines[max(0, i - 8):i])
        assert f"preset `{preset}`" in above and marker in above, above
        assert "a guess" in above and "crucible mine calibrate" in above
    assert mc.load_config(out).suites == suites


def test_nothing_detected_template_is_not_language_specific(tmp_path):
    out = tmp_path / "mine.yaml"
    mc.write_default(out, tmp_path)
    head = out.read_text().split("suites:")[0]
    assert "pytest, Django, Vitest or Jest" not in head
    assert "preset" in head


def test_gradle_command_survives_render_and_runs_with_the_init_script(tmp_path):
    import os
    import shlex
    import subprocess

    from crucible.mine import scaffold

    make(tmp_path, {"build.gradle": "", "gradlew": "", "src/test/java/FooTest.java": ""})
    [s] = mc.detect_suites(tmp_path)
    # the `{suite_dir}` substitution and the mine.yaml round trip keep the Groovy braces verbatim
    out = tmp_path / "mine.yaml"
    out.write_text(mc.render_default([s]), encoding="utf-8")
    [s] = mc.load_config(out).suites
    files = ["src/test/java/FooTest.java", "x/BarTest.java"]
    cmd = verify.build_command(s, files)
    assert "{ tasks.withType(Test).configureEach { filter.failOnNoMatchingTests = false } }" in cmd
    assert cmd.endswith("--tests \\*.FooTest --tests \\*.BarTest")
    assert subprocess.run(["bash", "-n", "-c", cmd], check=False).returncode == 0
    # check.sh embeds the command via bash -c '<quoted>': still valid bash
    check = tmp_path / "check.sh"
    check.write_text(scaffold.check_script(s, files), encoding="utf-8")
    assert subprocess.run(["bash", "-n", str(check)], check=False).returncode == 0
    assert shlex.quote(cmd) in check.read_text(encoding="utf-8")
    # a fake wrapper receives `-I <init script>` with the filter disabled; the file is removed after
    fake = tmp_path / "gradlew"
    fake.write_text('#!/bin/bash\necho "ARGS: $*"\ncat "$2"\necho "$2" > init.path\n')
    fake.chmod(0o755)
    r = subprocess.run(["bash", "-c", cmd], cwd=tmp_path, env={"PATH": "/usr/bin:/bin"},
                       capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr
    assert "ARGS: -I " in r.stdout
    assert "test --console=plain --tests *.FooTest --tests *.BarTest" in r.stdout
    assert "filter.failOnNoMatchingTests = false" in r.stdout
    assert not os.path.exists((tmp_path / "init.path").read_text().strip())
