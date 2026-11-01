"""Single-factor evaluation builder for the L4 products (plan Step 7).

This is the ONLY module family allowed to join factor panels to forward
labels. The daily loop is deliberately driven by the same numpy formula
functions used in the golden tests, so there is no second SQL implementation
of IC/rank/performance semantics to drift away from the hand calculations.

Output:
  factor_ic_daily         full and cap/index segment daily IC/RankIC.
  factor_quantile_return  non-overlapping rebalance groups, all h offsets,
                          one-way outgoing turnover and both sample modes.
  factor_summary          IC/ICIR/NW-t plus offset-averaged group and
                          long-short performance, full/year/in-out/cap/index.
  factor_trial_log        append-only research record (core.evaluation.trial).

Out-of-sample is opt-in: without include_out_of_sample=True the evaluation
range is clamped to sample.in_sample_end. With it, guard_oos_access runs
before any factor/label row is read.
"""
from collections import defaultdict
import logging
import math

import numpy as np
import pyarrow as pa

from core.config.settings import load_settings
from core.data.store import date_sql, qs
from core.evaluation.ops import (one_way_turnover, pearson, performance_metrics,
                                 quantile_group_ids, rank_ic, summarize_ic)
from core.evaluation.trial import append_trial_log, guard_oos_access
from core.factor.registry import factor_map
from core.market.prices import date_window


def _flush(store, table_name, buffer, columns):
    if not buffer:
        return
    frame = pa.table({name: pa.array(values) for name, values in zip(columns, zip(*buffer))})
    store.db.register(f"{table_name}_batch", frame)
    store.db.execute(f"INSERT INTO {table_name} SELECT * FROM {table_name}_batch")
    store.db.unregister(f"{table_name}_batch")
    buffer.clear()


def _date_key(value):
    return str(value).replace("-", "")


def _group_count(n, segment_id, research):
    if segment_id == "full" and n < research["sample"]["min_cross_section_count"]:
        return 0
    minimum = research["evaluation"]["quantile_min_group_size"]
    if n >= research["sample"]["quantiles"] * minimum:
        return research["sample"]["quantiles"]
    if n >= research["sample"]["min_quantiles"] * minimum:
        return research["sample"]["min_quantiles"]
    return 0


_SUMMARY_IDS = ["factor_id", "universe_id", "process_id", "label_id",
                "sample_mode", "segment_id", "period_id"]
_SUMMARY_INTS = ["observations", "excluded_days", "ic_n_days", "rank_ic_n_days",
                 "rebalance_offsets", "group1_n_periods", "long_short_n_periods"]
_PERFORMANCE_METRICS = ["cumulative_return", "annualized_return", "mean_period_return",
                        "std_period_return", "sharpe", "max_drawdown", "calmar",
                        "win_rate", "profit_loss_ratio"]
_SUMMARY_DOUBLES = [
    "mean_cross_section", "ic_mean", "ic_std", "ic_win_rate",
    "rank_ic_mean", "rank_ic_std", "rank_ic_win_rate", "rank_icir",
    "annualized_rank_icir", "rank_ic_t_stat", "factor_rank_autocorrelation",
    "mean_group_count", "monotonicity", "group1_mean_one_way_turnover",
    "group1_breakeven_cost", "long_short_mean_one_way_turnover",
    "long_short_breakeven_cost",
] + [f"{prefix}_{metric}" for prefix in ("group1", "long_short")
     for metric in _PERFORMANCE_METRICS]


def _quantile_period_rows(table_name, in_sample_end):
    ids = ", ".join(_SUMMARY_IDS[:-1])
    values = "trade_date, rebalance_offset, group_id, group_count, ret, turnover"
    split = f"CASE WHEN trade_date <= {date_sql(qs(in_sample_end))} THEN 'in_sample' ELSE 'out_of_sample' END"
    return f"""
        SELECT {ids}, 'full' AS period_id, {values} FROM {table_name} WHERE segment_id='full'
        UNION ALL
        SELECT {ids}, 'year:' || CAST(year(trade_date) AS VARCHAR), {values}
        FROM {table_name} WHERE segment_id='full'
        UNION ALL
        SELECT {ids}, {split}, {values} FROM {table_name} WHERE segment_id='full'
        UNION ALL
        SELECT {ids}, segment_id AS period_id, {values} FROM {table_name} WHERE segment_id<>'full'
    """


def _ic_period_rows(table_name, in_sample_end):
    ids = ", ".join(_SUMMARY_IDS[:-1])
    values = "trade_date, n, ic, rank_ic, exclusion_reason"
    split = f"CASE WHEN trade_date <= {date_sql(qs(in_sample_end))} THEN 'in_sample' ELSE 'out_of_sample' END"
    return f"""
        SELECT {ids}, 'full' AS period_id, {values} FROM {table_name} WHERE segment_id='full'
        UNION ALL
        SELECT {ids}, 'year:' || CAST(year(trade_date) AS VARCHAR), {values}
        FROM {table_name} WHERE segment_id='full'
        UNION ALL
        SELECT {ids}, {split}, {values} FROM {table_name} WHERE segment_id='full'
        UNION ALL
        SELECT {ids}, segment_id AS period_id, {values} FROM {table_name} WHERE segment_id<>'full'
    """


def _mean(values):
    values = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    return float(np.mean(values)) if values else None


def _performance_summary(paths, periods_per_year):
    performances = [performance_metrics(returns, dates, periods_per_year)
                    for dates, returns in paths]
    result = {metric: _mean([item[metric] for item in performances])
              for metric in _PERFORMANCE_METRICS}
    result["n_periods"] = sum(item["n_periods"] for item in performances)
    drawdowns = [item for item in performances if item["max_drawdown"] is not None]
    if drawdowns:
        worst = min(drawdowns, key=lambda item: item["max_drawdown"])
        result["max_drawdown_start"] = worst["max_drawdown_start"]
        result["max_drawdown_end"] = worst["max_drawdown_end"]
    else:
        result["max_drawdown_start"] = result["max_drawdown_end"] = None
    return result


def _add_quantile_summary(row, offset_rows, monotonicity_rows, annualization_days):
    row.update({name: None for name in _SUMMARY_DOUBLES if name.startswith(("group1_", "long_short_"))})
    row.update(mean_group_count=None, monotonicity=None,
               group1_mean_one_way_turnover=None, group1_breakeven_cost=None,
               long_short_mean_one_way_turnover=None, long_short_breakeven_cost=None,
               rebalance_offsets=0, group1_n_periods=0, long_short_n_periods=0)
    if not offset_rows:
        return
    horizon = int(row["label_id"].rsplit("_", 1)[1])
    periods_per_year = annualization_days / horizon
    long_paths, long_short_paths = [], []
    long_turnovers, long_short_turnovers = [], []
    group_counts = []
    for offset_row in sorted(offset_rows, key=lambda item: item["rebalance_offset"]):
        dates = offset_row["dates"]
        long_returns = np.asarray(offset_row["long_returns"], dtype=np.float64)
        short_returns = np.asarray(offset_row["short_returns"], dtype=np.float64)
        long_paths.append((dates, long_returns))
        long_short_paths.append((dates, long_returns - short_returns))
        long_turnover = _mean(offset_row["long_turnovers"])
        short_turnover = _mean(offset_row["short_turnovers"])
        if long_turnover is not None:
            long_turnovers.append(long_turnover)
        if long_turnover is not None and short_turnover is not None:
            long_short_turnovers.append(long_turnover + short_turnover)
        group_counts.append(_mean(offset_row["group_counts"]))
    long_performance = _performance_summary(long_paths, periods_per_year)
    long_short_performance = _performance_summary(long_short_paths, periods_per_year)
    for metric, value in long_performance.items():
        row[f"group1_{metric}"] = value
    for metric, value in long_short_performance.items():
        row[f"long_short_{metric}"] = value
    row["rebalance_offsets"] = len(offset_rows)
    row["mean_group_count"] = _mean(group_counts)
    row["group1_mean_one_way_turnover"] = _mean(long_turnovers)
    row["long_short_mean_one_way_turnover"] = _mean(long_short_turnovers)
    long_gross = long_performance["mean_period_return"]
    long_short_gross = long_short_performance["mean_period_return"]
    if long_gross is not None and long_gross > 0 and row["group1_mean_one_way_turnover"]:
        row["group1_breakeven_cost"] = long_gross / row["group1_mean_one_way_turnover"]
    if (long_short_gross is not None and long_short_gross > 0
            and row["long_short_mean_one_way_turnover"]):
        row["long_short_breakeven_cost"] = (
            long_short_gross / row["long_short_mean_one_way_turnover"])
    monotonicities = []
    for groups in monotonicity_rows.values():
        ordered = sorted(groups)
        if len(ordered) >= 2:
            value = rank_ic([group_id for group_id, _ in ordered],
                            [group_return for _, group_return in ordered])
            if value is not None:
                monotonicities.append(-value)
    row["monotonicity"] = _mean(monotonicities)


def _build_summary(store, research, autocorrelations):
    in_sample_end = research["sample"]["in_sample_end"]
    annualization_days = research["evaluation"]["annualization_days"]
    ids = ", ".join(_SUMMARY_IDS)
    order = ", ".join(_SUMMARY_IDS)
    ic_sql = f"""
        SELECT {ids}, count(*) AS observations,
               list(n ORDER BY trade_date) AS ns,
               list(ic ORDER BY trade_date) AS ics,
               list(rank_ic ORDER BY trade_date) AS rank_ics,
               list(exclusion_reason ORDER BY trade_date) AS exclusions
        FROM ({_ic_period_rows('evaluation_ic', in_sample_end)})
        GROUP BY {order} ORDER BY {order}
    """
    rows = {}
    for item in store.rows(ic_sql):
        key = tuple(item[name] for name in _SUMMARY_IDS)
        row = {name: item[name] for name in _SUMMARY_IDS}
        horizon = int(row["label_id"].rsplit("_", 1)[1])
        stats = summarize_ic(item["ics"], item["rank_ics"], horizon, annualization_days)
        row.update(observations=item["observations"],
                   excluded_days=sum(reason is not None for reason in item["exclusions"]),
                   mean_cross_section=_mean(item["ns"]),
                   ic_n_days=stats["ic"]["n_days"], ic_mean=stats["ic"]["mean"],
                   ic_std=stats["ic"]["std"], ic_win_rate=stats["ic"]["win_rate"],
                   rank_ic_n_days=stats["rank_ic"]["n_days"],
                   rank_ic_mean=stats["rank_ic"]["mean"], rank_ic_std=stats["rank_ic"]["std"],
                   rank_ic_win_rate=stats["rank_ic"]["win_rate"],
                   rank_icir=stats["rank_icir"],
                   annualized_rank_icir=stats["annualized_rank_icir"],
                   rank_ic_t_stat=stats["rank_ic_t_stat"],
                   factor_rank_autocorrelation=autocorrelations.get(
                       (row["universe_id"], row["factor_id"], row["process_id"])))
        _add_quantile_summary(row, [], {}, annualization_days)
        rows[key] = row

    quantile_periods = _quantile_period_rows("evaluation_quantile", in_sample_end)
    quantile_sql = f"""
        WITH daily AS (
            SELECT {ids}, rebalance_offset, trade_date,
                   max(CASE WHEN group_id=1 THEN ret END) AS long_ret,
                   max(CASE WHEN group_id=group_count THEN ret END) AS short_ret,
                   max(CASE WHEN group_id=1 THEN turnover END) AS long_turnover,
                   max(CASE WHEN group_id=group_count THEN turnover END) AS short_turnover,
                   max(group_count) AS group_count
            FROM ({quantile_periods})
            GROUP BY {order}, rebalance_offset, trade_date
        )
        SELECT {ids}, rebalance_offset,
               list(trade_date ORDER BY trade_date) AS dates,
               list(long_ret ORDER BY trade_date) AS long_returns,
               list(short_ret ORDER BY trade_date) AS short_returns,
               list(long_turnover ORDER BY trade_date) AS long_turnovers,
               list(short_turnover ORDER BY trade_date) AS short_turnovers,
               list(group_count ORDER BY trade_date) AS group_counts
        FROM daily GROUP BY {order}, rebalance_offset ORDER BY {order}, rebalance_offset
    """
    offsets = defaultdict(list)
    for item in store.rows(quantile_sql):
        key = tuple(item[name] for name in _SUMMARY_IDS)
        offsets[key].append(item)

    monotonicity_sql = f"""
        SELECT {ids}, rebalance_offset, group_id,
               (sum(CAST(ret AS DECIMAL(38,18))) / count(ret))::DOUBLE AS mean_return
        FROM ({quantile_periods})
        GROUP BY {order}, rebalance_offset, group_id
        ORDER BY {order}, rebalance_offset, group_id
    """
    monotonicities = defaultdict(lambda: defaultdict(list))
    for item in store.rows(monotonicity_sql):
        key = tuple(item[name] for name in _SUMMARY_IDS)
        if item["mean_return"] is not None:
            monotonicities[key][item["rebalance_offset"]].append(
                (item["group_id"], item["mean_return"]))
    for key, offset_rows in offsets.items():
        if key not in rows:
            raise ValueError(f"Quantile rows lack matching IC summary key: {key}")
        _add_quantile_summary(rows[key], offset_rows, monotonicities[key], annualization_days)
    return [rows[key] for key in sorted(rows)]


def _summary_frame(rows):
    fields = ([pa.field(name, pa.string()) for name in _SUMMARY_IDS]
              + [pa.field(name, pa.int64()) for name in _SUMMARY_INTS]
              + [pa.field(name, pa.float64()) for name in _SUMMARY_DOUBLES]
              + [pa.field(f"{prefix}_max_drawdown_{edge}", pa.string())
                 for prefix in ("group1", "long_short") for edge in ("start", "end")])
    schema = pa.schema(fields)
    return pa.Table.from_arrays(
        [pa.array([row.get(field.name) for row in rows], type=field.type) for field in schema],
        schema=schema)


def evaluate_factors(store, start=None, end=None, factor_ids=None, process_ids=None,
                     horizons=None, include_out_of_sample=False, config_root=None,
                     merge_existing=False) -> dict:
    research = load_settings("factor_research.yaml", config_root)
    engine = research["engine"]
    store.db.execute(f"SET memory_limit={qs(engine['memory_limit'])}")
    store.db.execute(f"SET threads={int(engine['threads'])}")
    registry = factor_map(config_root)
    selected = list(factor_ids) if factor_ids is not None else list(registry)
    unknown = sorted(set(selected) - set(registry))
    if unknown:
        raise ValueError(f"Unregistered factor_ids: {unknown}")
    horizons = list(horizons or research["labels"]["horizons"])
    if any(h not in research["labels"]["horizons"] for h in horizons):
        raise ValueError(f"Unsupported evaluation horizons: {horizons}")
    start = start or research["sample"]["evaluation_start"]
    in_sample_end = research["sample"]["in_sample_end"]
    if include_out_of_sample:
        end = end or research["sample"]["quote_coverage_end"]
        if _date_key(end) > _date_key(in_sample_end):
            guard_oos_access(store, selected, config_root)
    else:
        end = end or in_sample_end
        if _date_key(end) > _date_key(in_sample_end):
            end = in_sample_end
    if _date_key(start) > _date_key(end):
        raise ValueError("Empty evaluation range after the out-of-sample clamp")
    processes = process_ids or [p["process_id"] for p in research["preprocessing"]["processes"]]
    primary = set(research["evaluation"]["segment_horizons"])
    universes = research["preprocessing"]["universes"]
    existing = {}
    if merge_existing:
        for name in ("factor_ic_daily", "factor_quantile_return", "factor_summary"):
            existing[name] = store.output_relation("l4", name)
    excluded = ", ".join(qs(factor_id) for factor_id in selected)

    def publication_query(name, temp_table):
        if not merge_existing:
            return f"SELECT * FROM {temp_table}"
        return (f"(SELECT * FROM {existing[name]} WHERE factor_id NOT IN ({excluded})) "
                f"UNION ALL SELECT * FROM {temp_table}")

    store.db.execute("""CREATE OR REPLACE TEMP TABLE evaluation_ic(
        trade_date DATE, factor_id VARCHAR, universe_id VARCHAR, process_id VARCHAR,
        label_id VARCHAR, sample_mode VARCHAR, segment_id VARCHAR,
        n INTEGER, ic DOUBLE, rank_ic DOUBLE, exclusion_reason VARCHAR)""")
    store.db.execute("""CREATE OR REPLACE TEMP TABLE evaluation_quantile(
        trade_date DATE, factor_id VARCHAR, universe_id VARCHAR, process_id VARCHAR,
        label_id VARCHAR, sample_mode VARCHAR, segment_id VARCHAR,
        rebalance_offset INTEGER, group_id INTEGER, group_count INTEGER,
        ret DOUBLE, n INTEGER, turnover DOUBLE)""")
    ic_buffer, quantile_buffer = [], []
    autocorrelations = {}

    processed = store.output_relation("l3", "factor_processed")
    factor_values = store.output_relation("l3", "factor_value")
    labels = store.output_relation("l3", "forward_return")
    calendar = store.output_relation("l1", "daily_calendar")
    index_members = store.output_relation("l3", "daily_index_member")
    index_codes = {item["benchmark_id"]: item["index_code"]
                   for item in research["benchmark"]["index_universes"]}
    label_joins = []
    for h in horizons:
        label_joins.append(f"""LEFT JOIN (SELECT trade_date, ts_code, ret, entry_open_at_up_limit
            FROM {labels} WHERE label_id='ret_o2o_{h}') l{h}
            ON l{h}.trade_date=p.trade_date AND l{h}.ts_code=p.ts_code""")
    index_joins = []
    for segment_id, index_code in index_codes.items():
        index_joins.append(f"""LEFT JOIN (SELECT DISTINCT trade_date, con_code AS ts_code
            FROM {index_members} WHERE index_code='{index_code}') {segment_id}
            ON {segment_id}.trade_date=p.trade_date AND {segment_id}.ts_code=p.ts_code""")

    for universe_id in universes:
        for factor_id in selected:
            expected_direction = registry[factor_id]["expected_direction"]
            for process_id in processes:
                logging.info("Evaluating universe=%s factor=%s process=%s",
                             universe_id, factor_id, process_id)
                previous_autocorr = None
                autocorr_values = []
                previous_members = {}
                query = f"""
                    SELECT p.trade_date, p.ts_code, c.trade_index, p.value, mv.value AS log_circ_mv,
                           {', '.join(f'l{h}.ret AS ret_{h}, l{h}.entry_open_at_up_limit AS limit_{h}' for h in horizons)},
                           {', '.join(f'({segment_id}.ts_code IS NOT NULL) AS member_{segment_id}' for segment_id in index_codes)}
                    FROM (SELECT * FROM {processed} WHERE factor_id={qs(factor_id)}
                          AND process_id={qs(process_id)} AND universe_id={qs(universe_id)}) p
                    JOIN {calendar} c ON c.trade_date=p.trade_date
                    LEFT JOIN (SELECT trade_date, ts_code, value FROM {factor_values}
                               WHERE factor_id='log_circ_mv') mv
                      ON mv.trade_date=p.trade_date AND mv.ts_code=p.ts_code
                    {' '.join(label_joins)}
                    {' '.join(index_joins)}
                    WHERE {date_window(store, start, end, 'p.trade_date')}
                    ORDER BY p.trade_date, p.ts_code
                """
                table = store.db.execute(query).to_arrow_table()
                if table.num_rows == 0:
                    continue
                dates_np = table.column("trade_date").to_numpy()
                dates = table.column("trade_date").to_pylist()
                codes = table.column("ts_code").to_pylist()
                trade_indices = table.column("trade_index").to_numpy()
                values = table.column("value").to_numpy(zero_copy_only=False).astype(np.float64)
                market_caps = table.column("log_circ_mv").to_numpy(zero_copy_only=False).astype(np.float64)
                returns = {h: table.column(f"ret_{h}").to_numpy(zero_copy_only=False).astype(np.float64)
                           for h in horizons}
                limits = {h: np.array(table.column(f"limit_{h}").to_pylist(), dtype=object) for h in horizons}
                memberships = {s: np.array(table.column(f"member_{s}").to_pylist(), dtype=bool)
                               for s in index_codes}
                boundaries = np.flatnonzero(np.r_[True, dates_np[1:] != dates_np[:-1]])
                starts = np.r_[boundaries, len(dates_np)]
                for lo, hi in zip(starts[:-1], starts[1:]):
                    trade_date = dates[lo]
                    day_codes = codes[lo:hi]
                    day_values = values[lo:hi]
                    current_factor = dict(zip(day_codes, day_values))
                    if previous_autocorr is not None:
                        common = sorted(set(current_factor) & set(previous_autocorr))
                        if len(common) >= 2:
                            correlation = rank_ic([current_factor[c] for c in common],
                                                  [previous_autocorr[c] for c in common])
                            if correlation is not None:
                                autocorr_values.append(correlation)
                    previous_autocorr = current_factor
                    cap_segment = np.zeros(hi - lo, dtype=int)
                    valid_cap = ~np.isnan(market_caps[lo:hi])
                    if np.count_nonzero(valid_cap) >= 3:
                        cap_segment[valid_cap] = quantile_group_ids(
                            -market_caps[lo:hi][valid_cap], 1, 3)  # cap_1 = smallest
                    segments = [("full", np.ones(hi - lo, dtype=bool))]
                    if primary.intersection(horizons):
                        for cap_id in (1, 2, 3):
                            segments.append((f"cap_{cap_id}", cap_segment == cap_id))
                        for segment_id in index_codes:
                            segments.append((segment_id, memberships[segment_id][lo:hi]))
                    for h in horizons:
                        ret_all = returns[h][lo:hi]
                        limit_all = limits[h][lo:hi]
                        for sample_mode in ("theoretical", "tradable"):
                            base_valid = ~np.isnan(ret_all)
                            if sample_mode == "tradable":
                                base_valid &= np.array([flag is not True for flag in limit_all])
                            for segment_id, segment_mask in segments:
                                if segment_id != "full" and h not in primary:
                                    continue
                                valid = base_valid & segment_mask
                                n = int(np.count_nonzero(valid))
                                group_count = _group_count(n, segment_id, research)
                                exclusion = None
                                if segment_id == "full" and n < research["sample"]["min_cross_section_count"]:
                                    exclusion = "insufficient_cross_section"
                                elif group_count == 0:
                                    exclusion = "insufficient_quantile_coverage"
                                x = day_values[valid]
                                y = ret_all[valid]
                                ic = pearson(x, y) if exclusion is None else None
                                ric = rank_ic(x, y) if exclusion is None else None
                                ic_buffer.append((trade_date, factor_id, universe_id, process_id,
                                                  f"ret_o2o_{h}", sample_mode, segment_id,
                                                  n, ic, ric, exclusion))
                                if exclusion is not None:
                                    continue
                                groups = quantile_group_ids(x, expected_direction, group_count)
                                offset = int(trade_indices[lo] % h)
                                selected_codes = np.asarray(day_codes, dtype=object)[valid]
                                for group_id in range(1, group_count + 1):
                                    member_mask = groups == group_id
                                    members = selected_codes[member_mask]
                                    key = (segment_id, h, sample_mode, offset, group_id)
                                    turnover = one_way_turnover(previous_members.get(key, []), members)
                                    previous_members[key] = set(members)
                                    quantile_buffer.append((
                                        trade_date, factor_id, universe_id, process_id,
                                        f"ret_o2o_{h}", sample_mode, segment_id,
                                        offset, group_id, group_count,
                                        float(np.mean(y[member_mask])) if np.any(member_mask) else None,
                                        int(np.count_nonzero(member_mask)), turnover))
                    if len(ic_buffer) >= 200000:
                        _flush(store, "evaluation_ic", ic_buffer,
                               ["trade_date", "factor_id", "universe_id", "process_id", "label_id",
                                "sample_mode", "segment_id", "n", "ic", "rank_ic", "exclusion_reason"])
                    if len(quantile_buffer) >= 200000:
                        _flush(store, "evaluation_quantile", quantile_buffer,
                               ["trade_date", "factor_id", "universe_id", "process_id", "label_id",
                                "sample_mode", "segment_id", "rebalance_offset", "group_id",
                                "group_count", "ret", "n", "turnover"])
                autocorrelations[(universe_id, factor_id, process_id)] = (
                    float(np.mean(autocorr_values)) if autocorr_values else None)
                del table
    _flush(store, "evaluation_ic", ic_buffer,
           ["trade_date", "factor_id", "universe_id", "process_id", "label_id",
            "sample_mode", "segment_id", "n", "ic", "rank_ic", "exclusion_reason"])
    _flush(store, "evaluation_quantile", quantile_buffer,
           ["trade_date", "factor_id", "universe_id", "process_id", "label_id",
            "sample_mode", "segment_id", "rebalance_offset", "group_id", "group_count",
            "ret", "n", "turnover"])

    ic_result = store.publish("l4", "factor_ic_daily", publication_query("factor_ic_daily", "evaluation_ic"),
                              ["trade_date", "factor_id", "universe_id", "process_id",
                               "label_id", "sample_mode", "segment_id"], partition_by="factor_id")
    quantile_result = store.publish(
        "l4", "factor_quantile_return", publication_query("factor_quantile_return", "evaluation_quantile"),
                                    ["trade_date", "factor_id", "universe_id", "process_id",
                                     "label_id", "sample_mode", "segment_id", "rebalance_offset", "group_id"],
                                    partition_by="factor_id")
    summary_rows = _build_summary(store, research, autocorrelations)
    summary_frame = _summary_frame(summary_rows)
    store.db.register("evaluation_summary_frame", summary_frame)
    summary_result = store.publish(
        "l4", "factor_summary", publication_query("factor_summary", "evaluation_summary_frame"),
                                   ["factor_id", "universe_id", "process_id", "label_id",
                                    "sample_mode", "segment_id", "period_id"], partition_by="factor_id")
    store.db.unregister("evaluation_summary_frame")
    records = []
    for factor_id in selected:
        records.append({
            "factor_id": factor_id, "factor_version": registry[factor_id]["version"],
            "process_ids": processes, "universe_ids": universes, "horizons": horizons,
            "evaluation_start": str(start), "evaluation_end": str(end),
            "oos_accessed": bool(include_out_of_sample and _date_key(end) > _date_key(in_sample_end)),
            "ic_daily_sha256": ic_result["sha256"], "quantile_return_sha256": quantile_result["sha256"],
            "summary_sha256": summary_result["sha256"], "config_root": config_root,
        })
    trial_result = append_trial_log(store, records)
    return {"factor_ic_daily": ic_result, "factor_quantile_return": quantile_result,
            "factor_summary": summary_result, "factor_trial_log": trial_result}
