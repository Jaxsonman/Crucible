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
