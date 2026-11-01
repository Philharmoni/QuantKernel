"""Load and validate configs/factor_registry.yaml.

Stage 2 source directory: no raw-table access and no core.label import here
(enforced by core.factor.entry guards).
"""
from core.config.settings import load_settings

REQUIRED_FIELDS = ("factor_id", "version", "category", "inputs", "formula",
                   "lookback", "min_periods", "expected_direction", "unit",
                   "valid_from", "pit")


def load_factor_registry(config_root=None) -> dict:
    registry = load_settings("factor_registry.yaml", config_root)
    factors = registry["factors"]
    if not isinstance(factors, list) or not factors:
        raise ValueError("factor_registry.yaml must contain a non-empty factors list")
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
    return registry


def factor_map(config_root=None) -> dict:
    return {factor["factor_id"]: factor for factor in load_factor_registry(config_root)["factors"]}
