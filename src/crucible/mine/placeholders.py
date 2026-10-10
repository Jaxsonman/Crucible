"""The placeholder mini-language of a suite's `test_command`.

Grammar (see docs/design.md, "Placeholders")::

    placeholder := "{" source ("|" filter)* "}"
    source      := "tests" | "modules"
    filter      := name | name ":" argument

A `{` is a placeholder only when it is not directly preceded by `$` (so `${VAR}` is shell) and
is followed by `tests` or `modules` and then `|` or `}`. Every other brace (`{}`, `{a,b}`,
`awk '{print $1}'`, `{foo}`) is left alone, except near-misses that are almost certainly typos:
`{Tests}`, `{test}`, `{module}`, `{foo|stem}` (filter syntax) and `{ tests }` (whitespace) are
errors. A backslash escapes `|`, `}` and `\\` inside an argument
(`\\n` and `\\t` are a newline and a tab; any other `\\x` is kept as written).
"""

import posixpath
import re
import shlex
from dataclasses import dataclass, field
from pathlib import PurePosixPath

SOURCES = ("tests", "modules")
NO_ARG = ("stem", "dir", "unique")
NEEDS_ARG = ("module", "prefix", "suffix")  # a non-empty argument
FILTERS = ("module", "stem", "dir", "unique", "prefix", "suffix", "join")
_OPEN = re.compile(r"(\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*)")
_TYPOS = frozenset(SOURCES) | {"test", "module"}
_ESCAPES = {"|": "|", "}": "}", "\\": "\\", "n": "\n", "t": "\t"}


class PlaceholderError(ValueError):
    """A malformed `test_command` placeholder; the message stands alone."""


class OutsideRoot(ValueError):
    """A test file lies outside the root a `module` filter (or `module_root`) names."""

    def __init__(self, path: str, root: str):
        self.path, self.root = path, root
        super().__init__(f"{path} is outside {root}")


@dataclass(frozen=True)
class Filter:
    name: str
    arg: str | None = None


@dataclass(frozen=True)
class Placeholder:
    source: str
    filters: tuple[Filter, ...] = ()


@dataclass(frozen=True)
class Command:
    parts: tuple["str | Placeholder", ...] = field(default_factory=tuple)

    @property
    def placeholders(self) -> list[Placeholder]:
        return [p for p in self.parts if isinstance(p, Placeholder)]


def _read_body(text: str, start: int) -> tuple[list[str], int] | None:
    """Split the placeholder body starting at `start` on unescaped `|`. Returns the segments
    (escapes resolved) and the index of the closing `}`, or None if it never closes."""
    segs, cur, i = [], [], start
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            cur.append(_ESCAPES.get(nxt, ch + nxt))
            i += 2
            continue
        if ch == "}":
            segs.append("".join(cur))
            return segs, i
        if ch == "|":
            segs.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    return None


def _check_relative(arg: str) -> bool:
    p = PurePosixPath(arg)
    return not (p.is_absolute() or ".." in p.parts)


def _parse_filters(shown: str, segs: list[str]) -> tuple[Filter, ...]:
    out: list[Filter] = []
    decorated = ""  # the last prefix/suffix seen: value filters may not follow it
    for seg in segs:
        name, colon, arg = seg.partition(":")
        if not name:
            raise PlaceholderError(f"{shown}: empty filter")
        if out and out[-1].name == "join":
            raise PlaceholderError(f"{shown}: 'join' must be the last filter, got {name!r} after it")
        if name not in FILTERS:
            raise PlaceholderError(
                f"{shown}: unknown filter {name!r} (allowed: {', '.join(FILTERS)})")
        if name in NO_ARG and colon:
            raise PlaceholderError(f"{shown}: {name!r} takes no argument")
        if name in NEEDS_ARG and not arg:
            raise PlaceholderError(f"{shown}: {name!r} needs an argument ({name}:<value>)")
        if name == "join" and not colon:
            raise PlaceholderError(f"{shown}: 'join' needs an argument (join:<separator>)")
        if name == "module" and not _check_relative(arg):
            raise PlaceholderError(
                f"{shown}: module: must be a path inside the repo, got {arg!r}")
        if name in ("module", "stem", "dir") and decorated:
            raise PlaceholderError(
                f"{shown}: {name!r} cannot follow {decorated!r} "
                f"(prefix and suffix wrap the finished item, so they come last)")
        if name in ("prefix", "suffix"):
            decorated = name
        out.append(Filter(name, arg if colon else None))
    return tuple(out)


def parse(command: str) -> Command:
    """Split `command` into literal text and placeholders. Raises PlaceholderError."""
    parts: list[str | Placeholder] = []
    lit: list[str] = []
    i, n = 0, len(command)
    while i < n:
        ch = command[i]
        m = _OPEN.match(command, i + 1) if ch == "{" and not (i and command[i - 1] == "$") else None
        if m is None:
            lit.append(ch)
            i += 1
            continue
        lead, name, trail = m.groups()
        after = command[m.end():m.end() + 1]
        closed = after in ("|", "}")
        if name in SOURCES and closed and (lead or trail):
            raise PlaceholderError(
                f"whitespace inside placeholder {command[i:m.end() + 1]!r}: write "
                f"{{{name}}} with no spaces around the name")
        if name in SOURCES and not closed and not lead:
            raise PlaceholderError(
                f"unclosed placeholder {{{name}... (expected '|' or '}}' after {name!r})")
        typo = closed and (name.lower() in _TYPOS or after == "|")
        if name not in SOURCES and not typo:  # `{print $1}`, `{foo}`, `{a,b}`: shell or program
            lit.append(ch)
            i += 1
            continue
        if name in SOURCES and not closed:
            lit.append(ch)
            i += 1
            continue
        body = _read_body(command, m.end())
        if body is None:
            raise PlaceholderError(f"unclosed placeholder {command[i:]!r}: missing '}}'")
        segs, end = body
        segs = segs[1:]  # the first segment is the empty text between the name and `|`/`}`
        shown = command[i:end + 1]
        if name not in SOURCES:
            raise PlaceholderError(
                f"unknown placeholder {{{name}}} (allowed: "
                f"{', '.join('{' + s + '}' for s in SOURCES)}; "
                f"write ${{{name}}} for a shell variable)")
        if lit:
            parts.append("".join(lit))
            lit = []
        parts.append(Placeholder(name, _parse_filters(shown, segs)))
        i = end + 1
    if lit:
        parts.append("".join(lit))
    return Command(tuple(parts))


# --- rendering ---

@dataclass
class _Item:
    value: str  # repo-relative path at first; transformed by the filters
    pre: str = ""
    post: str = ""
    path: bool = True  # still a path relative to cwd (no module/stem transform applied)
    root: bool = False  # `dir` of a file at the cwd (value `.`)

    def text(self) -> str:
        """pre + value + post; the cwd itself behind a prefix ending in `/` is just the prefix
        (`./` + `.` would be `./.`)."""
        if self.root and self.value == "." and self.pre.endswith("/"):
            return self.pre + self.post
        return self.pre + self.value + self.post


def _outside(rel: str) -> bool:
    return rel == ".." or rel.startswith("../")


def _module(item: _Item, root: str, cwd: str, legacy: bool, orig: str) -> None:
    repo_rel = posixpath.normpath(posixpath.join(cwd, item.value))
    rel = posixpath.relpath(repo_rel, root)
    if _outside(rel):
        raise OutsideRoot(orig, root)
    p = PurePosixPath(rel)
    if legacy:
        p = p.with_suffix("") if p.suffix == ".py" else p
    elif p.suffix:
        p = p.with_suffix("")
    item.value = ".".join(p.parts)
    item.path = False


def _expand(ph: Placeholder, files: list[str], cwd: str, module_root: str | None) -> str:
    filters = list(ph.filters)
    if ph.source == "modules":
        if module_root is None:
            raise PlaceholderError("{modules} needs module_root")
        filters.insert(0, Filter("_legacy_module", module_root))
    items = [(f, _Item(posixpath.relpath(f, cwd))) for f in files]
    join: str | None = None
    for flt in filters:
        if flt.name in ("module", "_legacy_module"):
            for orig, it in items:
                _module(it, flt.arg or ".", cwd, flt.name == "_legacy_module", orig)
        elif flt.name == "stem":
            for _, it in items:
                it.value = PurePosixPath(it.value).stem
                it.path = False
        elif flt.name == "dir":
            for _, it in items:
                it.value = posixpath.dirname(it.value) or "."
                it.root = it.value == "."
        elif flt.name == "unique":
            seen, kept = set(), []
            for pair in items:
                if pair[1].value not in seen:
                    seen.add(pair[1].value)
                    kept.append(pair)
            items = kept
        elif flt.name == "prefix":
            for _, it in items:
                it.pre = (flt.arg or "") + it.pre
        elif flt.name == "suffix":
            for _, it in items:
                it.post += flt.arg or ""
        elif flt.name == "join":
            join = flt.arg
    if join is not None and join != " ":
        return shlex.quote(join.join(it.text() for _, it in items))
    # default: every item is its own shell word (value quoted; prefix/suffix are the
    # configuration's own text and stay unquoted)
    # A path item that starts with `-` (and has no prefix in front of it) would be parsed as an
    # option by the runner: render it as `./-name`.
    return " ".join(
        it.text() if it.root and it.value == "." and it.pre.endswith("/") else
        it.pre + shlex.quote("./" + it.value if it.path and not it.pre and it.value.startswith("-")
                             else it.value) + it.post for _, it in items)


def render(cmd: Command, test_files: list[str], cwd: str = ".", module_root: str | None = None) -> str:
    """Fill the placeholders. `test_files` are repo-relative; they start out relative to `cwd`.
    Raises OutsideRoot when a `module` filter (or `{modules}`) meets a file outside its root."""
    return "".join(
        p if isinstance(p, str) else _expand(p, test_files, cwd, module_root) for p in cmd.parts)
