"""Calibrate self-built benchmark returns against the official index daily.

Stage 2 source directory: no raw-table access here either; the official
series comes from the L1 output core.reference.market already validated.

The comparison is deliberately honest about its limits (plan Step 4): the
official index_daily is a price index without dividend reinvestment, while
self-built returns use adjusted closes; constituents lag by the index_weight
publication lag; and circ_mv weighting is not the official free-float tiered
weighting. The output therefore reports the approximation error (Pearson
correlation, tracking error, mean difference) and always carries
basis=self_built_approximation_not_official_index -- it never presents the
self-built series as the official index.
"""
import math

from core.config.settings import load_settings


def calibration_metrics(pairs, annualization_days=252) -> dict:
    """Closed-form metrics over aligned (official, built) daily-return pairs.

    Variance uses ddof=0 (population), matching the frozen preprocessing
    standardize convention. Pearson is None when there are fewer than 2 pairs
    or either series is constant (zero denominator); tracking error and mean
    difference only need one pair.
    """
    pairs = [(float(official), float(built)) for official, built in pairs]
    n = len(pairs)
    result = {"n": n, "pearson": None, "mean_diff": None,
              "tracking_error_daily": None, "tracking_error_annualized": None}
    if n == 0:
        return result
    mean_official = sum(official for official, _ in pairs) / n
    mean_built = sum(built for _, built in pairs) / n
    diffs = [built - official for official, built in pairs]
    mean_diff = sum(diffs) / n
    diff_variance = sum((d - mean_diff) ** 2 for d in diffs) / n
    tracking_error = math.sqrt(diff_variance)
    result.update(mean_diff=mean_diff, tracking_error_daily=tracking_error,
                  tracking_error_annualized=tracking_error * math.sqrt(annualization_days))
    if n >= 2:
        var_official = sum((official - mean_official) ** 2 for official, _ in pairs) / n
        var_built = sum((built - mean_built) ** 2 for _, built in pairs) / n
        covariance = sum((official - mean_official) * (built - mean_built)
                         for official, built in pairs) / n
        denominator = math.sqrt(var_official * var_built)
        if denominator > 0:
            result["pearson"] = covariance / denominator
    return result


def build_benchmark_calibration(store, config_root=None) -> dict:
    research = load_settings("factor_research.yaml", config_root)
    config = research["benchmark"]["calibration"]
    benchmark_id = config["benchmark_id"]
    official_code = config["official_index_code"]
    min_overlap = config["min_overlap_days"]
    if config["variance_ddof"] != 0:
        raise ValueError("benchmark.calibration.variance_ddof must be 0 (population variance)")
    if config["basis"] != "self_built_approximation_not_official_index":
        raise ValueError("benchmark.calibration.basis must remain self_built_approximation_not_official_index")
    annualization = config["annualization_days"]
    benchmark = store.output_relation("l3", "daily_benchmark_return")
    official = store.output_relation("l1", "index_daily_available")

    methods = research["benchmark"]["methods"]
    reports = {}
    for method in methods:
        rows = store.rows(f"""
            WITH off AS (
                SELECT trade_date, close/pre_close-1 AS official_ret FROM {official}
                WHERE ts_code='{official_code}'
            ), built AS (
                SELECT trade_date, ret AS built_ret FROM {benchmark}
                WHERE benchmark_id='{benchmark_id}' AND method='{method}'
            )
            SELECT o.trade_date, o.official_ret, b.built_ret
            FROM off o JOIN built b USING(trade_date)
            WHERE o.official_ret IS NOT NULL AND b.built_ret IS NOT NULL
            ORDER BY o.trade_date
        """)
        metrics = calibration_metrics([(r["official_ret"], r["built_ret"]) for r in rows],
                                      annualization)
        metrics.update({
            "method": method,
            "overlap_start": rows[0]["trade_date"].isoformat() if rows else None,
            "overlap_end": rows[-1]["trade_date"].isoformat() if rows else None,
            "sufficient_overlap": metrics["n"] >= min_overlap,
        })
        reports[method] = metrics
    official_coverage = store.rows(f"""SELECT min(trade_date) AS first_date,
        max(trade_date) AS last_date, count(*) AS days FROM {official}
        WHERE ts_code='{official_code}'""")[0]
    report = {
        "benchmark_id": benchmark_id,
        "official_index_code": official_code,
        "basis": config["basis"],
        "variance_ddof": 0,
        "annualization_days": annualization,
        "min_overlap_days": min_overlap,
        "official_coverage": official_coverage,
        "methods": reports,
        "caveats": [
            "官方index_daily为价格指数（不含分红再投资），自建收益使用复权收盘价，口径天然存在差异。",
            "自建成分来自index_weight月度快照加发布滞后的每日展开，调样生效日与官方可能不同。",
            "circ_mv加权不是官方自由流通股本分级靠档加权；等权口径与官方市值加权方式不同。",
            "官方日线未覆盖的日期不进入重叠区间；000300.SH/000905.SH官方日线下载按用户指令挂起。",
            "本报告全部序列为自建近似口径，不冒充官方指数。",
        ],
    }
    store.write_json(("l3", "daily_benchmark_return", "calibration.json"), report)
    lines = [
        f"# 自建基准收益标定报告：{benchmark_id} vs 官方 {official_code}",
        "",
        "> 口径声明：以下全部为**自建近似基准**，不是官方指数。官方 `index_daily` 为价格指数"
        "（不含分红再投资），自建收益使用复权价，成分含发布滞后，权重口径亦非官方自由流通靠档加权。",
        "",
        f"- 官方数据覆盖：{official_coverage['first_date']} 至 {official_coverage['last_date']}"
        f"（{official_coverage['days']} 个交易日）",
        f"- 方差口径：ddof=0（总体方差）；年化系数：{annualization}；最小重叠天数要求：{min_overlap}",
        "",
        "| 方法 | 重叠天数 | 重叠区间 | Pearson 相关 | 跟踪误差(日) | 跟踪误差(年化) | 平均偏差(日) | 重叠是否充足 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for method, metrics in reports.items():
        overlap = f"{metrics['overlap_start']} ~ {metrics['overlap_end']}" if metrics["n"] else "无"
        lines.append(
            f"| {method} | {metrics['n']} | {overlap} "
            f"| {_fmt(metrics['pearson'])} | {_fmt(metrics['tracking_error_daily'])} "
            f"| {_fmt(metrics['tracking_error_annualized'])} | {_fmt(metrics['mean_diff'])} "
            f"| {'是' if metrics['sufficient_overlap'] else '否'} |")
    lines += ["", "## 偏离来源说明", ""]
    lines += [f"- {caveat}" for caveat in report["caveats"]]
    lines.append("")
    path = store.paths.output("l3", "daily_benchmark_return", "calibration.md")
    path.write_text("\n".join(lines), encoding="utf-8")
    return report


def _fmt(value, digits=6):
    return "NULL" if value is None else f"{value:.{digits}f}"
