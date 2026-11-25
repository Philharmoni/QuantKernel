"""Build L3 factor_processed: per-day cross-sectional preprocessing panels.

Stage 2 source directory: only L1/L2/L3 products are consumed, never raw
tables and never core.label (enforced by core.factor.entry guards).

For each (universe, factor, trade_date) the frozen pipeline of
core.preprocess.ops runs on the cross-section sorted by ts_code (fixed order =>
bitwise-reproducible numpy summation), producing every configured process_id.
Output is keyed by (trade_date, ts_code, factor_id, universe_id, process_id);
days that fail a sample-size/variance requirement emit no rows and are listed
in the diagnostics with their reason, never forward-filled.
"""
from core.config.settings import load_settings
from core.data.store import qs
from core.factor.registry import factor_map
from core.market.prices import date_window
from core.preprocess.ops import process_cross_section
from core.universe.custom import custom_universe_identity

import numpy as np
import pyarrow as pa

_FLUSH_ROWS = 1_000_000


def _universe_members_sql(store, universe_id, research, custom=False):
    if custom:
        custom_universe_identity(store, universe_id)
        relation = store.output_relation("l2", "custom_research_universe")
        return (f"SELECT trade_date,ts_code FROM {relation} WHERE universe_id={qs(universe_id)} "
                "AND is_in_universe IS TRUE")
    universe = store.output_relation("l2", "daily_research_universe")
    if universe_id in research["benchmark"]["research_universes"]:
        return (f"SELECT trade_date, ts_code FROM {universe} "
                f"WHERE universe_id={qs(universe_id)} AND is_in_universe IS TRUE")
    for item in research["benchmark"]["index_universes"]:
        if item["benchmark_id"] == universe_id:
            members = store.output_relation("l3", "daily_index_member")
            return (f"SELECT trade_date, con_code AS ts_code FROM {members} "
                    f"WHERE index_code={qs(item['index_code'])}")
    raise ValueError(f"Unknown preprocessing universe: {universe_id}")


def build_factor_processed(store, start=None, end=None, config_root=None, factor_ids=None,
                           merge_existing=False, market_cap_relation=None, custom_universe_id=None) -> dict:
    research = load_settings("factor_research.yaml", config_root)
    pre = research["preprocessing"]
    if custom_universe_id is not None and merge_existing:
        raise ValueError("Custom-universe preprocessing requires an isolated output root")
    universes = [custom_universe_id] if custom_universe_id is not None else pre["universes"]
    engine = research["engine"]
    store.db.execute(f"SET memory_limit={qs(engine['memory_limit'])}")
    store.db.execute(f"SET threads={int(engine['threads'])}")
    registry = factor_map(config_root)
    selected = list(factor_ids) if factor_ids is not None else list(registry)
    unknown = sorted(set(selected) - set(registry))
    if unknown:
        raise ValueError(f"Unregistered factor_ids: {unknown}")
    existing = None
    if merge_existing:
        existing = store.output_relation("l3", "factor_processed")
    if pre["winsorize_method"] != "median_mad" or pre["standardize"] != "zscore":
        raise ValueError("preprocessing must remain median_mad + zscore")
    if pre["winsorize_zero_mad"] != "skip" or pre["missing_policy"] != "no_fill":
        raise ValueError("preprocessing winsorize_zero_mad/missing_policy changed unexpectedly")
    if pre["industry_policy"] not in ("strict", "backfilled"):
        raise ValueError("preprocessing.industry_policy must be strict or backfilled")
    statuses = research["industry"][f"{pre['industry_policy']}_usable_statuses"]
    status_sql = ", ".join(qs(s) for s in statuses)
    neutral_ids = [p["process_id"] for p in pre["processes"] if p["neutralize"]]
    if len(neutral_ids) > 1:
        raise ValueError("Only one neutralizing process version is supported per build")
    neutral_id = neutral_ids[0] if neutral_ids else None

    store.db.execute("""CREATE OR REPLACE TEMP TABLE processed_parts(
        trade_date DATE, ts_code VARCHAR, factor_id VARCHAR,
        universe_id VARCHAR, process_id VARCHAR, value DOUBLE)""")
    factor_values = store.output_relation("l3", "factor_value")
    market_cap = market_cap_relation or factor_values
    industry = store.output_relation("l3", "daily_industry")
    window = date_window(store, start, end, "f.trade_date")
    quantile_rows = []
    diag_rows = []
    flush_buffer = {"rows": [], "count": 0}

    def flush():
        if not flush_buffer["rows"]:
            return
        columns = list(zip(*flush_buffer["rows"]))
        table = pa.table({name: pa.array(column) for name, column in zip(
            ("trade_date", "ts_code", "factor_id", "universe_id", "process_id", "value"), columns)})
        store.db.register("processed_batch", table)
        store.db.execute("INSERT INTO processed_parts SELECT * FROM processed_batch")
        store.db.unregister("processed_batch")
        flush_buffer["rows"].clear()
        flush_buffer["count"] = 0

    for universe_id in universes:
        members = _universe_members_sql(store, universe_id, research,
                                        custom=custom_universe_id is not None)
        for factor_id in selected:
            query = f"""
                SELECT f.trade_date, f.ts_code, f.value,
                       CASE WHEN ind.industry_pit_status IN ({status_sql}) THEN ind.l1_code END AS industry,
                       mv.value AS log_circ_mv
                FROM (SELECT * FROM {factor_values} WHERE factor_id={qs(factor_id)}) f
                JOIN ({members}) u ON u.trade_date=f.trade_date AND u.ts_code=f.ts_code
                LEFT JOIN {industry} ind ON ind.trade_date=f.trade_date AND ind.ts_code=f.ts_code
                LEFT JOIN (SELECT trade_date, ts_code, value FROM {market_cap}
                           WHERE factor_id='log_circ_mv') mv
                  ON mv.trade_date=f.trade_date AND mv.ts_code=f.ts_code
                WHERE {window}
                ORDER BY f.trade_date, f.ts_code
            """
            table = store.db.execute(query).to_arrow_table()
            if table.num_rows == 0:
                continue
            dates = table.column("trade_date").to_numpy()
            date_list = table.column("trade_date").to_pylist()
            codes = table.column("ts_code").to_pylist()
            values = table.column("value").to_numpy(zero_copy_only=False).astype(np.float64)
            industries = table.column("industry").to_pylist()
            mv = table.column("log_circ_mv").to_numpy(zero_copy_only=False).astype(np.float64)
            boundaries = np.flatnonzero(np.r_[True, dates[1:] != dates[:-1]])
            starts = np.r_[boundaries, len(dates)]
            for position, (lo, hi) in enumerate(zip(starts[:-1], starts[1:])):
                trade_date = date_list[lo]
                result = process_cross_section(
                    values[lo:hi], industries[lo:hi], mv[lo:hi],
                    winsorize_k=pre["winsorize_k"], unknown_label=pre["unknown_industry_label"],
                    min_cross_section=pre["min_cross_section"],
                    min_neutralization_df=pre["min_neutralization_df"],
                    neutral_process_id=neutral_id,
                    min_residual_std=pre["min_residual_std"])
                diag = result.diagnostics
                diag_rows.append((trade_date, factor_id, universe_id,
                                  diag.get("n_input"), diag.get("n_valid"),
                                  diag.get("unknown_industry_count"), diag.get("unknown_industry_share"),
                                  diag.get("winsorize_clipped"), diag.get("winsorize_zero_mad"),
                                  diag.get("neutralize_groups"), diag.get("neutralize_df"),
                                  diag.get("neutralize_beta"),
                                  diag.get("neutralize_max_abs_group_sum_residual"),
                                  diag.get("neutralize_abs_sum_residual_x"),
                                  diag.get("skip_reason"), diag.get("neutralize_skip_reason")))
                if position % pre["quantile_sample_every_n_days"] == 0 and result.quantiles:
                    quantile_rows.append((trade_date, factor_id, universe_id,
                                          *result.quantiles["before"], *result.quantiles["after"]))
                for process_id, (indices, processed) in result.values.items():
                    for index, value in zip(indices, processed):
                        flush_buffer["rows"].append((trade_date, codes[lo + int(index)], factor_id,
                                                     universe_id, process_id, float(value)))
                    flush_buffer["count"] += len(indices)
                if flush_buffer["count"] >= _FLUSH_ROWS:
                    flush()
            del table, dates, codes, values, industries, mv
    flush()
    query = "SELECT * FROM processed_parts"
    if existing:
        excluded = ", ".join(qs(factor_id) for factor_id in selected)
        query = f"(SELECT * FROM {existing} WHERE factor_id NOT IN ({excluded})) UNION ALL {query}"
    result = store.publish("l3", "factor_processed", query,
                           ["trade_date", "ts_code", "factor_id", "universe_id", "process_id"],
                           partition_by="factor_id")
    _write_diagnostics(store, diag_rows, quantile_rows, pre, selected, merge_existing)
    return result


def _write_diagnostics(store, diag_rows, quantile_rows, pre, selected, merge_existing=False):
    import pandas as pd
    diag = pd.DataFrame(diag_rows, columns=[
        "trade_date", "factor_id", "universe_id", "n_input", "n_valid",
        "unknown_industry_count", "unknown_industry_share", "clipped", "zero_mad",
        "neutralize_groups", "neutralize_df", "neutralize_beta",
        "max_abs_group_sum_residual", "abs_sum_residual_x", "skip_reason", "neutralize_skip_reason"])
    store.db.register("processed_diag_new", diag)
    excluded = ", ".join(qs(factor_id) for factor_id in selected)
    old_diag = store.paths.output("l3", "factor_processed", "processing_diagnostics.parquet")
    if merge_existing and old_diag.is_file():
        store.db.execute(f"""CREATE OR REPLACE TEMP TABLE processed_diag AS
            SELECT * FROM read_parquet({qs(old_diag.as_posix())}) WHERE factor_id NOT IN ({excluded})
            UNION ALL SELECT * FROM processed_diag_new""")
    else:
        store.db.execute("CREATE OR REPLACE TEMP TABLE processed_diag AS SELECT * FROM processed_diag_new")
    store.db.execute(f"""COPY (SELECT * FROM processed_diag ORDER BY factor_id, universe_id, trade_date)
        TO {qs(store.paths.output('l3', 'factor_processed', 'processing_diagnostics.parquet').as_posix())}
        (FORMAT PARQUET, COMPRESSION ZSTD)""")
    quantiles = pd.DataFrame(quantile_rows, columns=[
        "trade_date", "factor_id", "universe_id", "q01_before", "q50_before", "q99_before",
        "q01_after", "q50_after", "q99_after"])
    old_quantiles = store.paths.output("l3", "factor_processed", "winsorize_quantiles.parquet")
    if not quantiles.empty or (merge_existing and old_quantiles.is_file()):
        store.db.register("processed_quantiles_new", quantiles)
        if merge_existing and old_quantiles.is_file():
            store.db.execute(f"""CREATE OR REPLACE TEMP TABLE processed_quantiles AS
                SELECT * FROM read_parquet({qs(old_quantiles.as_posix())}) WHERE factor_id NOT IN ({excluded})
                UNION ALL SELECT * FROM processed_quantiles_new""")
        else:
            store.db.execute("CREATE OR REPLACE TEMP TABLE processed_quantiles AS SELECT * FROM processed_quantiles_new")
        store.db.execute(f"""COPY (SELECT * FROM processed_quantiles ORDER BY factor_id, trade_date)
            TO {qs(store.paths.output('l3', 'factor_processed', 'winsorize_quantiles.parquet').as_posix())}
            (FORMAT PARQUET, COMPRESSION ZSTD)""")
        store.db.unregister("processed_quantiles_new")
    summary = store.rows("""SELECT factor_id, universe_id, count(*) AS days,
        sum(n_valid) AS valid_values, sum(clipped) AS clipped_values,
        sum(CASE WHEN zero_mad THEN 1 ELSE 0 END) AS zero_mad_days,
        avg(unknown_industry_share) AS mean_unknown_industry_share,
        max(unknown_industry_share) AS max_unknown_industry_share,
        max(max_abs_group_sum_residual) AS max_abs_group_sum_residual,
        max(abs_sum_residual_x) AS max_abs_sum_residual_x,
        sum(CASE WHEN skip_reason IS NOT NULL THEN 1 ELSE 0 END) AS skipped_days,
        sum(CASE WHEN neutralize_skip_reason IS NOT NULL THEN 1 ELSE 0 END) AS neutralize_skipped_days
        FROM processed_diag GROUP BY 1,2 ORDER BY 1,2""")
    reasons = store.rows("""SELECT factor_id, universe_id, skip_reason, neutralize_skip_reason, count(*) AS days
        FROM processed_diag WHERE skip_reason IS NOT NULL OR neutralize_skip_reason IS NOT NULL
        GROUP BY 1,2,3,4 ORDER BY 1,2,3,4""")
    store.db.unregister("processed_diag_new")
    store.write_json(("l3", "factor_processed", "processing_report.json"),
                     {"summary": summary, "skip_reasons": reasons,
                      "quantile_sample_every_n_days": pre["quantile_sample_every_n_days"]})
    lines = ["# 因子预处理报告", "",
             "| 因子 | 样本 | 天数 | 有效值 | 截断值 | MAD=0天数 | 未知行业占比(均值/最大) "
             "| 中性化最大行业残差和 | 中性化|Σresid·x| | 跳过天数 | 中性化跳过天数 |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for row in summary:
        lines.append(
            f"| {row['factor_id']} | {row['universe_id']} | {row['days']} | {row['valid_values']} "
            f"| {row['clipped_values']} | {row['zero_mad_days']} "
            f"| {_num(row['mean_unknown_industry_share'])}/{_num(row['max_unknown_industry_share'])} "
            f"| {_sci(row['max_abs_group_sum_residual'])} | {_sci(row['max_abs_sum_residual_x'])} "
            f"| {row['skipped_days']} | {row['neutralize_skipped_days']} |")
    lines += ["", "## 跳过原因分布", "", "| 因子 | 样本 | 主版本原因 | 中性化原因 | 天数 |", "|---|---|---|---|---|"]
    for row in reasons:
        lines.append(f"| {row['factor_id']} | {row['universe_id']} | {row['skip_reason'] or ''} "
                     f"| {row['neutralize_skip_reason'] or ''} | {row['days']} |")
    lines += ["", "去极值前后分位数对比见 `winsorize_quantiles.parquet`（按配置的间隔抽样），"
              "逐日处理诊断见 `processing_diagnostics.parquet`。", ""]
    store.paths.output("l3", "factor_processed", "processing_report.md").write_text(
        "\n".join(lines), encoding="utf-8")


def _sci(value):
    return "" if value is None else f"{value:.3e}"


def _num(value, digits=4):
    return "" if value is None else f"{value:.{digits}f}"
