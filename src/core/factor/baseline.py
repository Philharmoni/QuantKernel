"""Batch dispatcher for the per-factor research source library.

Every accepted non-composite factor is implemented in
research/factors/<factor_id>/compute.py. This module preserves the Stage 2
batch/truncation contract without maintaining a second copy of factor formulas.
"""
from core.config.settings import load_settings
from core.data.store import qs
from core.factor.plugin import load_factor_compute
from core.factor.registry import factor_map


def compute_factor_parts(store, start=None, end=None, input_end=None,
                         factor_ids=None, config_root=None) -> str:
    registry = factor_map(config_root)
    engine = load_settings("factor_research.yaml", config_root)["engine"]
    store.db.execute(f"SET memory_limit={qs(engine['memory_limit'])}")
    store.db.execute(f"SET threads={int(engine['threads'])}")
    selected = (list(factor_ids) if factor_ids is not None
                else [factor_id for factor_id, spec in registry.items()
                      if spec["category"] != "composite"])
    unknown = sorted(set(selected) - set(registry))
    if unknown:
        raise ValueError(f"Unregistered factor_ids: {unknown}")
    composites = [factor_id for factor_id in selected
                  if registry[factor_id]["category"] == "composite"]
    if composites:
        raise ValueError(f"Composite factors use core.evaluation.composite: {composites}")
    store.db.execute("CREATE OR REPLACE TEMP TABLE factor_parts("
                     "trade_date DATE, ts_code VARCHAR, factor_id VARCHAR, value DOUBLE)")
    for factor_id in selected:
        module = load_factor_compute(factor_id)
        relation = module.compute(store, start=start, end=end, input_end=input_end,
                                  spec=registry[factor_id], params=None)
        store.db.execute(f"INSERT INTO factor_parts SELECT trade_date,ts_code,factor_id,value FROM {relation}")
    for factor_id in selected:
        store.db.execute(f"DELETE FROM factor_parts WHERE factor_id={qs(factor_id)} "
                         f"AND trade_date<strptime({qs(registry[factor_id]['valid_from'])},'%Y%m%d')::DATE")
    store.db.execute("DROP TABLE IF EXISTS factor_price_input")
    store._factor_plugin_input_marker = None
    store._factor_plugin_windows_ready = False
    return "factor_parts"


def build_factor_values(store, start=None, end=None, input_end=None,
                        factor_ids=None, config_root=None) -> dict:
    compute_factor_parts(store, start, end, input_end, factor_ids, config_root)
    return store.publish("l3", "factor_value", "SELECT * FROM factor_parts",
                         ["trade_date", "ts_code", "factor_id"], partition_by="factor_id")
