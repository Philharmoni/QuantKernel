"""Step 8 transparent factor composites and their cutoff-safe rolling weights.

This module is deliberately in core.evaluation: it is the only family allowed
to join factor panels to labels. Equal weights use direction-adjusted ws_z
scores. Rolling weights use signed RankICIR over the latest eligible in-sample
IC observations; an IC day is eligible at T only when every label used in that
cross-section has actually exited before T (max(exit_date) < T).
"""
from collections import defaultdict
from datetime import datetime
import hashlib
import json
import logging
import math

import numpy as np
import pyarrow as pa

from core.config.settings import load_settings
from core.data.store import json_text, qs
from core.evaluation.ops import rank_ic
from core.factor.registry import factor_map
from core.market.prices import date_window


def _date_key(value):
    return str(value).replace("-", "")


def _parse_date(value):
    text = str(value)
    return datetime.strptime(text, "%Y%m%d" if "-" not in text else "%Y-%m-%d").date()


def composition_settings(config_root=None):
    research = load_settings("factor_research.yaml", config_root)
    composition = research["composition"]
    registry = factor_map(config_root)
    components = list(composition["components"])
    if len(components) != len(set(components)) or not components:
        raise ValueError("composition.components must be non-empty and unique")
    unknown = sorted(set(components) - set(registry))
    if unknown:
        raise ValueError(f"Unregistered composition components: {unknown}")
    if any(registry[factor_id]["category"] == "composite" for factor_id in components):
        raise ValueError("Composite factors cannot be composition components")
    if any(registry[factor_id]["expected_direction"] == 0 for factor_id in components):
        raise ValueError("Zero-direction components are not supported")
    specs = composition["composites"]
    ids = [spec["composite_id"] for spec in specs]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate composite_id")
    if set(ids) != {factor_id for factor_id, spec in registry.items()
                    if spec["category"] == "composite"}:
        raise ValueError("composition.composites and registry composite factors must match")
    for spec in specs:
        if spec["method"] not in ("equal_weight", "rolling_icir"):
            raise ValueError(f"Unsupported composite method: {spec['method']}")
        if spec["method"] == "rolling_icir":
            if spec["label_id"] not in {f"ret_o2o_{h}" for h in research["labels"]["horizons"]}:
                raise ValueError(f"Unsupported rolling-ICIR label: {spec['label_id']}")
            if not 1 <= spec["min_periods"] <= spec["lookback_observations"]:
                raise ValueError("rolling_icir requires 1 <= min_periods <= lookback_observations")
    return research, composition, registry


def signed_icir_weights(histories, trade_date, lookback, min_periods, in_sample_end):
    """Return per-component rolling stats; only fully realized in-sample IC days.

    histories maps component_id to rows with signal_date/max_exit_date/rank_ic.
    The strict inequality max_exit_date < trade_date is intentional: a label
    exiting at T is not available for forming T's close-to-close factor score.
    """
    trade_date = _parse_date(trade_date)
    in_sample_end = _parse_date(in_sample_end)
    result = {}
    for component_id, rows in histories.items():
        eligible = [row for row in rows
                    if row["rank_ic"] is not None
                    and row["signal_date"] <= in_sample_end
                    and row["max_exit_date"] <= in_sample_end
                    and row["max_exit_date"] < trade_date]
        eligible.sort(key=lambda row: row["signal_date"])
        selected = eligible[-lookback:]
        values = np.asarray([row["rank_ic"] for row in selected], dtype=np.float64)
        mean = float(np.mean(values)) if len(values) >= min_periods else None
        std = float(np.std(values)) if len(values) >= min_periods else None
        icir = mean / std if mean is not None and std > 0 else None
        result[component_id] = {
            "n_history": len(selected), "ic_mean": mean, "ic_std": std, "icir": icir,
            "last_signal_date": selected[-1]["signal_date"] if selected else None,
            "last_exit_date": max(row["max_exit_date"] for row in selected) if selected else None,
            "weight": 0.0,
        }
    denominator = sum(abs(item["icir"]) for item in result.values() if item["icir"] is not None)
    if denominator > 0:
        for item in result.values():
            if item["icir"] is not None:
                item["weight"] = item["icir"] / denominator
    return result


def _flush(store, table_name, buffer, columns):
    if not buffer:
        return
    frame = pa.table({name: pa.array(values) for name, values in zip(columns, zip(*buffer))})
    store.db.register(f"{table_name}_batch", frame)
    store.db.execute(f"INSERT INTO {table_name} SELECT * FROM {table_name}_batch")
    store.db.unregister(f"{table_name}_batch")
    buffer.clear()


def _build_ic_history(store, research, composition, registry, input_end):
    in_sample_end = research["sample"]["in_sample_end"]
    end = min(_date_key(input_end or in_sample_end), _date_key(in_sample_end))
    start = research["sample"]["evaluation_start"]
    if _date_key(start) > end:
        raise ValueError("No in-sample IC history available at this input cutoff")
    store.db.execute("""CREATE OR REPLACE TEMP TABLE composite_ic_history(
        signal_date DATE, factor_id VARCHAR, universe_id VARCHAR, process_id VARCHAR,
        label_id VARCHAR, n INTEGER, rank_ic DOUBLE, max_exit_date DATE)""")
    processed = store.output_relation("l3", "factor_processed")
    labels = store.output_relation("l3", "forward_return")
    rolling_specs = [spec for spec in composition["composites"] if spec["method"] == "rolling_icir"]
    if not rolling_specs:
        return
    if len({spec["label_id"] for spec in rolling_specs}) != 1:
        raise ValueError("All rolling composites must share one label_id in a single build")
    label_id = rolling_specs[0]["label_id"]
    columns = ["signal_date", "factor_id", "universe_id", "process_id", "label_id",
               "n", "rank_ic", "max_exit_date"]
    buffer = []
    window = date_window(store, start, end, "p.trade_date")
    for factor_id in composition["components"]:
        direction = registry[factor_id]["expected_direction"]
        query = f"""
            SELECT p.trade_date, p.ts_code, p.value, l.ret, l.exit_date
            FROM (SELECT * FROM {processed} WHERE factor_id={qs(factor_id)}
                  AND universe_id={qs(composition['universe_id'])}
                  AND process_id={qs(composition['process_id'])}) p
            JOIN (SELECT trade_date, ts_code, ret, exit_date FROM {labels}
                  WHERE label_id={qs(label_id)}) l
              ON l.trade_date=p.trade_date AND l.ts_code=p.ts_code
            WHERE {window} ORDER BY p.trade_date, p.ts_code
        """
        table = store.db.execute(query).to_arrow_table()
        if not table.num_rows:
            continue
        dates_np = table.column("trade_date").to_numpy()
        dates = table.column("trade_date").to_pylist()
        values = table.column("value").to_numpy(zero_copy_only=False).astype(np.float64)
        returns = table.column("ret").to_numpy(zero_copy_only=False).astype(np.float64)
        exits = table.column("exit_date").to_pylist()
        boundaries = np.flatnonzero(np.r_[True, dates_np[1:] != dates_np[:-1]])
        starts = np.r_[boundaries, len(dates_np)]
        for lo, hi in zip(starts[:-1], starts[1:]):
            valid = ~np.isnan(returns[lo:hi])
            n = int(np.count_nonzero(valid))
            if n < research["sample"]["min_cross_section_count"]:
                continue
            day_exits = [value for value, keep in zip(exits[lo:hi], valid) if keep]
            if any(value is None for value in day_exits):
                raise ValueError(f"Non-null label with null exit_date at {dates[lo]} / {factor_id}")
            max_exit = max(day_exits)
            # Do not even evaluate an IC cross-section whose labels were not all
            # realized by this build's cutoff (in-sample end or earlier D).
            if max_exit > _parse_date(end):
                continue
            correlation = rank_ic(direction * values[lo:hi][valid], returns[lo:hi][valid])
            if correlation is not None:
                buffer.append((dates[lo], factor_id, composition["universe_id"],
                               composition["process_id"], label_id, n, correlation, max_exit))
        if buffer:
            _flush(store, "composite_ic_history", buffer, columns)
        del table


def _build_weights(store, research, composition, registry, start, end, input_end):
    store.db.execute("""CREATE OR REPLACE TEMP TABLE composite_weights(
        trade_date DATE, composite_id VARCHAR, component_id VARCHAR, method VARCHAR,
        direction INTEGER, n_history INTEGER, ic_mean DOUBLE, ic_std DOUBLE,
        icir DOUBLE, weight DOUBLE, last_signal_date DATE, last_exit_date DATE)""")
    calendar = store.output_relation("l1", "daily_calendar")
    window = date_window(store, start, end, "trade_date")
    cutoff = f" AND trade_date<=DATE {qs(str(input_end))}" if input_end else ""
    dates = [row["trade_date"] for row in store.rows(
        f"SELECT trade_date FROM {calendar} WHERE {window}{cutoff} ORDER BY trade_date")]
    history_rows = store.rows("""SELECT factor_id, signal_date, max_exit_date, rank_ic
        FROM composite_ic_history ORDER BY factor_id, signal_date""")
    histories = defaultdict(list)
    for row in history_rows:
        histories[row["factor_id"]].append(row)
    columns = ["trade_date", "composite_id", "component_id", "method", "direction",
               "n_history", "ic_mean", "ic_std", "icir", "weight",
               "last_signal_date", "last_exit_date"]
    buffer = []
    components = composition["components"]
    for trade_date in dates:
        for spec in composition["composites"]:
            if spec["method"] == "equal_weight":
                stats = {factor_id: {"n_history": 0, "ic_mean": None, "ic_std": None,
                                     "icir": None, "weight": 1.0 / len(components),
                                     "last_signal_date": None, "last_exit_date": None}
                         for factor_id in components}
            else:
                stats = signed_icir_weights(
                    {factor_id: histories.get(factor_id, []) for factor_id in components},
                    trade_date, spec["lookback_observations"],
                    spec["min_periods"], research["sample"]["in_sample_end"])
            for factor_id in components:
                item = stats[factor_id]
                buffer.append((trade_date, spec["composite_id"], factor_id, spec["method"],
                               registry[factor_id]["expected_direction"], item["n_history"],
                               item["ic_mean"], item["ic_std"], item["icir"], item["weight"],
                               item["last_signal_date"], item["last_exit_date"]))
        if len(buffer) >= 100000:
            _flush(store, "composite_weights", buffer, columns)
    _flush(store, "composite_weights", buffer, columns)


def compute_composite_parts(store, start=None, end=None, input_end=None, config_root=None):
    """Build composite temp tables with every dynamic input cut off at input_end."""
    research, composition, registry = composition_settings(config_root)
    engine = research["engine"]
    store.db.execute(f"SET memory_limit={qs(engine['memory_limit'])}")
    store.db.execute(f"SET threads={int(engine['threads'])}")
    start = start or research["sample"]["evaluation_start"]
    end = end or research["sample"]["quote_coverage_end"]
    logging.info("Building realized IC history for %d components", len(composition["components"]))
    _build_ic_history(store, research, composition, registry, input_end)
    logging.info("Building composite weights through %s", end)
    _build_weights(store, research, composition, registry, start, end, input_end)
    processed = store.output_relation("l3", "factor_processed")
    cutoff = f" AND p.trade_date<=DATE {qs(str(input_end))}" if input_end else ""
    store.db.execute(f"""CREATE OR REPLACE TEMP TABLE factor_composite_parts AS
        WITH weighted AS (
            SELECT w.trade_date, w.composite_id, p.ts_code, w.weight, w.direction, p.value
            FROM composite_weights w
            JOIN (SELECT * FROM {processed} WHERE universe_id={qs(composition['universe_id'])}
                  AND process_id={qs(composition['process_id'])}) p
              ON p.trade_date=w.trade_date AND p.factor_id=w.component_id
            WHERE 1=1{cutoff}
        ), aggregated AS (
            SELECT trade_date, composite_id, ts_code,
                   sum(CAST(weight*direction*value AS DECIMAL(38,24))) AS numerator,
                   sum(CAST(abs(weight) AS DECIMAL(38,18))) AS denominator,
                   count(*) AS component_count
            FROM weighted GROUP BY 1,2,3
        )
        SELECT trade_date, ts_code, composite_id AS factor_id,
               (numerator/denominator)::DOUBLE AS value
        FROM aggregated
        WHERE component_count>={int(composition['min_component_count'])}
          AND denominator>0 AND isfinite((numerator/denominator)::DOUBLE)
    """)
    for spec in composition["composites"]:
        factor_id = spec["composite_id"]
        store.db.execute(f"DELETE FROM factor_composite_parts WHERE factor_id={qs(factor_id)} "
                         f"AND trade_date<strptime({qs(registry[factor_id]['valid_from'])},'%Y%m%d')::DATE")
    return "factor_composite_parts"


def build_composite_factors(store, start=None, end=None, input_end=None, config_root=None) -> dict:
    research, composition, _ = composition_settings(config_root)
    compute_composite_parts(store, start, end, input_end, config_root)
    realized = store.publish(
        "l4", "factor_ic_realized", "SELECT * FROM composite_ic_history",
        ["signal_date", "factor_id", "universe_id", "process_id", "label_id"],
        partition_by="factor_id")
    weights = store.publish(
        "l4", "factor_composite_weight", "SELECT * FROM composite_weights",
        ["trade_date", "composite_id", "component_id"], partition_by="composite_id")
    composite_values = store.publish(
        "l3", "factor_composite_value", "SELECT * FROM factor_composite_parts",
        ["trade_date", "ts_code", "factor_id"], partition_by="factor_id")
    existing = store.output_relation("l3", "factor_value")
    composite_ids = [spec["composite_id"] for spec in composition["composites"]]
    excluded = ", ".join(qs(factor_id) for factor_id in composite_ids)
    merged = store.publish(
        "l3", "factor_value",
        f"(SELECT * FROM {existing} WHERE factor_id NOT IN ({excluded})) "
        "UNION ALL SELECT * FROM factor_composite_parts",
        ["trade_date", "ts_code", "factor_id"], partition_by="factor_id")
    digest = hashlib.sha256(json_text(composition).encode("utf-8")).hexdigest()
    report = {"composition_sha256": digest, "components": composition["components"],
              "composite_ids": composite_ids, "input_end": input_end,
              "start": start or research["sample"]["evaluation_start"],
              "end": end or research["sample"]["quote_coverage_end"],
              "rows": {name: manifest["rows"] for name, manifest in
                       (("factor_ic_realized", realized), ("factor_composite_weight", weights),
                        ("factor_composite_value", composite_values), ("factor_value", merged))}}
    store.write_json(("l3", "factor_composite_value", "composition_report.json"), report)
    return {"factor_ic_realized": realized, "factor_composite_weight": weights,
            "factor_composite_value": composite_values, "factor_value": merged,
            "composition_sha256": digest}
