"""Diagnosis hints for calibration: regex over a step's log tail + exit code -> hint.

The table lives in `src/crucible/data/diagnostics.yaml` (shipped in the wheel); contributors add
rows there. See the header comment of that file for the row format.
"""

import re
from dataclasses import dataclass, field
from importlib import resources

import yaml

STAGES = ("setup", "test")
STATUSES = ("no-tests-ran",)
LOG_WINDOW = 64 * 1024  # bytes of log tail the patterns are searched in
_EXIT_LINE = re.compile(r"^exit code: (-?\d+)$", re.MULTILINE)
_CWD_LINE = re.compile(r"^\(cwd [^\n]*\n", re.MULTILINE)


@dataclass(frozen=True)
class Rule:
    id: str
    category: str
    hint: str
    pattern: re.Pattern | None = None
    exit: tuple[int, ...] = ()
    stages: tuple[str, ...] = STAGES
    status: str | None = None
    unless: re.Pattern | None = None  # the row does not apply to a log this matches
    suppresses: tuple[str, ...] = ()  # ids of rows hidden when this (root-cause) row matches

    def matches(self, text: str, exit_code: int | None, stage: str) -> bool:
        if stage not in self.stages:
            return False
        if self.exit and exit_code not in self.exit:
            return False
        if self.unless is not None and self.unless.search(text):
            return False
        return self.pattern is None or self.pattern.search(text) is not None


@dataclass(frozen=True)
class Hint:
    id: str
    category: str
    hint: str
    status: str | None = None

    def to_dict(self) -> dict:
        return {"id": self.id, "category": self.category, "hint": self.hint}


@dataclass(frozen=True)
class Table:
    rules: list[Rule] = field(default_factory=list)
    status_hints: dict[str, str] = field(default_factory=dict)
    # Patterns showing tests really ran (pass/fail counts): a `status` row (no tests ran) never
    # applies to a log that matches one.
    ran_tests: tuple[re.Pattern, ...] = ()


def parse_table(data: object, where: str = "diagnostics.yaml") -> Table:
    """Validate the parsed YAML. Errors are ValueError naming the row."""
    if not isinstance(data, dict) or not isinstance(data.get("rules"), list):
        raise ValueError(f"{where}: expected a mapping with a `rules` list")  # noqa: TRY004
    rules, seen = [], set()
    for i, row in enumerate(data["rules"]):
        label = f"{where}: rules[{i}]"
        if not isinstance(row, dict):
            raise ValueError(f"{label}: must be a mapping")  # noqa: TRY004
        rid = row.get("id")
        if not isinstance(rid, str) or not rid:
            raise ValueError(f"{label}: `id` must be a non-empty string")
        label = f"{where}: rule {rid!r}"
        if rid in seen:
            raise ValueError(f"{label}: duplicate id")
        seen.add(rid)
        for key in ("category", "hint"):
            if not isinstance(row.get(key), str) or not row[key].strip():
                raise ValueError(f"{label}: `{key}` must be a non-empty string")
        unknown = set(row) - {"id", "category", "hint", "pattern", "exit", "stage", "status", "unless",
                                "suppresses"}
        if unknown:
            raise ValueError(f"{label}: unknown field(s) {sorted(unknown)}")
        pattern = None
        if "pattern" in row:
            try:
                pattern = re.compile(str(row["pattern"]), re.MULTILINE)
            except re.error as e:
                raise ValueError(f"{label}: invalid `pattern`: {e}") from e
        unless = None
        if "unless" in row:
            try:
                unless = re.compile(str(row["unless"]), re.MULTILINE)
            except re.error as e:
                raise ValueError(f"{label}: invalid `unless`: {e}") from e
        exit_codes = row.get("exit", [])
        if not isinstance(exit_codes, list) or not all(
                isinstance(c, int) and not isinstance(c, bool) for c in exit_codes):
            raise ValueError(f"{label}: `exit` must be a list of integers")
        if pattern is None and not exit_codes:
            raise ValueError(f"{label}: needs `pattern` and/or `exit`")
        stage = row.get("stage", list(STAGES))
        stages = [stage] if isinstance(stage, str) else stage
        if not isinstance(stages, list) or not stages or any(s not in STAGES for s in stages):
            raise ValueError(f"{label}: `stage` must be one of {list(STAGES)} (or a list)")
        status = row.get("status")
        if status is not None and status not in STATUSES:
            raise ValueError(f"{label}: `status` must be one of {list(STATUSES)}")
        sup = row.get("suppresses", [])
        if not isinstance(sup, list) or not all(isinstance(x, str) for x in sup):
            raise ValueError(f"{label}: `suppresses` must be a list of row ids")
        rules.append(Rule(rid, row["category"], " ".join(row["hint"].split()), pattern,
                          tuple(exit_codes), tuple(stages), status, unless, tuple(sup)))
    for r in rules:
        missing = [x for x in r.suppresses if x not in seen]
        if missing:
            raise ValueError(f"{where}: rule {r.id!r}: `suppresses` names unknown row(s) {missing}")
    status_hints = data.get("status_hints") or {}
    if not isinstance(status_hints, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in status_hints.items()):
        raise ValueError(f"{where}: `status_hints` must map status names to text")
    ran = data.get("ran_tests") or []
    if not isinstance(ran, list) or not all(isinstance(x, str) for x in ran):
        raise ValueError(f"{where}: `ran_tests` must be a list of regexes")
    try:
        ran_patterns = tuple(re.compile(x, re.MULTILINE) for x in ran)
    except re.error as e:
        raise ValueError(f"{where}: invalid `ran_tests` regex: {e}") from e
    return Table(rules, {k: " ".join(v.split()) for k, v in status_hints.items()}, ran_patterns)


def load_table() -> Table:
    text = resources.files("crucible").joinpath("data", "diagnostics.yaml").read_text(
        encoding="utf-8")
    return parse_table(yaml.safe_load(text))


def split_log(text: str) -> tuple[str, int | None]:
    """(body without the `$ command` / `(cwd ...)` header, exit code written by the step runner
    or None). The header is dropped so a command line can never match a pattern."""
    code = None
    for m in _EXIT_LINE.finditer(text):
        code = int(m.group(1))
    if text.startswith("$ "):
        m = _CWD_LINE.search(text)
        if m:
            text = text[m.end():]
    return text, code


def tail_lines(text: str, n: int = 20, max_chars: int = 4000) -> str:
    """The last `n` lines of `text`, capped at `max_chars`."""
    out = "\n".join(text.rstrip("\n").split("\n")[-n:])
    return out[-max_chars:]


def diagnose(text: str, exit_code: int | None, stage: str, table: Table | None = None) -> list[Hint]:
    """Hints for one step's log (`text` is the body from `split_log`), de-duplicated by text."""
    table = table or load_table()
    window = text[-LOG_WINDOW:]
    ran = any(p.search(window) for p in table.ran_tests)
    matched = [r for r in table.rules
               if not (ran and r.status)  # test counts are present: the runner did run tests
               and r.matches(window, exit_code, stage)]
    # A root-cause row (`suppresses`) hides the generic symptom rows it names.
    hidden = {x for r in matched for x in r.suppresses}
    hints, seen = [], set()
    for rule in matched:
        if rule.id not in hidden and rule.hint not in seen:
            seen.add(rule.hint)
            hints.append(Hint(rule.id, rule.category, rule.hint, rule.status))
    return hints


def ran_tests(text: str, table: Table | None = None) -> bool:
    """True if the log shows test pass/fail counts (the runner really ran tests)."""
    table = table or load_table()
    window = text[-LOG_WINDOW:]
    return any(p.search(window) for p in table.ran_tests)
