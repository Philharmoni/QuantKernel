"""Load, validate, and serialize the factor registry.

On disk, each factor_id is a top-level mapping key. Legacy experiment snapshots
with a factors list remain readable; all callers receive normalized specs that
include factor_id.
"""
import yaml

from core.config.settings import load_settings

REQUIRED_FIELDS = ("factor_id", "version", "category", "inputs", "formula",
                   "lookback", "min_periods", "expected_direction", "unit",
                   "valid_from", "pit")


def normalize_factor_registry(registry) -> dict:
    if not isinstance(registry, dict) or not registry:
        raise ValueError("factor_registry.yaml must contain a non-empty factor mapping")
    if "factors" in registry:
        factors = registry["factors"]
        if not isinstance(factors, list) or not factors:
            raise ValueError("Legacy factor_registry.yaml must contain a non-empty factors list")
    else:
        factors = []
        for factor_id, body in registry.items():
            if not isinstance(body, dict):
                raise ValueError(f"factor_registry: {factor_id} must contain a metadata mapping")
            if "factor_id" in body:
                raise ValueError(f"factor_registry: {factor_id} repeats factor_id inside its body")
            factors.append({"factor_id": factor_id, **body})
    seen = set()
    for factor in factors:
        missing = [field for field in REQUIRED_FIELDS if field not in factor]
        if missing:
            raise ValueError(f"factor_registry: {factor.get('factor_id')} missing fields {missing}")
        factor_id = factor["factor_id"]
        if factor_id in seen:
            raise ValueError(f"factor_registry: duplicate factor_id {factor_id}")
        seen.add(factor_id)
        if factor["expected_direction"] not in (-1, 0, 1):
            raise ValueError(f"factor_registry: {factor_id} expected_direction must be -1/0/1")
        lookback = factor["lookback"]
        min_periods = factor["min_periods"]
        if isinstance(min_periods, int) and not isinstance(min_periods, bool):
            if not isinstance(lookback, int) or min_periods < 1 or min_periods > lookback:
                raise ValueError(f"factor_registry: {factor_id} min_periods must be within [1, lookback]")
        elif min_periods not in (None, "exact_endpoints"):
            raise ValueError(f"factor_registry: {factor_id} invalid min_periods {min_periods}")
    return {"factors": factors}


def load_factor_registry(config_root=None) -> dict:
    return normalize_factor_registry(load_settings("factor_registry.yaml", config_root))


def factor_map(config_root=None) -> dict:
    return {factor["factor_id"]: factor for factor in load_factor_registry(config_root)["factors"]}


def _flow_value(value) -> str:
    text = yaml.safe_dump([value], allow_unicode=True, default_flow_style=True,
                          sort_keys=False, width=10**6).strip()
    return text[1:-1].strip()


def factor_registry_block(spec) -> str:
    factor_id = spec["factor_id"]
    lines = [f"{factor_id}:"]
    for key, value in spec.items():
        if key != "factor_id":
            lines.append(f"  {key}: {_flow_value(value)}")
    return "\n".join(lines)


def factor_registry_text(specs) -> str:
    return "\n\n".join(factor_registry_block(spec) for spec in specs) + "\n"
