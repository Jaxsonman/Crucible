"""The `test_command` placeholder mini-language: grammar, filters, quoting, errors."""

import subprocess

import pytest

from crucible.mine import config as mc
from crucible.mine import placeholders as ph
from crucible.mine import verify as vf


def suite(command, cwd=".", module_root=None):
    return mc.Suite(name="s", test_paths=["**"], test_command=command, cwd=cwd,
                    module_root=module_root)


def build(command, files, **kw):
    return vf.build_command(suite(command, **kw), files)


# --- filters ---

def test_plain_tests_is_space_joined_and_quoted():
    assert build("pytest {tests}", ["a/test_x.py", "b c/test_y.py"]) == \
        "pytest a/test_x.py 'b c/test_y.py'"


def test_paths_are_relative_to_suite_cwd():
    assert build("run {tests}", ["backend/t/a.py"], cwd="backend") == "run t/a.py"


def test_module_filter_django_style():
    assert build("python manage.py test {tests|module:backend}",
                 ["backend/shop/tests/test_cart.py", "backend/shop/tests.py"], cwd="backend") == \
        "python manage.py test shop.tests.test_cart shop.tests"


def test_module_filter_strips_any_extension():
    cmd = "go {tests|module:src/test/java|join:,}"
    assert build(cmd, ["src/test/java/com/x/FooTest.java"]) == "go com.x.FooTest"
    assert build("t {tests|module:.}", ["a/b.test.js"]) == "t a.b.test"


def test_modules_alias_matches_module_root_and_keeps_legacy_behaviour():
    s = suite("python manage.py test {modules}", cwd="backend", module_root="backend")
    assert vf.build_command(s, ["backend/shop/tests/test_cart.py", "backend/shop/tests.py"]) == \
        "python manage.py test shop.tests.test_cart shop.tests"
    # legacy: only `.py` is stripped, other files keep their dotted name
    assert vf.build_command(s, ["backend/a/b.test.js"]) == "python manage.py test a.b.test.js"


def test_modules_alias_accepts_more_filters():
    s = suite("t {modules|unique|prefix:-k }", module_root="m")
    assert vf.build_command(s, ["m/a.py", "m/a.py"]) == "t -k a"


def test_stem_dir_unique():
    assert build("t {tests|stem}", ["x/FooTest.java", "y/Bar.spec.ts"]) == "t FooTest Bar.spec"
    assert build("go test {tests|dir|unique|prefix:./}",
                 ["pkg/a/a_test.go", "pkg/a/b_test.go", "pkg/c/c_test.go"]) == \
        "go test ./pkg/a ./pkg/c"
    assert build("t {tests|dir}", ["a_test.go"]) == "t ."


def test_maven_style_single_word_join():
    assert build("mvn test -Dtest={tests|stem|join:,}",
                 ["src/test/java/AFooTest.java", "src/test/java/b/BarTest.java"]) == \
        "mvn test -Dtest=AFooTest,BarTest"
    # one word: a joined string with a space in it is quoted as a whole
    assert build("echo {tests|join:, }", ["a b.py", "c.py"]) == "echo 'a b.py, c.py'"


def test_gradle_style_per_item_prefix():
    assert build("gradle test {tests|module:src/test/java|prefix:--tests }",
                 ["src/test/java/com/FooTest.java", "src/test/java/BarTest.java"]) == \
        "gradle test --tests com.FooTest --tests BarTest"


def test_prefix_suffix_per_item_and_quoting_of_value_only():
    assert build("x {tests|prefix:--f=|suffix:;}", ["a b"]) == "x --f='a b';"
    # order: prefixes stack outward
    assert build("x {tests|prefix:a|prefix:b}", ["f"]) == "x baf"


def test_join_space_is_the_default_form():
    assert build("x {tests|join: }", ["a b", "c"]) == "x 'a b' c"


def test_join_newline_escape():
    assert build("x {tests|join:\\n}", ["a", "b"]) == "x 'a\nb'"


def test_escaped_pipe_and_brace_in_args():
    assert build("x {tests|prefix:a\\|b\\}|suffix:\\\\}", ["f"]) == "x a|b}f\\"


def test_whole_suite_command_without_placeholder():
    assert build("make test", ["a/test_x.py"]) == "make test"


def test_empty_file_list_gives_empty_substitution():
    assert build("x {tests}", []) == "x "


# --- shell braces are not placeholders ---

@pytest.mark.parametrize("cmd", [
    'echo "${HOME}" ${tests}', "echo ${A:-x}", "find . -exec ls {} \\;", "echo {a,b}.txt",
    "awk '{print $1}'", "echo {1..3}", "echo { }", "echo ${tests|stem}", "echo {",
    "awk '{print}'", "echo {foo}", "echo { print }", "echo {tests_dir}", "echo {testsuite}",
])
def test_brace_syntax_that_is_not_a_placeholder_passes_through(cmd):
    assert build(cmd, ["a.py"]) == cmd


def test_placeholder_next_to_dollar_brace():
    assert build("echo ${HOME} {tests}", ["a.py"]) == "echo ${HOME} a.py"


# --- injection safety ---

NASTY = ["it's a test.py", 'q"uote.py', "$(touch pwned).py", "`touch pwned`.py", "a;b.py",
         "nl\nname.py", "sp ace.py", "-dash.py", "*.py", "back\\slash.py", "~x.py"]


@pytest.mark.parametrize("cmd", ["{tests}", "{tests|stem}", "{tests|module:.}",
                                 "{tests|dir|prefix:./}", "{tests|join:,}", "{tests|stem|join:;}"])
def test_every_filter_keeps_nasty_names_inert(tmp_path, cmd):
    tmp_path = tmp_path / "cwd"
    tmp_path.mkdir()
    script = f"printf '%s\\0' {build(cmd, NASTY)}"
    r = subprocess.run(["bash", "-c", script], cwd=tmp_path, capture_output=True, check=False)
    assert r.returncode == 0, r.stderr
    assert not (tmp_path / "pwned").exists()
    assert not list(tmp_path.iterdir())


def test_nasty_names_exact_roundtrip(tmp_path):
    tmp_path = tmp_path / "cwd"
    tmp_path.mkdir()
    cmd = build("printf '%s\\0' {tests}", NASTY)
    out = subprocess.run(["bash", "-c", cmd], cwd=tmp_path, capture_output=True,
                         check=True).stdout.decode()
    # a leading `-` would be read as an option: path items get `./`
    assert out.split("\0")[:-1] == ["./" + n if n.startswith("-") else n for n in NASTY]
    assert not (tmp_path / "pwned").exists()


def test_nasty_join_is_one_word(tmp_path):
    tmp_path = tmp_path / "cwd"
    tmp_path.mkdir()
    cmd = build("printf '%s\\0' {tests|join:,}", NASTY)
    out = subprocess.run(["bash", "-c", cmd], cwd=tmp_path, capture_output=True,
                         check=True).stdout.decode()
    assert out.split("\0")[:-1] == [",".join(NASTY)]
    assert not (tmp_path / "pwned").exists()


# --- errors ---

@pytest.mark.parametrize("cmd,needle", [
    ("x {foo|stem}", "unknown placeholder {foo}"),
    ("x {test}", "unknown placeholder {test}"),
    ("x {module}", "unknown placeholder {module}"),
    ("x {TESTS}", "unknown placeholder {TESTS}"),
    ("x {Modules|unique}", "unknown placeholder {Modules}"),
    ("x { tests }", "whitespace inside placeholder"),
    ("x {tests }", "whitespace inside placeholder"),
    ("x { tests}", "whitespace inside placeholder"),
    ("x { tests|stem}", "whitespace inside placeholder"),
    ("x {tests|zip}", "unknown filter 'zip'"),
    ("x {tests|stem:3}", "'stem' takes no argument"),
    ("x {tests|module}", "'module' needs an argument"),
    ("x {tests|module:..}", "module: must be a path inside the repo"),
    ("x {tests|prefix}", "'prefix' needs an argument"),
    ("x {tests|join:,|stem}", "'join' must be the last filter"),
    ("x {tests|prefix:a|stem}", "'stem' cannot follow 'prefix'"),
    ("x {tests|suffix:a|module:.}", "'module' cannot follow 'suffix'"),
    ("x {tests|}", "empty filter"),
    ("x {tests|join:,|join:;}", "'join' must be the last filter"),
    ("x {tests", "unclosed placeholder"),
    ("x {Tests}", "unknown placeholder {Tests}"),
])
def test_parse_errors(cmd, needle):
    with pytest.raises(ph.PlaceholderError) as e:
        ph.parse(cmd)
    assert needle in str(e.value)


def test_uses_placeholders_and_names():
    assert ph.parse("make test").placeholders == []
    assert [p.source for p in ph.parse("x {tests} {modules|unique}").placeholders] == \
        ["tests", "modules"]


def test_module_outside_root_raises():
    with pytest.raises(ph.OutsideRoot):
        build("x {tests|module:backend}", ["other/test_a.py"])


def test_dash_names_render_as_dot_slash_paths():
    assert build("t {tests}", ["-x/a_test.py", "b.py"]) == "t ./-x/a_test.py b.py"
    assert build("t {tests|dir|unique}", ["-x/a_test.py"]) == "t ./-x"
    assert build("t {tests}", ["-x.py"], cwd=".") == "t ./-x.py"


def test_dash_rule_skips_transformed_and_prefixed_items():
    assert build("t {tests|stem}", ["-x.py"]) == "t -x"
    assert build("t {tests|module:.}", ["-x.py"]) == "t -x"
    assert build("t {tests|prefix:--f=}", ["-x.py"]) == "t --f=-x.py"
    assert build("t {tests|join:,}", ["-x.py", "-y.py"]) == "t -x.py,-y.py"
