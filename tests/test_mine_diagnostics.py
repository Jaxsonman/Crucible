"""The diagnostics table: every row has a sample log that triggers it."""

import pytest
import yaml

from crucible.mine import diagnostics as dg

# rule id -> (log text, exit code, stage). Adding a row to data/diagnostics.yaml needs an entry.
SAMPLES = {
    "runner-exit-127": ("whatever\n", 127, "test"),
    "command-not-found": ("bash: line 1: gradle: command not found\n", 127, "test"),
    "runner-file-missing": ("bash: ./gradlew: No such file or directory\n", 127, "test"),
    "permission-denied": ("bash: ./run.sh: Permission denied\n", 126, "test"),
    "exit-126": ("\n", 126, "test"),
    "pytest-no-tests": ("============ no tests ran in 0.01s ============\n", 5, "test"),
    "no-tests-generic": ("collected 0 items\n", 1, "test"),
    "pytest-path-not-found": ("ERROR: file or directory not found: tests/x.py\n", 4, "test"),
    "jest-no-tests": ("No tests found, exiting with code 1\n", 1, "test"),
    "vitest-no-tests": ("No test files found, exiting with code 1\n", 1, "test"),
    "mocha-no-tests": ("Error: No test files found: \"test/x.js\"\n", 1, "test"),
    "maven-no-tests": ("[ERROR] No tests were executed!  (Set -DfailIfNoTests=false)\n", 1, "test"),
    "gradle-no-tests": ("No tests found for given includes: [a.B](--tests filter)\n", 1, "test"),
    "go-no-test-files": ("?   \texample.com/pkg\t[no test files]\n", 0, "test"),
    "go-no-tests-to-run": ("ok  \texample.com/pkg\t0.003s [no tests to run]\n", 0, "test"),
    "go-package-not-found": ("go: cannot find package \"x/y\"\n", 1, "test"),
    "cargo-no-test-target": ("error: no test target named `foo`\n", 101, "test"),
    "rspec-no-examples": ("0 examples, 0 failures\n", 0, "test"),
    "phpunit-no-tests": ("No tests executed!\n", 0, "test"),
    "dotnet-no-match": ("No test matches the given testcase filter `X` in a.dll\n", 0, "test"),
    "mix-no-tests": ("There are no tests to run\n", 0, "test"),
    "python-import-error": ("ModuleNotFoundError: No module named 'requests'\n", 1, "test"),
    "java-class-missing": ("java.lang.ClassNotFoundException: com.acme.Foo\n", 1, "test"),
    "node-module-missing": ("Error: Cannot find module 'left-pad'\n", 1, "test"),
    "missing-env-keyerror": ("KeyError: 'DATABASE_URL'\n", 1, "test"),
    "missing-env-message": (
        "django.core.exceptions.ImproperlyConfigured: Set the SECRET_KEY\n", 1, "test"),
    "db-connection-refused": ("psycopg2.OperationalError: could not connect to server\n", 1, "test"),
    "jvm-no-java-toolchain": (
        "Cannot find a Java installation on your machine matching: {languageVersion=21}\n", 1,
        "setup"),
    "jvm-version-mismatch": ("error: release version 21 not supported\n", 1, "test"),
    "lombok-new-jdk": (
        "java.lang.ExceptionInInitializerError\nCaused by: lombok.javac.apt.LombokProcessor\n",
        1, "test"),
    "lombok-accessors-missing": (
        ("[ERROR] /w/src/main/java/a/C.java:[3,9] cannot find symbol\n"
         "  symbol:   method getUrl()\n  location: class a.B\n"), 1, "test"),
    "ruby-load-error": ("LoadError: cannot load such file -- rails/all\n", 1, "test"),
    "node-engine-mismatch": (
        'error foo@1.0.0: The engine "node" is incompatible with this module.\n', 1, "setup"),
    "compile-error-jvm": ("[ERROR] COMPILATION ERROR :\n", 1, "test"),
    "compile-error-ts": ("src/a.test.ts(3,1): error TS2305: Module has no exported member\n", 1,
                         "test"),
    "compile-error-go": ("FAIL\texample.com/pkg [build failed]\n", 1, "test"),
    "compile-error-other": ("error[E0432]: unresolved import `x`\n", 101, "test"),
    "timeout-setup": ("timed out after 1800.0s and was killed\n", None, "setup"),
    "timeout-test": ("timed out after 600.0s and was killed\n", None, "test"),
}


def test_table_loads_and_every_row_has_a_sample():
    table = dg.load_table()
    assert {r.id for r in table.rules} == set(SAMPLES)


@pytest.mark.parametrize("rule_id", sorted(SAMPLES))
def test_each_row_matches_its_sample(rule_id):
    text, code, stage = SAMPLES[rule_id]
    hints = dg.diagnose(text, code, stage)
    assert rule_id in [h.id for h in hints]
    rule = next(r for r in dg.load_table().rules if r.id == rule_id)
    assert rule.id and rule.hint and "`" in rule.hint  # names a concrete field


def test_no_tests_rows_set_the_status():
    table = dg.load_table()
    for rid in ("pytest-no-tests", "jest-no-tests", "maven-no-tests", "go-no-test-files",
                "rspec-no-examples", "cargo-no-test-target", "mix-no-tests"):
        assert next(r for r in table.rules if r.id == rid).status == "no-tests-ran"


def test_pytest_exit_5_needs_both_exit_and_text():
    assert "pytest-no-tests" not in [h.id for h in dg.diagnose("no tests ran\n", 1, "test")]
    assert "pytest-no-tests" not in [h.id for h in dg.diagnose("fine\n", 5, "test")]


def test_stage_filters_timeout_rows():
    ids = [h.id for h in dg.diagnose("timed out after 5.0s and was killed\n", None, "setup")]
    assert "timeout-setup" in ids and "timeout-test" not in ids


def test_vitest_and_mocha_rows_do_not_overlap():
    assert [h.id for h in dg.diagnose("Error: No test files found\n", 1, "test")] == [
        "mocha-no-tests"]


def test_clean_log_has_no_hints():
    assert dg.diagnose("3 passed in 0.1s\n", 0, "test") == []


def test_split_log_drops_header_and_reads_exit_code():
    text = "$ echo ECONNREFUSED\n(cwd /w)\nboom\n\nexit code: 3\n"
    body, code = dg.split_log(text)
    assert code == 3 and "ECONNREFUSED" not in body and "boom" in body


def test_tail_lines():
    assert dg.tail_lines("\n".join(map(str, range(50))), 3) == "47\n48\n49"


def test_status_hints_cover_all_calibrate_statuses():
    from crucible.mine.calibrate import STATUSES
    hints = dg.load_table().status_hints
    assert set(STATUSES) - {"ok"} <= set(hints)


@pytest.mark.parametrize("bad, msg", [
    ({"rules": [{"id": "a", "category": "c", "hint": "h"}]}, "needs `pattern`"),
    ({"rules": [{"id": "a", "category": "c", "hint": "h", "pattern": "("}]}, "invalid `pattern`"),
    ({"rules": [{"id": "a", "category": "c", "hint": "h", "pattern": "x"},
                {"id": "a", "category": "c", "hint": "h", "pattern": "x"}]}, "duplicate"),
    ({"rules": [{"id": "a", "category": "c", "hint": "h", "pattern": "x", "stage": "build"}]},
     "stage"),
    ({"rules": [{"id": "a", "category": "c", "hint": "h", "pattern": "x", "oops": 1}]}, "unknown"),
    ({"rules": [{"id": "a", "category": "c", "hint": "h", "exit": ["x"]}]}, "exit"),
    ({"nope": []}, "rules"),
])
def test_bad_tables_are_rejected(bad, msg):
    with pytest.raises(ValueError, match=msg):
        dg.parse_table(bad)


def test_shipped_yaml_parses_with_plain_yaml():
    from importlib import resources
    data = yaml.safe_load(
        resources.files("crucible").joinpath("data", "diagnostics.yaml").read_text())
    assert len(data["rules"]) == len(SAMPLES)


def ids_of(text, code=1, stage="test"):
    return [h.id for h in dg.diagnose(text, code, stage)]


def test_wrapper_file_missing_variants():
    for text in ("./go-test.sh: line 3: ./tools/x: No such file or directory\n",
                 "/work/run.sh: exec: go: not found\n",
                 'exec: "docker": executable file not found in $PATH\n'):
        assert "runner-file-missing" in ids_of(text, 127)


def test_exit_127_hint_mentions_container_wrapper():
    hint = next(r.hint for r in dg.load_table().rules if r.id == "runner-exit-127")
    assert "container" in hint and ".crucible/scripts" in hint


def test_lombok_getter_pattern():
    log = "[ERROR] symbol:   method getName()\n[INFO] annotationProcessor lombok 1.18\n"
    assert "lombok-new-jdk" in ids_of(log)
    assert "lombok-new-jdk" not in ids_of("[ERROR] symbol:   method getName()\n")
    for hint_id in ("jvm-no-java-toolchain", "jvm-version-mismatch", "lombok-new-jdk"):
        hint = next(r.hint for r in dg.load_table().rules if r.id == hint_id)
        assert "JAVA_HOME" in hint and "container" in hint


def test_no_tests_row_ignored_when_other_packages_ran_tests():
    go = "ok  \texample.com/a\t0.2s\n?   \texample.com/b\t[no test files]\n"
    assert "go-no-test-files" not in ids_of(go, 0)
    assert "go-no-test-files" in ids_of("?   \texample.com/b\t[no test files]\n", 0)
    fail = "--- FAIL: TestX\nFAIL\n?   \texample.com/b\t[no test files]\n"
    assert "go-no-test-files" not in ids_of(fail)


def test_maven_per_module_no_tests_with_other_modules_running():
    log = ("[INFO] Tests run: 5, Failures: 0, Errors: 0, Skipped: 0\n"
           "[INFO] No tests to run.\n")
    assert "maven-no-tests" not in ids_of(log, 0)
    assert "maven-no-tests" in ids_of("[INFO] No tests to run.\n", 0)
    assert "maven-no-tests" in ids_of("[INFO] Tests run: 0, Failures: 0\nNo tests to run.\n", 0)


def test_jest_and_gradle_no_tests_unless_counts():
    assert "jest-no-tests" not in ids_of("No tests found\nTests:       3 passed, 3 total\n", 0)
    assert "jest-no-tests" in ids_of("No tests found, exiting with code 1\n")
    assert "gradle-no-tests" not in ids_of(
        "No tests found for given includes: [x]\n12 tests completed, 1 failed\n")


def test_status_rows_do_not_apply_when_counts_are_present():
    # the global `ran_tests` guard: a pytest-style count beats a "no tests ran" style line
    assert "no-tests-generic" not in ids_of("collected 0 items\n3 passed in 0.1s\n", 0)
    assert "rspec-no-examples" in ids_of("0 examples, 0 failures\n", 0)


def test_unless_is_validated():
    import pytest as _pytest
    with _pytest.raises(ValueError, match="invalid `unless`"):
        dg.parse_table({"rules": [{"id": "x", "category": "c", "hint": "h", "pattern": "a",
                                   "unless": "("}]})
    t = dg.parse_table({"rules": [{"id": "x", "category": "c", "hint": "h", "pattern": "a",
                                   "unless": "b"}]})
    assert dg.diagnose("a", 0, "test", t) and not dg.diagnose("a b", 0, "test", t)
