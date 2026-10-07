import pytest
import yaml

from crucible import pricing

TEST_TABLE = {
    "models": {
        "m-one": {
            "aliases": ["one"], "efforts": ["low", "high"],
            "input": 2.0, "output": 10.0, "cache_write": 2.5, "cache_read": 0.2,
            "source_url": "https://example.test", "as_of": "2026-10-01",
        },
        "m-two": {
            "aliases": ["two"], "efforts": ["low"],
            "input": 1.0, "output": 5.0, "cache_write": 1.25, "cache_read": 0.1,
            "source_url": "https://example.test", "as_of": "2026-10-01",
        },
        "m-two-long": {
            "aliases": [], "efforts": ["low"],
            "input": 9.0, "output": 9.0, "cache_write": 9.0, "cache_read": 9.0,
            "source_url": "https://example.test", "as_of": "2026-10-01",
        },
    }
}


@pytest.fixture
def table(crucible_home):
    (crucible_home / "pricing.yaml").write_text(yaml.safe_dump(TEST_TABLE))


def toks(i=0, o=0, cw=0, cr=0):
    return {"input": i, "output": o, "cache_creation": cw, "cache_read": cr}


def test_cost_matches_hand_computation(table):
    usage = {
        "m-one": toks(i=1_000_000, o=500_000, cw=200_000, cr=3_000_000),
        "m-two": toks(i=400_000, o=100_000, cw=800_000, cr=1_000_000),
    }
    # m-one: 2.0 + 5.0 + 0.5 + 0.6 = 8.1 ; m-two: 0.4 + 0.5 + 1.0 + 0.1 = 2.0
    cost, unpriced = pricing.cost_usd(usage)
    assert cost == pytest.approx(10.1)
    assert unpriced == []


def test_match_model_exact_prefix_and_longest(table):
    assert pricing.match_model("m-one").id == "m-one"
    assert pricing.match_model("m-one-20261001").id == "m-one"
    assert pricing.match_model("m-two-long-x").id == "m-two-long"
    assert pricing.match_model("m-two-x").id == "m-two"
    assert pricing.match_model("other") is None


def test_unknown_model_gives_none_and_lists_id(table):
    cost, unpriced = pricing.cost_usd({"m-one": toks(i=10), "mystery": toks(i=5)})
    assert cost is None
    assert unpriced == ["mystery"]


def test_cost_empty_is_zero(table):
    assert pricing.cost_usd({}) == (0.0, [])


def test_resolve_model_alias_and_id(table):
    assert pricing.resolve_model("one") == "m-one"
    assert pricing.resolve_model("m-two") == "m-two"
    with pytest.raises(pricing.PricingError, match="unknown model"):
        pricing.resolve_model("nope")


def test_missing_source_or_as_of_rejected(crucible_home):
    for key in ("source_url", "as_of"):
        bad = yaml.safe_load(yaml.safe_dump(TEST_TABLE))
        del bad["models"]["m-one"][key]
        (crucible_home / "pricing.yaml").write_text(yaml.safe_dump(bad))
        with pytest.raises(pricing.PricingError, match=key):
            pricing.load_pricing()


def test_real_pricing_entries_complete():
    entries = pricing.load_pricing()
    ids = set(entries)
    assert {"claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5-20251001"} <= ids
    for e in entries.values():
        assert e.source_url.startswith("https://")
        assert e.as_of
        assert e.input > 0 and e.output > 0 and e.cache_write > 0 and e.cache_read > 0
    assert pricing.resolve_model("opus") == "claude-opus-5-5"
    assert pricing.resolve_model("sonnet") == "claude-sonnet-5-5"
    assert pricing.resolve_model("haiku") == "claude-haiku-4-5-20251001"
    assert pricing.match_model("claude-sonnet-5-5-20261001").id == "claude-sonnet-5-5"
