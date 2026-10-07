import json
from pathlib import Path

import pytest
from test_mine_verify import PY, RepoInfo, commit, git, ids, mine_yaml, verify

from crucible.cli import main
from crucible.mine import config as mc
from crucible.mine import leak

BASE_PRICING = '''class Cart:
    def __init__(self):
        self.items = []
        self.total = 0


def checkout(cart):
    return cart.total
'''
FIX_PRICING = BASE_PRICING + '''

MAX_DISCOUNT = 0.5
VALID_CODES = {"SAVE10"}


def apply_discount(cart, code):
    if code.upper() in VALID_CODES and cart.total > 0:
        cart.total = cart.total * (1 - MAX_DISCOUNT)
    return cart.total
'''
TEST = ('from cart.pricing import Cart, apply_discount\n\n'
        'def test_discount():\n    c = Cart()\n    c.total = 10\n'
        '    assert apply_discount(c, "save10") == 5\n')


# Test that exercises the fix only through `checkout`, so `apply_discount` stays hidden.
HIDDEN_TEST = ('from cart.pricing import Cart, checkout\n\n'
               'def test_discount():\n    c = Cart()\n    c.total = 10\n'
               '    assert checkout(c) == 5\n')
HIDDEN_FIX = BASE_PRICING.replace("return cart.total", "return apply_discount(cart, 'save10')") + '''

def apply_discount(cart, code):
    return cart.total * 0.5
'''


@pytest.fixture
def repo(tmp_path, monkeypatch, crucible_home):
    r = tmp_path / "proj"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    commit(r, "init", {"README.md": "hi\n", "cart/__init__.py": "",
                       "cart/pricing.py": BASE_PRICING})
    sha = commit(r, "add discounts", {"cart/pricing.py": FIX_PRICING,
                                      "tests/test_pricing.py": TEST})
    (crucible_home / "mine.yaml").write_text(mine_yaml(
        f"{PY} -m pytest -q -p no:cacheprovider {{tests}}"), encoding="utf-8")
    r = r.resolve()
    monkeypatch.chdir(r)
    return RepoInfo(r, {"fix": sha})


def sections(**kw):
    return [(k.replace("_", " ").capitalize(), v) for k, v in kw.items()]


def run(repo, crucible_home, text, name="What"):
    cfg = mc.load_config(crucible_home / "mine.yaml")
    base = git(repo.path, "rev-parse", f"{repo.sha['fix']}^")
    return leak.check_text(repo.path, base, repo.sha["fix"], cfg, [(name, text)])


def kinds(flags):
    return {(f.kind, f.span) for f in flags}


def _make_repo(tmp_path, monkeypatch, crucible_home, name, fix_files):
    r = tmp_path / name
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    commit(r, "init", {"README.md": "hi\n", "cart/__init__.py": "",
                       "cart/pricing.py": BASE_PRICING})
    sha = commit(r, "fix", fix_files)
    (crucible_home / "mine.yaml").write_text(mine_yaml(
        f"{PY} -m pytest -q -p no:cacheprovider {{tests}}"), encoding="utf-8")
    r = r.resolve()
    monkeypatch.chdir(r)
    return RepoInfo(r, {"fix": sha})


NOOP_TEST = "def test_ok():\n    assert True\n"


@pytest.fixture
def hidden(tmp_path, monkeypatch, crucible_home):
    return _make_repo(tmp_path, monkeypatch, crucible_home, "hid", {
        "cart/pricing.py": HIDDEN_FIX, "tests/test_pricing.py": HIDDEN_TEST})


def test_new_identifier_flagged_when_tests_do_not_use_it(hidden, crucible_home):
    flags = run(hidden, crucible_home, "Add a `apply_discount` helper.")
    assert ("new-identifier", "apply_discount") in kinds(flags)


def test_identifier_used_by_tests_is_interface_not_leak(repo, crucible_home):
    # the test diff imports and calls apply_discount, so the task may name it
    assert run(repo, crucible_home,
               "Add an `apply_discount` helper, and apply_discount(cart).") == []
    # identifiers the tests never reference are still flagged
    assert ("new-identifier", "MAX_DISCOUNT") in kinds(
        run(repo, crucible_home, "Cap it with MAX_DISCOUNT."))


def test_quoted_line_also_in_test_diff_is_not_a_leak(tmp_path, monkeypatch, crucible_home):
    line = "    assert checkout(c) == compute_total(c)"
    r = _make_repo(tmp_path, monkeypatch, crucible_home, "q", {
        "cart/pricing.py": BASE_PRICING + "\n\ndef compute_total(c):\n" + line + "\n",
        "tests/test_pricing.py": "from cart.pricing import checkout, compute_total\n\n"
                                 "def test_x(c):\n" + line + "\n"})
    text = "It must hold that `assert checkout(c) == compute_total(c)`."
    assert run(r, crucible_home, text) == []


def test_new_constant_in_prose_flagged(repo, crucible_home):
    assert ("new-identifier", "MAX_DISCOUNT") in kinds(
        run(repo, crucible_home, "Cap it with MAX_DISCOUNT."))


def test_quoted_code_flagged(repo, crucible_home):
    flags = run(repo, crucible_home,
                "The check is  `if code.upper() in VALID_CODES   and cart.total > 0:` basically.")
    assert any(f.kind == "quoted-code" and f.span.startswith("if code.upper()") for f in flags)


def test_file_instruction_flagged(repo, crucible_home):
    flags = run(repo, crucible_home, "In `cart/pricing.py`, add a check for expired codes.")
    assert any(f.kind == "file-instruction" for f in flags)
    flags = run(repo, crucible_home, "Update pricing.py so codes work.")
    assert any(f.kind == "file-instruction" for f in flags)


def test_file_without_verb_or_other_sentence_clean(repo, crucible_home):
    assert run(repo, crucible_home, "The pricing logic is described by cart/pricing.py.") == []
    assert run(repo, crucible_home,
               "Add a discount feature. The old code lives at cart/pricing.py.") == []


def test_behavioural_text_clean(repo, crucible_home):
    assert run(repo, crucible_home,
               "Discount codes should be case-insensitive and never take more than half off") == []


def test_existing_identifier_clean(repo, crucible_home):
    assert run(repo, crucible_home,
               "A `Cart` has a `total`; `checkout` returns it. See cart.total and checkout().") == []


def test_trivial_and_existing_lines_not_quotable(repo):
    assert leak.quoted_lines(["    return None", "    }", "    # ---------------------"], set()) == []
    assert leak.quoted_lines(["    return cart.total  # keep going"], {"return cart.total # keep going"}) == []


def test_json_and_exit_codes(repo, crucible_home, capsys):
    cid = ids(repo, "fix")[0]
    assert verify("--verify", cid) == 0
    assert main(["mine", "--local", "scaffold", cid]) == 0
    d = next((crucible_home / "tasks").iterdir())
    text = (d / "task.md").read_text()
    clean = {
        "_(Describe the change the agent should make.)_":
            "Discount codes should be case-insensitive.",
        "_(Explain the motivation.)_": "Customers type codes in any case.",
        "_(One concrete, checkable criterion per bullet.)_": "A lower-case code takes half off.",
    }
    for k, v in clean.items():
        text = text.replace(k, v)
    (d / "task.md").write_text(text)
    capsys.readouterr()
    assert main(["mine", "leakcheck", str(d)]) == 0
    assert capsys.readouterr().out.strip() == f"clean: {d.name}"
    assert main(["mine", "leakcheck", str(d), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert main(["mine", "prove", str(d)]) == 0
    assert "proven:" in capsys.readouterr().out
    # leaky
    (d / "task.md").write_text(text.replace(
        "A lower-case code takes half off.",
        "In `cart/pricing.py`, add `apply_discount` and check `if code.upper() in VALID_CODES "
        "and cart.total > 0:`."))
    assert main(["mine", "leakcheck", str(d)]) == 1
    err = capsys.readouterr().err
    assert "new-identifier" in err and "quoted-code" in err and "file-instruction" in err
    assert main(["mine", "leakcheck", str(d), "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert {f["kind"] for f in data} == {"new-identifier", "quoted-code", "file-instruction"}
    assert all(set(f) >= {"kind", "span", "section"} for f in data)
    assert {f["section"] for f in data} == {"Acceptance criteria"}
    assert main(["mine", "prove", str(d)]) == 1
    assert "leaks the solution" in capsys.readouterr().err
    assert main(["mine", "prove", str(d), "--allow-leaks"]) == 0
    assert "proven:" in capsys.readouterr().out


def test_leakcheck_bad_dir_exits_1(tmp_path, capsys):
    assert main(["mine", "leakcheck", str(Path(tmp_path) / "nope")]) == 1


# --- review findings -------------------------------------------------------

COUPON_FIX = BASE_PRICING + '''

def coupon_view(request):
    """Apply a discount to the order."""
    url = "/api/orders/coupons/"
    msg = "Invalid coupon, please try again"  # coupon rejected
    return url, msg
'''
COUPON_TEST = ('from cart.pricing import coupon_view\n\n'
               'def test_view():\n    assert coupon_view(None)[0] == "/api/orders/coupons/"\n')


@pytest.fixture
def coupon(tmp_path, monkeypatch, crucible_home):
    return _make_repo(tmp_path, monkeypatch, crucible_home, "cpn", {
        "cart/pricing.py": COUPON_FIX, "tests/test_pricing.py": COUPON_TEST})


def test_plain_words_from_strings_comments_docstrings_are_clean(coupon, crucible_home):
    for text in ("A `coupon` is rejected.", "Apply a `discount`.",
                 "Say `Invalid coupon` to the user."):
        assert run(coupon, crucible_home, text) == [], text


def test_plain_word_in_real_code_still_flagged(tmp_path, monkeypatch, crucible_home):
    r = _make_repo(tmp_path, monkeypatch, crucible_home, "pw", {
        "cart/pricing.py": BASE_PRICING + "\n\ndef checkout2(cart):\n    surcharge = 1\n"
                           "    return surcharge\n",
        "tests/test_pricing.py": NOOP_TEST})
    assert ("new-identifier", "surcharge") in kinds(run(r, crucible_home, "Add a `surcharge`."))


def test_strip_non_code():
    out = leak.strip_non_code([
        'x = "coupon"  # discount', "y = 'a b' // note", "/* block", "more */ z = 1",
        '"""doc', "words here", '"""', "w = 2"])
    assert "coupon" not in out and "discount" not in out and "note" not in out
    assert "block" not in out and "words" not in out
    assert all(t in out for t in ("x", "y", "z", "w"))


def test_url_path_not_split_into_words(tmp_path, monkeypatch, crucible_home):
    r = _make_repo(tmp_path, monkeypatch, crucible_home, "url", {
        "cart/pricing.py": BASE_PRICING + '\n\ndef coupon_view():\n'
                           '    return "/api/orders/coupons/"\n',
        "tests/test_pricing.py": NOOP_TEST})
    flags = run(r, crucible_home, "POST to `/api/orders/coupons/`.")
    assert ("new-identifier", "/api/orders/coupons/") in kinds(flags)
    assert {f.span for f in flags} == {"/api/orders/coupons/"}


def test_url_path_referenced_by_tests_clean(coupon, crucible_home):
    assert run(coupon, crucible_home, "POST to `/api/orders/coupons/`.") == []
    assert run(coupon, crucible_home, "Use `POST /api/orders/coupons/`.") == []


def test_unicode_identifier_flagged(tmp_path, monkeypatch, crucible_home):
    r = _make_repo(tmp_path, monkeypatch, crucible_home, "uni", {
        "cart/pricing.py": BASE_PRICING + "\n\ndef naïve_fn(c):\n    return c\n",
        "tests/test_pricing.py": NOOP_TEST})
    assert ("new-identifier", "naïve_fn") in kinds(run(r, crucible_home, "Add naïve_fn to it."))
    assert ("new-identifier", "naïve_fn") in kinds(run(r, crucible_home, "Add `naïve_fn`."))
    assert leak.candidate_tokens("see naïveCall and über_x") >= {"naïveCall", "über_x"}


def test_quote_match_ignores_inline_backticks(tmp_path, monkeypatch, crucible_home):
    r = _make_repo(tmp_path, monkeypatch, crucible_home, "bt", {
        "cart/pricing.py": BASE_PRICING + '\n\ndef f(x):\n'
                           '    if x.strip().lower() == "save10":\n        return 1\n',
        "tests/test_pricing.py": NOOP_TEST})
    flags = run(r, crucible_home, 'Check `if x.strip().lower()` `== "save10"` first.')
    assert any(f.kind == "quoted-code" for f in flags)
    fenced = "```python\nif x.strip().lower() == \"save10\":\n```"
    assert any(f.kind == "quoted-code" for f in run(r, crucible_home, fenced))


def test_pathlike_has_no_catastrophic_backtracking():
    import time

    start = time.monotonic()
    leak.candidate_paths("see `/api/" + "a" * 40 + "()` and `/x/" + "b" * 40 + "?q=1`")
    assert time.monotonic() - start < 1.0
    assert leak.PATHLIKE.match("/api/orders/coupons/")
    assert leak.PATHLIKE.match("GET /api/orders/")
    assert leak.PATHLIKE.match("/")
    assert leak.PATHLIKE.match("frontend/src/App.tsx")


def test_strip_non_code_keeps_code_after_stray_quotes_and_hashes():
    strip = leak.strip_non_code
    assert "brandnew" in strip(["const a = /'/.test(s); brandnew(1)"])
    assert "brandnew" in strip(["this.#x = brandnew(1)"])
    assert "brandnew" in strip(["s = 'a\\", "brandnew(1)"])
    assert "brandnew" in strip(["let s = `abc", "brandnew(1)"])
    assert "brandnew" in strip(["/* unterminated", "brandnew(1)"])
    # real strings and comments are still blanked
    out = strip(["x = 'coupon'  # discount here", "y = 1 // promo", "/* note */ z = `tmpl`"])
    for word in ("coupon", "discount", "promo", "note", "tmpl"):
        assert word not in out
    assert "x =" in out and "y = 1" in out and "z =" in out
