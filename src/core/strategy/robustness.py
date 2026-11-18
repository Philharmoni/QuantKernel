"""Step 13 R1 PIT, truncation and stale-position pressure helpers."""
import json

from core.strategy.contract import canonical_hash


def pressure_factor(stale_trade_days, non_tradable=False):
    if non_tradable or stale_trade_days >= 120:
        return 0.0
    if stale_trade_days >= 60:
        return 0.5
    return 1.0


def stress_position_value(reference_value, stale_trade_days, non_tradable=False):
    return float(reference_value) * pressure_factor(stale_trade_days, non_tradable)


class PITFacts:
    """Fail closed if a truncated build attempts to read beyond its authorized date."""
    def __init__(self, provider, cutoff):
        self.provider = provider
        self.cutoff = cutoff

    def __call__(self, day, codes):
        if day > self.cutoff:
            raise PermissionError(f"Future fact read blocked after {self.cutoff}: {day}")
        return self.provider(day, codes)


def _hash_rows(rows):
    normalized = json.loads(json.dumps(rows, ensure_ascii=False, default=str))
    return canonical_hash(normalized)


def prefix_comparison(full_rows, variant_rows, cutoff, date_field="trade_date"):
    full = [row for row in full_rows if row[date_field] <= cutoff]
    variant_all = list(variant_rows)
    variant = [row for row in variant_all if row[date_field] <= cutoff]
    future_rows = len(variant_all) - len(variant)
    return {"full_prefix_rows": len(full), "variant_rows": len(variant),
        "variant_future_rows": future_rows, "full_prefix_sha256": _hash_rows(full),
        "variant_sha256": _hash_rows(variant), "mismatch_count": int(full != variant),
        "prefix_equal": full == variant}
