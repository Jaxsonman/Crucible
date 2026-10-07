"""Model price table. See docs/design.md (Tokens and cost)."""

from dataclasses import dataclass
from datetime import date

import yaml

from crucible import paths

PRICE_FIELDS = ("input", "output", "cache_write", "cache_read")
TOKEN_KEYS = {
    "input": "input",
    "output": "output",
    "cache_write": "cache_creation",
    "cache_read": "cache_read",
}


class PricingError(Exception):
    pass


@dataclass(frozen=True)
class ModelPrice:
    id: str
    aliases: tuple[str, ...]
    efforts: tuple[str, ...]
    input: float
    output: float
    cache_write: float
    cache_read: float
    source_url: str
    as_of: str


def _needer(path, model_id, raw):
    def need(key):
        if raw.get(key) in (None, ""):
            raise PricingError(f"{path}: entry {model_id!r} is missing `{key}`")
        return raw[key]

    return need


def load_pricing() -> dict[str, ModelPrice]:
    """Load pricing.yaml from the Crucible home, keyed by model ID."""
    path = paths.pricing_file()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise PricingError(f"{path}: cannot read pricing table: {e}") from e
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, dict):
        raise PricingError(f"{path}: expected a top-level `models` mapping")
    out: dict[str, ModelPrice] = {}
    for model_id, raw in models.items():
        if not isinstance(raw, dict):
            raise PricingError(f"{path}: entry {model_id!r} must be a mapping")
        need = _needer(path, model_id, raw)

        prices = {}
        for field in PRICE_FIELDS:
            value = need(field)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise PricingError(f"{path}: entry {model_id!r} `{field}` must be a number >= 0")
            prices[field] = float(value)
        as_of = need("as_of")
        out[str(model_id)] = ModelPrice(
            id=str(model_id),
            aliases=tuple(str(a) for a in raw.get("aliases") or ()),
            efforts=tuple(str(e) for e in raw.get("efforts") or ()),
            source_url=str(need("source_url")),
            as_of=as_of.isoformat() if isinstance(as_of, date) else str(as_of),
            **prices,
        )
    return out


def resolve_model(alias_or_id: str) -> str:
    """Return the full model ID for an alias or ID in the table. Raises PricingError."""
    table = load_pricing()
    if alias_or_id in table:
        return alias_or_id
    for entry in table.values():
        if alias_or_id in entry.aliases:
            return entry.id
    known = ", ".join(sorted({*table, *(a for e in table.values() for a in e.aliases)}))
    raise PricingError(f"unknown model {alias_or_id!r} (known: {known})")


def match_model(model_id: str) -> ModelPrice | None:
    """Exact ID, else the entry whose ID is the longest prefix of model_id, else None."""
    table = load_pricing()
    if model_id in table:
        return table[model_id]
    prefixes = [i for i in table if model_id.startswith(i)]
    return table[max(prefixes, key=len)] if prefixes else None


def cost_usd(tokens_by_model: dict[str, dict[str, int]]) -> tuple[float | None, list[str]]:
    """Price a per-model token breakdown.

    Returns (cost, unpriced_model_ids). If any model is unpriced the cost is None.
    Never raises for unknown models.
    """
    total = 0.0
    unpriced: list[str] = []
    for model_id, tokens in tokens_by_model.items():
        entry = match_model(model_id)
        if entry is None:
            unpriced.append(model_id)
            continue
        for field, key in TOKEN_KEYS.items():
            total += (tokens.get(key) or 0) * getattr(entry, field) / 1_000_000
    return (None if unpriced else total), unpriced
