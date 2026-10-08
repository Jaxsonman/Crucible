"""Solution leak check.

Flags task text (What, Why, Acceptance criteria) that gives away the fix: quoted code, identifiers
the diff introduces, and file-level edit instructions. The non-test diff is recomputed from git
(`base` = the task's `ref`, `merge` from `.mine.json` / `source`). Tuned for few false positives:
anything the agent can already see at base is allowed.
"""

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from crucible.mine import candidates
from crucible.mine.config import MineConfig, path_matches
from crucible.mine.scaffold import merge_sha
from crucible.task import Task

MIN_QUOTE_LEN = 20
EDIT_VERBS = (r"add|adds|adding|change|changes|changing|modify|modifies|modifying|update|updates|"
              r"updating|edit|edits|editing|rewrite|rewrites|replace|replaces|remove|removes|"
              r"create|creates|implement|implements|patch|patches")
# Keywords and literals that are never "introduced" by a diff.
COMMON_WORDS = frozenset(
    ["true", "false", "none", "null", "nil", "undefined", "self", "this", "return", "if", "else", "elif", "for", "while", "in", "is", "not", "and", "or", "def", "class", "function", "const", "let", "var", "import", "from", "pass", "break", "continue", "raise", "try", "except", "finally", "with", "as", "lambda", "new", "void", "int", "str", "string", "bool", "float", "list", "dict", "set"])
FILE_EXTS = frozenset(
    ["py", "js", "jsx", "ts", "tsx", "md", "json", "yaml", "yml", "txt", "sh", "css", "scss", "html", "toml", "cfg", "ini", "lock", "rb", "go", "rs", "java", "c", "h", "cpp"])
TRIVIAL_LINE = re.compile(
    r"^(return|raise|pass|continue|break|else|try|finally|yield)\b[\w .]{0,12}$")
# Unicode-aware: `\w` matches letters of any script; `[^\W\d]` is a letter or underscore.
IDENT = re.compile(r"[^\W\d]\w*")
BACKTICK = re.compile(r"`+([^`\n]+)`+")
SNAKE = re.compile(r"\b[^\W\d_][^\W_]*(?:_[^\W_]+)+\b|\b_\w*[^\W\d_]\w*")
WORD = re.compile(r"\b[^\W\d_][^\W_]*\b")
DOTTED = re.compile(r"\b[^\W\d]\w*(?:\.[^\W\d]\w*)+\b")
CALL = re.compile(r"\b([^\W\d]\w*)\(\)")
_SEG = r"[\w\-.{}:<>*]+"
PATHLIKE = re.compile(
    rf"^(?:(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+)?(?:(?:/{_SEG})+/?|/|{_SEG}(?:/{_SEG})+/?)$")


@dataclass(frozen=True)
class Flag:
    kind: str     # quoted-code | new-identifier | file-instruction
    span: str
    section: str  # What | Why | Acceptance criteria
    reason: str

    def to_dict(self) -> dict:
        return {"kind": self.kind, "span": self.span, "section": self.section,
                "reason": self.reason}


def is_camel(word: str) -> bool:
    """A lower-case letter followed later by an upper-case one (camelCase/PascalCase with an
    inner capital); Unicode-aware."""
    seen_lower = False
    for ch in word:
        if ch.islower() or ch.isdigit():
            seen_lower = seen_lower or ch.islower()
        elif ch.isupper() and seen_lower:
            return True
    return False


def code_shaped(tok: str) -> bool:
    return "_" in tok or is_camel(tok)


def _is_comment_hash(text: str, i: int) -> bool:
    """`#` starts a comment at line start or after whitespace (not `this.#x`, `a#b`)."""
    return i == 0 or text[i - 1] in " \t\n"


@dataclass(frozen=True)
class CommentSyntax:
    line: tuple[str, ...]
    block: tuple[tuple[str, str], ...] = ()


_C_LIKE = CommentSyntax(("//",), (("/*", "*/"),))
# Unknown extension: the permissive union (what the check always did).
DEFAULT_SYNTAX = CommentSyntax(("#", "//"), (("/*", "*/"),))
_HASH = CommentSyntax(("#",))
_DASH = CommentSyntax(("--",))
_SQL = CommentSyntax(("--",), (("/*", "*/"),))
# Markup files embed scripts and styles, so they take `<!-- -->` plus the C-like comments.
_MARKUP = CommentSyntax(("//",), (("<!--", "-->"), ("/*", "*/")))
_LUA = CommentSyntax(("--",), (("--[[", "]]"),))
SYNTAX_BY_EXT: dict[str, CommentSyntax] = {
    **dict.fromkeys(
        ["py", "rb", "sh", "bash", "zsh", "yaml", "yml", "toml", "pl", "pm", "r", "ps1", "mk", "cmake", "tf"], _HASH),
    **dict.fromkeys(
        ["java", "kt", "kts", "scala", "go", "rs", "js", "jsx", "mjs", "cjs", "ts", "tsx", "cs", "swift", "dart", "c", "h", "cc", "cpp", "cxx", "hpp", "m", "groovy", "gradle", "scss", "less"], _C_LIKE),
    "php": CommentSyntax(("//", "#"), (("/*", "*/"),)),
    "css": CommentSyntax((), (("/*", "*/"),)),
    "sql": _SQL,
    **dict.fromkeys(["hs", "elm"], _DASH),
    "lua": _LUA,
    **dict.fromkeys(["html", "htm", "xhtml", "xml", "vue", "jsp", "svelte", "astro"], _MARKUP),
}
# Haskell and Elm: `--` followed by one of these is an operator (`-->`, `--|`), not a comment.
_OPERATOR_CHARS = frozenset("!#$%&*+./<=>?@\\^|~:")


def comment_syntax(ext: str | None) -> CommentSyntax:
    """The comment syntax for a file extension (without the dot, any case); the permissive
    default for an unknown one or None."""
    return SYNTAX_BY_EXT.get((ext or "").lower().lstrip("."), DEFAULT_SYNTAX)


def _line_comment_at(text: str, i: int, syntax: CommentSyntax, ext: str | None) -> bool:
    for marker in syntax.line:
        if not text.startswith(marker, i):
            continue
        if marker == "#":
            if _is_comment_hash(text, i):
                return True
        elif marker == "--" and (ext or "").lower() in ("hs", "elm"):
            if text[i + 2:i + 3] not in _OPERATOR_CHARS:
                return True
        else:
            return True
    return False


def strip_non_code(lines: list[str], ext: str | None = None) -> str:
    """Added lines with string literals (incl. triple-quoted/docstrings and JS template
    strings) and comments blanked out. Comment syntax follows the file extension `ext`
    (`py`: `#`; C-like languages: `//` and `/* */`; `sql`: `--` and `/* */`; `html`/`xml`:
    `<!-- -->`; ...; unknown: `#`, `//` and `/* */`), see `SYNTAX_BY_EXT`. Approximate: a
    construct that isn't closed where it should be (a one-line string on its line, a
    multi-line one or a block comment by the end of the text) is kept as code, so a stray
    quote or `/*` never swallows real code."""
    syntax = comment_syntax(ext)
    text = "\n".join(lines)
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        block = next((b for b in syntax.block if text.startswith(b[0], i)), None)
        end = text.find(block[1], i + len(block[0])) if block else -1
        if block and end >= 0:  # a closed block comment (checked first: Lua `--[[` vs `--`)
            i = end + len(block[1])
            out.append(" ")
        elif _line_comment_at(text, i, syntax, ext):
            while i < n and text[i] != "\n":
                i += 1
        elif block:  # never closed: kept as code
            out.append(ch)
            i += 1
        elif ch in "\"'`":
            triple = text[i:i + 3] == ch * 3
            q = ch * 3 if triple else ch
            multiline = triple or ch == "`"
            j = i + len(q)
            closed = False
            while j < n:
                if text[j] == "\\" and j + 1 < n and text[j + 1] != "\n":
                    j += 2
                    continue
                if text.startswith(q, j):
                    j += len(q)
                    closed = True
                    break
                if text[j] == "\n" and not multiline:
                    break
                j += 1
            if closed:
                out.append(" ")
                i = j
            else:
                out.append(ch)
                i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def norm(s: str) -> str:
    return " ".join(s.split())


def added_lines(diff: str) -> list[str]:
    return [ln[1:] for ln in diff.splitlines() if ln.startswith("+") and not ln.startswith("+++")]


def _git_ok(repo: Path, *args: str) -> bool:
    r = subprocess.run(["git", *args], cwd=repo, capture_output=True, check=False)
    return r.returncode == 0


def _base_lines(repo: Path, base: str, files: list[str]) -> set[str]:
    out: set[str] = set()
    for f in files:
        r = subprocess.run(["git", "show", f"{base}:{f}"], cwd=repo, capture_output=True, check=False)
        if r.returncode == 0:
            out.update(norm(ln) for ln in r.stdout.decode("utf-8", "replace").splitlines())
    return out


def quoted_lines(added: list[str], existing: set[str]) -> list[str]:
    """Added lines worth looking for in text: long enough, not just punctuation or a trivial
    keyword line, and not already present at base."""
    keep = []
    for raw in added:
        line = norm(raw)
        for cand in (line, line.rstrip(":;,{ ")):
            if (len(cand) >= MIN_QUOTE_LEN and re.search(r"[A-Za-z0-9]", cand)
                    and not TRIVIAL_LINE.match(cand) and line not in existing):
                keep.append(cand)
    return list(dict.fromkeys(keep))


def candidate_paths(text: str) -> set[str]:
    """URL/route-like backticked spans (`/api/x/`, `POST /api/x`, `api/x`), kept whole."""
    out = set()
    for m in BACKTICK.finditer(text):
        span = m.group(1).strip()
        if "/" in span and PATHLIKE.match(span):
            out.add(span.split()[-1])
    return out


def candidate_tokens(text: str) -> set[str]:
    """Identifier-like tokens in `text`: snake_case, camelCase, dotted parts, `name()`, and
    everything inside backticks (except URL-path-like spans, see `candidate_paths`)."""
    toks: set[str] = set()
    for m in BACKTICK.finditer(text):
        span = m.group(1).strip()
        if "/" in span and PATHLIKE.match(span):
            continue
        toks.update(IDENT.findall(m.group(1)))
    prose = BACKTICK.sub(" ", text)
    toks.update(SNAKE.findall(prose))
    toks.update(w for w in WORD.findall(prose) if is_camel(w))
    toks.update(CALL.findall(prose))
    for d in DOTTED.findall(prose):
        parts = d.split(".")
        if parts[-1].lower() in FILE_EXTS:
            continue
        toks.update(p for p in parts if len(p) >= 3)
    return {t for t in toks if t.lower() not in COMMON_WORDS and len(t) >= 2}


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?:;])\s+|\n+", text) if s.strip()]


def _file_mentioned(sentence: str, path: str) -> bool:
    for name in (path, path.rsplit("/", 1)[-1]):
        if re.search(rf"(?<![\w./-]){re.escape(name)}(?![\w/-])", sentence):
            return True
    return False


def file_instructions(text: str, files: list[str]) -> list[str]:
    hits = []
    for s in _sentences(text):
        for f in files:
            if not _file_mentioned(s, f):
                continue
            name = re.escape(f.rsplit("/", 1)[-1])
            if (re.search(rf"\b(?:{EDIT_VERBS})\b", s, re.IGNORECASE)
                    or re.search(rf"\bin\s+[`'\"]?(?:[\w./-]*/)?{name}", s, re.IGNORECASE)):
                hits.append(s)
                break
    return hits


def check_text(repo: Path, base: str, merge: str, cfg: MineConfig,
               sections: list[tuple[str, str]]) -> list[Flag]:
    """Leak flags for `sections` ((name, text) pairs) against the base->merge non-test diff."""
    changes = candidates.file_changes(repo, base, merge)
    tests, other, _ = candidates.split_files(changes, cfg.suites)
    other = [f for f in other if not path_matches(f.path, cfg.exclude_paths)]
    paths = [f.path for f in other]
    # Added lines per file: comment syntax (and so what counts as code) depends on its extension.
    by_file = {f.path: added_lines(candidates.diff_text(repo, base, merge, [f.path]))
               for f in other}
    added = [ln for lines in by_file.values() for ln in lines]
    # Names the hidden tests use are interface, not leaks.
    test_added = added_lines(candidates.diff_text(
        repo, base, merge, [f.path for f in tests if f.status != "deleted"]))
    test_norm = {norm(ln) for ln in test_added}
    test_blob = "\n".join(test_added)
    test_tokens = {t for ln in test_added for t in IDENT.findall(ln)}
    quotes = [q for q in quoted_lines(added, _base_lines(repo, base, paths))
              if q not in test_norm and q not in {x.rstrip(":;,{ ") for x in test_norm}]
    all_tokens = {t for ln in added for t in IDENT.findall(ln)}
    code_tokens = {t for p, lines in by_file.items()
                   for t in IDENT.findall(strip_non_code(lines, Path(p).suffix))}
    added_blob = "\n".join(added)
    flags: list[Flag] = []
    introduced: dict[str, bool] = {}

    def is_new(tok: str) -> bool:
        if tok in test_tokens:
            return False
        if tok not in (all_tokens if code_shaped(tok) else code_tokens):
            return False
        if tok not in introduced:
            introduced[tok] = not _git_ok(repo, "grep", "-w", "-F", "-q", "-e", tok, base)
        return introduced[tok]

    def path_is_new(p: str) -> bool:
        return (p in added_blob and p not in test_blob
                and not _git_ok(repo, "grep", "-F", "-q", "-e", p, base))

    for name, text in sections:
        flat = norm(text.replace("`", ""))
        for q in quotes:
            if q in flat:
                flags.append(Flag("quoted-code", q, name,
                                  "text quotes a line the fix adds"))
        for tok in sorted(candidate_tokens(text)):
            if is_new(tok):
                flags.append(Flag("new-identifier", tok, name,
                                  "identifier introduced by the fix, absent from the base tree"))
        for p in sorted(candidate_paths(text)):
            if path_is_new(p):
                flags.append(Flag("new-identifier", p, name,
                                  "route/path introduced by the fix, absent from the base tree"))
        for s in file_instructions(text, paths):
            flags.append(Flag("file-instruction", s, name,
                              "names a file the fix changes together with an edit verb"))
    return list(dict.fromkeys(flags))


def check_task(task: Task, cfg: MineConfig) -> list[Flag]:
    sections = [("What", task.what), ("Why", task.why)]
    sections += [("Acceptance criteria", c) for c in task.criteria]
    return check_text(task.repo_path, task.ref or "", merge_sha(task), cfg, sections)


__all__ = ["Flag", "check_task", "check_text"]
