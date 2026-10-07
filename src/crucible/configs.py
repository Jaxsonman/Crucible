"""Model configurations (model:effort) and named matrices. See docs/design.md (Vocabulary)."""

from dataclasses import dataclass
from pathlib import Path

import yaml

from crucible import paths, pricing

EFFORTS = ("low", "medium", "high", "xhigh", "max")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    model: str
    effort: str

    @property
    def slug(self) -> str:
        return f"{self.model}-{self.effort}"

    @property
    def label(self) -> str:
        return f"{self.model}:{self.effort}"


def parse_config(text: str) -> Config:
    """Parse and validate 'model:effort' against pricing.yaml. Raises ConfigError."""
    model, sep, effort = text.strip().partition(":")
    model, effort = model.strip(), effort.strip()
    if not sep or ":" in effort:
        raise ConfigError(f"invalid config {text!r}: expected model:effort, e.g. opus:high")
    if not model:
        raise ConfigError(f"invalid config {text!r}: model is empty")
    if effort not in EFFORTS:
        raise ConfigError(
            f"invalid config {text!r}: effort {effort!r} must be one of {', '.join(EFFORTS)}"
        )
    try:
        model_id = pricing.resolve_model(model)
        entry = pricing.load_pricing()[model_id]
    except pricing.PricingError as e:
        raise ConfigError(f"invalid config {text!r}: {e}") from e
    if effort not in entry.efforts:
        supported = ", ".join(entry.efforts) or "none"
        raise ConfigError(
            f"invalid config {text!r}: {model_id} does not support effort {effort!r} "
            f"(supported: {supported})"
        )
    return Config(model=model, effort=effort)


def parse_configs(text: str) -> list[Config]:
    parts = [p for p in (s.strip() for s in text.split(",")) if p]
    if not parts:
        raise ConfigError("no configs given")
    return [parse_config(p) for p in parts]


def load_matrix(path: str | Path) -> list[Config]:
    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise ConfigError(f"{path}: cannot read matrix: {e}") from e
    items = data.get("configs") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        raise ConfigError(f"{path}: expected `configs:` as a non-empty list of model:effort")
    try:
        return [parse_config(str(i)) for i in items]
    except ConfigError as e:
        raise ConfigError(f"{path}: {e}") from e


def _stems(d: Path) -> set[str]:
    return {p.stem for p in d.glob("*.yaml")} if d.is_dir() else set()


def list_matrices() -> list[str]:
    """Names of local matrices (<home>/configs)."""
    return sorted(_stems(paths.configs_dir()))


def list_all_matrices() -> list[tuple[str, bool]]:
    """Union of local and bundled matrices as (name, is_local), sorted by name."""
    local = _stems(paths.configs_dir())
    return sorted((n, n in local) for n in local | _stems(paths.bundled_configs_dir()))


def load_named(name: str) -> list[Config]:
    path = paths.config_file(name)
    if not path.is_file():
        raise ConfigError(f"matrix {name!r} not found ({paths.configs_dir() / f'{name}.yaml'})")
    return load_matrix(path)
