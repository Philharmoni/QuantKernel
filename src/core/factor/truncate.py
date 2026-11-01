"""Truncation-invariance gate (plan Step 5's core anti-look-ahead check).

For every registered factor and each sampled truncation date D, the factor is
recomputed with every input restricted to <= D (core.factor.baseline's
input_end) and the value at D must equal the full-history build at D
bit-for-bit. Any difference means information after D leaked into the factor.

Comparison is exact (==), not tolerant: all factor arithmetic is deterministic
per row or per ordered window, so a correct implementation has zero drift.
"""
from datetime import datetime

from core.data.store import qs
from core.factor.baseline import compute_factor_parts
from core.factor.registry import factor_map


def check_truncation_invariance(store, dates, factor_ids=None, config_root=None) -> dict:
    registry = factor_map(config_root)
    dates = [datetime.strptime(value, "%Y%m%d" if "-" not in value else "%Y-%m-%d").date()
             if isinstance(value, str) else value for value in dates]
    selected = list(factor_ids) if factor_ids is not None else list(registry)
    full = store.output_relation("l3", "factor_value")
    report = {"dates": [str(d) for d in dates], "factor_ids": selected,
              "compared_values": 0, "mismatches": [], "passed": True}
    baseline_ids = [factor_id for factor_id in selected if registry[factor_id]["category"] != "composite"]
    composite_ids = [factor_id for factor_id in selected if registry[factor_id]["category"] == "composite"]
    selected_sql = ", ".join(qs(factor_id) for factor_id in selected)
    for date in dates:
        if baseline_ids:
            compute_factor_parts(store, start=date, end=date, input_end=date,
                                 factor_ids=baseline_ids, config_root=config_root)
        else:
            store.db.execute("CREATE OR REPLACE TEMP TABLE factor_parts("
                             "trade_date DATE, ts_code VARCHAR, factor_id VARCHAR, value DOUBLE)")
        truncated = {(r["ts_code"], r["factor_id"]): r["value"] for r in store.rows(
            "SELECT ts_code, factor_id, value FROM factor_parts")}
        if composite_ids:
            from core.evaluation.composite import compute_composite_parts
            compute_composite_parts(store, start=date, end=date, input_end=date,
                                    config_root=config_root)
            composite_sql = ", ".join(qs(factor_id) for factor_id in composite_ids)
            truncated.update({(r["ts_code"], r["factor_id"]): r["value"] for r in store.rows(
                f"SELECT ts_code, factor_id, value FROM factor_composite_parts "
                f"WHERE factor_id IN ({composite_sql})")})
        reference = {(r["ts_code"], r["factor_id"]): r["value"] for r in store.rows(
            f"SELECT ts_code, factor_id, value FROM {full} WHERE trade_date=DATE {qs(str(date))} "
            f"AND factor_id IN ({selected_sql})")}
        report["compared_values"] += len(reference)
        for key in sorted(set(reference) | set(truncated)):
            expected = reference.get(key)
            actual = truncated.get(key)
            # NULL vs missing-row is the same "not computable" outcome only if
            # both sides agree; presence differences are genuine mismatches.
            if expected != actual or (expected is None) != (actual is None):
                report["mismatches"].append({"date": str(date), "ts_code": key[0],
                                             "factor_id": key[1],
                                             "full_build": expected, "truncated_build": actual})
    report["passed"] = not report["mismatches"]
    return report
