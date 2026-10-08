import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from crucible import paths
from crucible.cli import main
from crucible.prompt import PREAMBLE, build_prompt, prompt_sha256
from crucible.task import TaskError, load_task

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = REPO_ROOT / "tasks" / "example-fizzbuzz"

BODY = """
## What

Do the thing.

## Why

Because.

## Acceptance criteria

- It works.
- It is fast.
"""


def make_task(home, front=None, body=BODY, name="t1", fixture=True, files=()):
    d = home / "tasks" / name
    d.mkdir(parents=True)
    if fixture:
        (d / "fixture").mkdir()
    for f in files:
        (d / f).write_text("x")
    fm = front if front is not None else f"id: {name}\nkind: feature\nrepo: fixture"
    (d / "task.md").write_text(f"---\n{fm}\n---\n{body}")
    return d


def copy_example(home):
    dst = home / "tasks" / "example-fizzbuzz"
    shutil.copytree(EXAMPLE, dst)
    return dst


def test_valid_example_loads():
    t = load_task(EXAMPLE)
    assert t.id == "example-fizzbuzz"
    assert t.kind == "feature"
    assert t.repo == "fixture"
    assert t.check == "./check.sh"
    assert len(t.criteria) >= 3
    assert t.task_dir == EXAMPLE.resolve()


def test_defaults(crucible_home):
    t = load_task(make_task(crucible_home))
    assert (t.timeout_minutes, t.max_turns, t.check, t.ref) == (30, 100, None, None)
    assert t.criteria == ["It works.", "It is fast."]


def test_rule_frontmatter_present(crucible_home):
    d = make_task(crucible_home)
    (d / "task.md").write_text(BODY)
    with pytest.raises(TaskError, match="task.md.*frontmatter"):
        load_task(d)


def test_rule_id_equals_dir(crucible_home):
    d = make_task(crucible_home, front="id: other\nkind: feature\nrepo: fixture")
    with pytest.raises(TaskError, match="directory name"):
        load_task(d)


def test_rule_id_pattern(crucible_home):
    d = make_task(crucible_home, name="Bad_ID", front="id: Bad_ID\nkind: x\nrepo: fixture")
    with pytest.raises(TaskError, match="must match"):
        load_task(d)


def test_rule_kind_present(crucible_home):
    d = make_task(crucible_home, front="id: t1\nrepo: fixture")
    with pytest.raises(TaskError, match="kind"):
        load_task(d)


def test_rule_repo_fixture_dir_exists(crucible_home):
    d = make_task(crucible_home, fixture=False)
    with pytest.raises(TaskError, match="fixture/"):
        load_task(d)


def test_rule_repo_must_be_git_repo(crucible_home):
    d = make_task(crucible_home, front="id: t1\nkind: x\nrepo: ../nope\nref: main")
    with pytest.raises(TaskError, match="git repository"):
        load_task(d)


def test_rule_repo_ref_required(crucible_home):
    repo = crucible_home / "somerepo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    d = make_task(crucible_home, front=f"id: t1\nkind: x\nrepo: {repo}")
    with pytest.raises(TaskError, match="ref"):
        load_task(d)


def test_repo_git_with_ref_ok_and_relative_resolves(crucible_home):
    repo = crucible_home / "tasks" / "t1" / "rel-repo"
    d = make_task(crucible_home, front="id: t1\nkind: x\nrepo: rel-repo\nref: main")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    t = load_task(d)
    assert t.repo_path == repo.resolve()


def test_rule_check_file_exists(crucible_home):
    d = make_task(crucible_home, front="id: t1\nkind: x\nrepo: fixture\ncheck: ./check.sh")
    with pytest.raises(TaskError, match="check"):
        load_task(d)


def test_check_resolves_against_task_dir(crucible_home):
    d = make_task(
        crucible_home, front="id: t1\nkind: x\nrepo: fixture\ncheck: ./check.sh", files=["check.sh"]
    )
    assert load_task(d).check_path == (d / "check.sh").resolve()


@pytest.mark.parametrize("section", ["What", "Why", "Acceptance criteria"])
def test_rule_section_present(crucible_home, section):
    body = BODY.replace(f"## {section}", "## Other")
    with pytest.raises(TaskError, match=f"missing section '## {section}'"):
        load_task(make_task(crucible_home, body=body))


@pytest.mark.parametrize("section", ["What", "Why"])
def test_rule_section_non_empty(crucible_home, section):
    body = BODY.replace("Do the thing.", "").replace("Because.", "")
    with pytest.raises(TaskError, match="empty"):
        load_task(make_task(crucible_home, body=body))


def test_rule_criteria_section_non_empty(crucible_home):
    body = "\n## What\n\na\n\n## Why\n\nb\n\n## Acceptance criteria\n\n"
    with pytest.raises(TaskError, match="Acceptance criteria.*empty"):
        load_task(make_task(crucible_home, body=body))


def test_rule_at_least_one_bullet(crucible_home):
    body = BODY.replace("- It works.\n- It is fast.", "Just prose.")
    with pytest.raises(TaskError, match="no bullet"):
        load_task(make_task(crucible_home, body=body))


def test_rule_no_placeholder(crucible_home):
    body = BODY.replace("Because.", "_(fill me in)_")
    with pytest.raises(TaskError, match="placeholder.*'## Why'"):
        load_task(make_task(crucible_home, body=body))


def test_error_names_file(crucible_home):
    d = make_task(crucible_home, front="id: t1\nrepo: fixture")
    with pytest.raises(TaskError) as e:
        load_task(d)
    assert str(d / "task.md") in str(e.value)


def test_cli_validate_example(capsys):
    assert main(["task", "validate", str(EXAMPLE)]) == 0
    assert capsys.readouterr().out.strip() == "ok: example-fizzbuzz"


def test_cli_validate_error_exits_1(crucible_home, capsys):
    d = make_task(crucible_home, front="id: t1\nrepo: fixture")
    assert main(["task", "validate", str(d)]) == 1
    assert "kind" in capsys.readouterr().err


def test_task_new_and_refuse_overwrite(crucible_home, capsys):
    assert main(["task", "new", "demo"]) == 0
    d = crucible_home / "tasks" / "demo"
    assert (d / "task.md").is_file()
    assert (d / "fixture" / ".gitkeep").is_file()
    assert "repo: fixture" in (d / "task.md").read_text()
    assert main(["task", "validate", str(d)]) == 1
    err = capsys.readouterr().err
    assert "placeholder" in err
    for name in ("What", "Why", "Acceptance criteria"):
        assert f"'## {name}'" in err
    assert main(["task", "new", "demo"]) == 1
    assert "already exists" in capsys.readouterr().err
    assert not (REPO_ROOT / "tasks" / "demo").exists()
    assert paths.tasks_dir() == crucible_home / "tasks"


def test_task_new_invalid_id(crucible_home, capsys):
    assert main(["task", "new", "Bad Id"]) == 1
    assert not any((crucible_home / "tasks").iterdir())


def test_build_prompt_contents_and_hash():
    t = load_task(EXAMPLE)
    p = build_prompt(t)
    assert p.startswith(PREAMBLE)
    assert "You are working autonomously in this repository." in p
    assert t.what in p and t.why in p
    for c in t.criteria:
        assert f"- {c}" in p
    assert prompt_sha256(p) == prompt_sha256(build_prompt(t))
    assert len(prompt_sha256(p)) == 64


def test_example_check_fails_on_untouched_fixture(crucible_home):
    d = copy_example(crucible_home)
    r = subprocess.run(["bash", str(d / "check.sh")], cwd=d / "fixture", capture_output=True, check=False)
    assert r.returncode != 0


def test_example_check_passes_on_correct_solution(crucible_home, tmp_path):
    d = copy_example(crucible_home)
    ws = tmp_path / "ws"
    shutil.copytree(d / "fixture", ws)
    (ws / "fizzbuzz.py").write_text(
        "def fizzbuzz(n):\n"
        "    if n < 0:\n        raise ValueError\n"
        "    return ['FizzBuzz' if i % 15 == 0 else 'Fizz' if i % 3 == 0 else "
        "'Buzz' if i % 5 == 0 else str(i) for i in range(1, n + 1)]\n"
    )
    r = subprocess.run(["bash", str(d / "check.sh")], cwd=ws, capture_output=True, check=False)
    assert r.returncode == 0, r.stderr


def test_example_hidden_script_not_in_fixture():
    assert not (EXAMPLE / "fixture" / "hidden_test.py").exists()
    assert sys.version_info >= (3, 12)


def criteria_body(criteria_text: str) -> str:
    return f"\n## What\n\nDo it.\n\n## Why\n\nBecause.\n\n## Acceptance criteria\n\n{criteria_text}"


def test_wrapped_bullet_is_one_criterion(crucible_home):
    body = criteria_body(
        "- `add` trims the title. An empty title\n"
        "  raises `ValueError`.\n"
        "- Second bullet\n"
        "continues lazily here.\n"
    )
    t = load_task(make_task(crucible_home, body=body))
    assert t.criteria == [
        "`add` trims the title. An empty title raises `ValueError`.",
        "Second bullet continues lazily here.",
    ]


def test_nested_bullet_is_a_continuation(crucible_home):
    body = criteria_body("- Parent text\n  - sub one\n  * sub two\n- Next\n")
    t = load_task(make_task(crucible_home, body=body))
    assert t.criteria == ["Parent text - sub one * sub two", "Next"]


def test_blank_line_ends_a_bullet(crucible_home):
    body = criteria_body("- First\n\n- Second\n  wrapped\n")
    assert load_task(make_task(crucible_home, body=body)).criteria == ["First", "Second wrapped"]


def test_text_before_first_bullet_is_error(crucible_home):
    body = criteria_body("Some intro prose.\n- First\n")
    with pytest.raises(TaskError, match="'## Acceptance criteria' has text outside bullets"):
        load_task(make_task(crucible_home, body=body))


def test_text_after_blank_line_is_error(crucible_home):
    body = criteria_body("- First\n\nA trailing paragraph.\n")
    with pytest.raises(TaskError, match="text outside bullets"):
        load_task(make_task(crucible_home, body=body))


def test_criteria_only_continuation_text_has_no_bullets(crucible_home):
    body = criteria_body("  indented prose\n  more prose\n")
    with pytest.raises(TaskError, match="has no bullet criteria"):
        load_task(make_task(crucible_home, body=body))


def test_committed_todo_list_criteria_are_whole():
    t = load_task(REPO_ROOT / "tasks" / "todo-list")
    assert len(t.criteria) == 6
    assert t.criteria[0] == (
        "`add` trims leading and trailing whitespace from the title. An empty or "
        "whitespace-only title raises `ValueError`."
    )
    assert "- " + t.criteria[0] in build_prompt(t)


def test_max_budget_usd_optional_positive_number(crucible_home):
    assert load_task(make_task(crucible_home)).max_budget_usd is None
    d = make_task(crucible_home, name="t2",
                  front="id: t2\nkind: x\nrepo: fixture\nmax_budget_usd: 1.5")
    assert load_task(d).max_budget_usd == 1.5
    d = make_task(crucible_home, name="t3",
                  front="id: t3\nkind: x\nrepo: fixture\nmax_budget_usd: 2")
    assert load_task(d).max_budget_usd == 2


@pytest.mark.parametrize("value", ["0", "-1", "lots", "true", ".nan", ".inf"])
def test_max_budget_usd_must_be_positive_number(crucible_home, value):
    d = make_task(crucible_home, front=f"id: t1\nkind: x\nrepo: fixture\nmax_budget_usd: {value}")
    with pytest.raises(TaskError, match="max_budget_usd"):
        load_task(d)


def test_templates_show_commented_budget(crucible_home):
    from crucible.task import repo_task_md

    assert main(["task", "new", "demo"]) == 0
    assert "# max_budget_usd:" in (crucible_home / "tasks" / "demo" / "task.md").read_text()
    assert "# max_budget_usd:" in repo_task_md("x", "../..", "abc123")
