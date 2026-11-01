"""Factor coverage and vendor-estimate comparison reports (plan Step 5).

Coverage: per factor per trading day, published value count divided by the
number of quoted stocks that day (daily_adjusted_price rows). Days whose
coverage falls below half of the previous available day are listed explicitly
so a sudden collapse can be located instead of hidden in an average.

Vendor comparison: self-computed PIT ep_ttm/bp against the vendor pe_ttm/pb
reciprocals. The vendor fields are comparison-only, never factor inputs. The
report includes the share of large-deviation rows falling inside an
announcement window (+/-5 trading days around any available financial
disclosure of the same stock), which is the expected concentration pattern
when the two sides disagree because of PIT visibility rather than an error.
"""
from core.config.settings import load_settings
from core.data.store import qs

DROP_RATIO = 0.5
LARGE_DEVIATION = 0.05
ANNOUNCEMENT_WINDOW_DAYS = 5


def build_factor_coverage_report(store, config_root=None) -> dict:
    registry = load_settings("factor_registry.yaml", config_root)
    factor_ids = [f["factor_id"] for f in registry["factors"]]
    relation = store.output_relation("l3", "factor_value")
    prices = store.output_relation("l1", "daily_adjusted_price")
    registered = ", ".join(f"({qs(factor_id)})" for factor_id in factor_ids)
    store.db.execute(f"""CREATE OR REPLACE TEMP VIEW factor_coverage_daily AS
        WITH counts AS (
            SELECT factor_id, trade_date, count(*) AS value_count FROM {relation} GROUP BY 1,2
        ), quoted AS (
            SELECT trade_date, count(*) AS quoted_count FROM {prices} GROUP BY 1
        ), factors AS (
            SELECT factor_id FROM (VALUES {registered}) AS v(factor_id)
        )
        SELECT f.factor_id, q.trade_date, coalesce(c.value_count,0) AS value_count,
               q.quoted_count,
               coalesce(c.value_count,0)/q.quoted_count AS coverage
        FROM quoted q CROSS JOIN factors f
        LEFT JOIN counts c ON c.factor_id=f.factor_id AND c.trade_date=q.trade_date
    """)
    store.db.execute("""CREATE OR REPLACE TEMP VIEW factor_coverage_drops AS
        SELECT factor_id, trade_date, value_count, quoted_count, coverage,
               lag(coverage) OVER (PARTITION BY factor_id ORDER BY trade_date) AS prev_coverage
        FROM factor_coverage_daily
    """)
    summary = store.rows("""SELECT factor_id, count(*) AS days, sum(value_count) AS values,
        avg(coverage) AS mean_coverage, min(coverage) AS min_coverage
        FROM factor_coverage_daily GROUP BY 1 ORDER BY 1""")
    drops = store.rows(f"""SELECT factor_id, trade_date, prev_coverage, coverage, value_count, quoted_count
        FROM factor_coverage_drops
        WHERE prev_coverage>0 AND coverage<{DROP_RATIO}*prev_coverage
        ORDER BY factor_id, trade_date""")
    coverage_path = store.paths.output("l3", "factor_value", "coverage_daily.parquet")
    coverage_path.parent.mkdir(parents=True, exist_ok=True)
    store.db.execute(f"COPY (SELECT * FROM factor_coverage_daily ORDER BY factor_id, trade_date) "
                     f"TO {qs(coverage_path.as_posix())} (FORMAT PARQUET, COMPRESSION ZSTD)")
    report = {"factors": factor_ids, "drop_ratio": DROP_RATIO, "summary": summary,
              "coverage_drops": drops}
    store.write_json(("l3", "factor_value", "coverage_report.json"), report)
    lines = ["# 因子覆盖率报告", "",
             "| 因子 | 覆盖天数 | 总值数 | 平均覆盖率 | 最低覆盖率 | 骤降天数 |",
             "|---|---|---|---|---|---|"]
    drop_counts = {}
    for drop in drops:
        drop_counts[drop["factor_id"]] = drop_counts.get(drop["factor_id"], 0) + 1
    for row in summary:
        lines.append(f"| {row['factor_id']} | {row['days']} | {row['values']} "
                     f"| {row['mean_coverage']:.4f} | {row['min_coverage']:.4f} "
                     f"| {drop_counts.get(row['factor_id'], 0)} |")
    lines += ["", "## 覆盖率骤降日期（覆盖率低于前一交易日的一半）", "",
              "| 因子 | 日期 | 前一日覆盖率 | 当日覆盖率 | 当日值数/行情数 |", "|---|---|---|---|---|"]
    for drop in drops[:200]:
        lines.append(f"| {drop['factor_id']} | {drop['trade_date']} | {drop['prev_coverage']:.4f} "
                     f"| {drop['coverage']:.4f} | {drop['value_count']}/{drop['quoted_count']} |")
    if len(drops) > 200:
        lines.append(f"| ... | 其余 {len(drops)-200} 条见 coverage_report.json | | | |")
    lines.append("")
    store.paths.output("l3", "factor_value", "coverage_report.md").write_text(
        "\n".join(lines), encoding="utf-8")
    return report


def build_vendor_comparison(store, config_root=None) -> dict:
    relation = store.output_relation("l3", "factor_value")
    prices = store.output_relation("l1", "daily_adjusted_price")
    financial = store.output_relation("l1", "financial_available")
    calendar = store.output_relation("l1", "daily_calendar")
    report = {"large_deviation_threshold": LARGE_DEVIATION,
              "announcement_window_trading_days": ANNOUNCEMENT_WINDOW_DAYS, "comparisons": {}}
    for factor_id, vendor_field in (("ep_ttm", "pe_ttm"), ("bp", "pb")):
        store.db.execute(f"""CREATE OR REPLACE TEMP VIEW vendor_pairs AS
            SELECT f.trade_date, f.ts_code, f.value AS self_value,
                   1/try_cast(p.{vendor_field} AS DOUBLE) AS vendor_value
            FROM (SELECT * FROM {relation} WHERE factor_id='{factor_id}') f
            JOIN {prices} p ON p.trade_date=f.trade_date AND p.ts_code=f.ts_code
            WHERE try_cast(p.{vendor_field} AS DOUBLE) IS NOT NULL
              AND try_cast(p.{vendor_field} AS DOUBLE)<>0
              AND f.value IS NOT NULL
        """)
        store.db.execute("""CREATE OR REPLACE TEMP VIEW vendor_deviations AS
            SELECT *, (self_value-vendor_value)/abs(vendor_value) AS rel_dev
            FROM vendor_pairs
        """)
        stats = store.rows(f"""SELECT count(*) AS n,
            median(abs(rel_dev)) AS median_abs_rel_dev,
            quantile_cont(abs(rel_dev),0.9) AS p90_abs_rel_dev,
            count(*) FILTER (WHERE abs(rel_dev)>{LARGE_DEVIATION}) AS large_deviation_count
            FROM vendor_deviations""")[0]
        large = store.rows(f"""SELECT count(*) AS n, count(*) FILTER (WHERE announcement) AS in_window FROM (
            SELECT d.*, EXISTS (
                SELECT 1 FROM {financial} fa JOIN {calendar} ca ON ca.trade_date=fa.available_date
                JOIN {calendar} cb ON cb.trade_date=d.trade_date
                WHERE fa.ts_code=d.ts_code AND fa.availability_status='available'
                  AND abs(cb.trade_index-ca.trade_index)<={ANNOUNCEMENT_WINDOW_DAYS}
            ) AS announcement
            FROM vendor_deviations d WHERE abs(d.rel_dev)>{LARGE_DEVIATION}
        )""")
        large_row = large[0]
        stats["large_deviation_announcement_window_share"] = (
            large_row["in_window"] / large_row["n"] if large_row["n"] else None)
        report["comparisons"][factor_id] = stats
    store.write_json(("l3", "factor_value", "vendor_comparison.json"), report)
    lines = ["# 自算 PIT 因子与厂商估值字段对照", "",
             "厂商 `pe_ttm`/`pb` 仅作对照，不作为因子输入。偏差 = (自算值 - 厂商倒数) / |厂商倒数|。",
             "公告窗口定义为同一股票任一有效财务披露 `available_date` 前后 "
             f"{ANNOUNCEMENT_WINDOW_DAYS} 个交易日内。", "",
             "| 对照 | 样本数 | 绝对相对偏差中位数 | P90 | 大偏差样本数(>5%) | 大偏差落在公告窗口比例 |",
             "|---|---|---|---|---|---|"]
    for factor_id, stats in report["comparisons"].items():
        share = stats["large_deviation_announcement_window_share"]
        share_text = "无大偏差样本" if share is None else f"{share:.4f}"
        lines.append(f"| {factor_id} vs 1/{'pe_ttm' if factor_id == 'ep_ttm' else 'pb'} "
                     f"| {stats['n']} | {stats['median_abs_rel_dev']:.6f} "
                     f"| {stats['p90_abs_rel_dev']:.6f} | {stats['large_deviation_count']} | {share_text} |")
    lines.append("")
    store.paths.output("l3", "factor_value", "vendor_comparison.md").write_text(
        "\n".join(lines), encoding="utf-8")
    return report
