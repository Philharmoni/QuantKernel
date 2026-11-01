"""Step 9 deterministic per-factor reports generated only from research artifacts."""
import csv
import hashlib
import json
from pathlib import Path

from core.config.settings import load_settings
from core.data.store import code_identity, json_text, qs
from core.evaluation.trial import read_trial_log
from core.factor.registry import factor_map


_SUMMARY_METRICS = [
    "rank_ic_mean", "annualized_rank_icir", "rank_ic_t_stat",
    "long_short_annualized_return", "long_short_sharpe", "long_short_max_drawdown",
    "long_short_mean_one_way_turnover", "group1_annualized_return", "group1_sharpe",
    "monotonicity", "factor_rank_autocorrelation",
]
_SOURCE_TABLES = [
    ("l3", "factor_value"), ("l3", "factor_processed"), ("l4", "factor_ic_daily"),
    ("l4", "factor_quantile_return"), ("l4", "factor_summary"),
    ("l4", "factor_correlation"), ("l4", "factor_trial_log"),
]


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _sources(store):
    result = {}
    for layer, name in _SOURCE_TABLES:
        manifest_path = store.paths.output(layer, name, "manifest.json")
        if not manifest_path.is_file():
            raise FileNotFoundError(f"Report source manifest is missing: {layer}/{name}")
        manifest = _read_json(manifest_path)
        result[f"{layer}/{name}"] = {"sha256": manifest["sha256"], "rows": manifest["rows"]}
    return result


def _horizon(row):
    return int(row["label_id"].rsplit("_", 1)[1])


def _fmt(value, digits=6):
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _table(lines, headers, rows):
    lines.append("| " + " | ".join(headers) + " |")
    lines.append("|" + "|".join("---" for _ in headers) + "|")
    for row in rows:
        lines.append("| " + " | ".join(str(value) for value in row) + " |")
    lines.append("")


def _primary(rows, process_id, horizon, period_id="in_sample"):
    matches = [row for row in rows if row["process_id"] == process_id
               and _horizon(row) == horizon and row["period_id"] == period_id]
    if not matches and period_id == "in_sample":
        return _primary(rows, process_id, horizon, "full")
    return matches[0] if matches else None


def _summary_flat(factor_id, spec, trial, rows, coverage, has_oos):
    result = {"factor_id": factor_id, "version": spec["version"], "category": spec["category"],
              "sample_status": "in_sample_and_out_of_sample" if has_oos else "in_sample_only",
              "trial_id": trial["trial_id"], "oos_accessed": bool(trial.get("oos_accessed")),
              "mean_coverage": coverage.get("mean_coverage"),
              "min_coverage": coverage.get("min_coverage")}
    for horizon in (5, 20):
        row = _primary(rows, "ws_z", horizon)
        for metric in _SUMMARY_METRICS:
            result[f"h{horizon}_{metric}"] = None if row is None else row.get(metric)
    return result


def _factor_markdown(payload):
    factor = payload["factor"]
    coverage = payload["coverage"]
    processing = payload["preprocessing"]
    trial = payload["trial"]
    lines = [f"# 因子报告：`{factor['factor_id']}`", "",
             f"版本：v{factor['version']}；类别：`{factor['category']}`；方向：{factor['expected_direction']}。",
             f"公式：`{factor['formula']}`", "",
             f"输入：`{json.dumps(factor['inputs'], ensure_ascii=False, sort_keys=True)}`", "",
             f"PIT 约束：{factor['pit']}", "",
             f"评价状态：**{payload['sample_status']}**；主口径：`ws_z / theoretical / full / in_sample`。",
             "所有评价数字来自下方所列 L4 `factor_summary` 等表；未计入交易费用，多空收益不是可交易承诺。", "",
             "## 覆盖率与预处理", "",
             f"来源：`factor_value/coverage_report.json`、`factor_processed/processing_report.json`。", ""]
    _table(lines, ["因子值数量", "覆盖天数", "平均覆盖", "最低覆盖"],
           [[coverage.get("values"), coverage.get("days"), _fmt(coverage.get("mean_coverage")),
             _fmt(coverage.get("min_coverage"))]])
    _table(lines, ["预处理有效值", "截面不足跳过日", "中性化跳过日", "未知行业占比上限"],
           [[processing.get("valid_values"), processing.get("skipped_days"),
             processing.get("neutralize_skipped_days"), _fmt(processing.get("max_unknown_industry_share"))]])
    lines += ["## IC / RankIC / IC 衰减", "",
              "来源：`factor_summary`；理论口径、全样本、样本内，`ws_z`。", ""]
    _table(lines, ["h", "天数", "IC 均值", "IC 标准差", "RankIC 均值", "RankICIR(年化)", "NW t", "秩自相关"],
           [[_horizon(row), row["rank_ic_n_days"], _fmt(row["ic_mean"]), _fmt(row["ic_std"]),
             _fmt(row["rank_ic_mean"]), _fmt(row["annualized_rank_icir"]),
             _fmt(row["rank_ic_t_stat"]), _fmt(row["factor_rank_autocorrelation"])]
            for row in payload["ic_decay"]])
    lines += ["## 预处理版本对比", "",
              "来源：`factor_summary`；理论口径、全样本、样本内。", ""]
    _table(lines, ["h", "处理版本", "RankIC", "RankICIR(年化)", "NW t", "多空夏普", "单调性"],
           [[_horizon(row), row["process_id"], _fmt(row["rank_ic_mean"]),
             _fmt(row["annualized_rank_icir"]), _fmt(row["rank_ic_t_stat"]),
             _fmt(row["long_short_sharpe"]), _fmt(row["monotonicity"])]
            for row in payload["process_comparison"]])
    lines += ["## 分层、换手与绩效", "",
              "各非重叠偏移路径分别计算后取均值；成本为平均毛收益/平均单边换手。", ""]
    _table(lines, ["h", "多头年化", "多头夏普", "多头 MDD", "多头换手", "多头成本上限",
                   "多空年化", "多空夏普", "多空 MDD", "多空 Calmar", "多空换手", "多空成本上限"],
           [[_horizon(row), _fmt(row["group1_annualized_return"]), _fmt(row["group1_sharpe"]),
             _fmt(row["group1_max_drawdown"]), _fmt(row["group1_mean_one_way_turnover"]),
             _fmt(row["group1_breakeven_cost"]), _fmt(row["long_short_annualized_return"]),
             _fmt(row["long_short_sharpe"]), _fmt(row["long_short_max_drawdown"]),
             _fmt(row["long_short_calmar"]), _fmt(row["long_short_mean_one_way_turnover"]),
             _fmt(row["long_short_breakeven_cost"])] for row in payload["quantile_performance"]])
    lines += ["## 年度评价", ""]
    _table(lines, ["年度", "h", "RankIC", "NW t", "多头年化", "多空年化", "多空夏普"],
           [[row["period_id"].split(":", 1)[1], _horizon(row), _fmt(row["rank_ic_mean"]),
             _fmt(row["rank_ic_t_stat"]), _fmt(row["group1_annualized_return"]),
             _fmt(row["long_short_annualized_return"]), _fmt(row["long_short_sharpe"])]
            for row in payload["annual"]])
    lines += ["## 市值与指数成分分段", "",
              "分段仅使用主评价前瞻期 h=5/20；指数为成分覆盖切片，不代表被动跟踪。", ""]
    _table(lines, ["h", "分段", "RankIC", "NW t", "多头年化", "多空年化", "多空夏普"],
           [[_horizon(row), row["segment_id"], _fmt(row["rank_ic_mean"]), _fmt(row["rank_ic_t_stat"]),
             _fmt(row["group1_annualized_return"]), _fmt(row["long_short_annualized_return"]),
             _fmt(row["long_short_sharpe"])] for row in payload["segments"]])
    lines += ["## 因子相关性与冗余提示", "",
              "来源：`factor_correlation`；按平均日截面相关的绝对值降序列示。", ""]
    _table(lines, ["相关因子", "平均相关", "平均绝对相关", "天数", "冗余提示"],
           [[row["peer"], _fmt(row["mean_correlation"]), _fmt(row["mean_abs_correlation"]),
             row["n_days"], _fmt(row["redundant"])] for row in payload["correlations"]])
    if payload["out_of_sample"]:
        lines += ["## 样本外评价", ""]
        _table(lines, ["h", "处理版本", "RankIC", "NW t", "多空年化", "多空夏普"],
               [[_horizon(row), row["process_id"], _fmt(row["rank_ic_mean"]), _fmt(row["rank_ic_t_stat"]),
                 _fmt(row["long_short_annualized_return"]), _fmt(row["long_short_sharpe"])]
                for row in payload["out_of_sample"]])
    else:
        lines += ["## 样本外评价", "", "**尚未正式访问锁定样本外，因此没有样本外绩效结论。**", ""]
    lines += ["## 研究记录与来源指纹", "",
              f"trial_id：`{trial['trial_id']}`；评价区间：{trial['evaluation_start']}–{trial['evaluation_end']}；"
              f"OOS 访问：{_fmt(bool(trial.get('oos_accessed')))}。", "",
              "增量合并后当前 L4 文件哈希可与该因子历史 trial 的独立运行哈希不同；两者均保留，"
              "当前报告数字始终取下列当前 L4 文件。", ""]
    _table(lines, ["来源", "行数", "SHA-256"],
           [[name, meta["rows"], meta["sha256"]] for name, meta in payload["sources"].items()])
    lines += ["## 数据与解释边界", "",
              "- 行业 PIT 仅 `limited`：真实数据缺申万历史成分，行业中性化只能反映当前快照回填。",
              "- 沪深300/中证500/中证1000 为自建成分收益基准；官方日线/权重核验继续挂起。",
              "- 分组换手与盈亏平衡成本是无费用统计诊断，未模拟实际订单、冲击成本或涨跌停成交队列。",
              "- 相关聚类是冗余提示，不构成自动删因子或资本配置结论。", ""]
    return "\n".join(lines)


def _summary_markdown(rows, sources):
    lines = ["# 全因子研究汇总", "",
             "主口径：`ws_z / theoretical / full / in_sample`；完整定义和分段见各因子报告。", "",
             f"来源 `factor_summary` SHA-256：`{sources['l4/factor_summary']['sha256']}`。", ""]
    _table(lines, ["因子", "版本", "状态", "h5 RankIC", "h5 ICIR", "h5 NW-t", "h5 多空夏普",
                   "h20 RankIC", "h20 ICIR", "h20 NW-t", "h20 多空夏普", "平均覆盖"],
           [[row["factor_id"], row["version"], row["sample_status"],
             _fmt(row["h5_rank_ic_mean"]), _fmt(row["h5_annualized_rank_icir"]),
             _fmt(row["h5_rank_ic_t_stat"]), _fmt(row["h5_long_short_sharpe"]),
             _fmt(row["h20_rank_ic_mean"]), _fmt(row["h20_annualized_rank_icir"]),
             _fmt(row["h20_rank_ic_t_stat"]), _fmt(row["h20_long_short_sharpe"]),
             _fmt(row["mean_coverage"])] for row in rows])
    lines += ["`in_sample_only` 不代表样本外无效，而代表尚未正式评价；样本外状态以各因子报告为准。", ""]
    return "\n".join(lines)


def build_factor_reports(store, factor_ids=None, config_root=None) -> dict:
    research = load_settings("factor_research.yaml", config_root)
    registry = factor_map(config_root)
    selected = list(factor_ids) if factor_ids is not None else list(registry)
    unknown = sorted(set(selected) - set(registry))
    if unknown:
        raise ValueError(f"Unregistered factor_ids: {unknown}")
    summary_relation = store.output_relation("l4", "factor_summary")
    all_summary = store.rows(f"SELECT * FROM {summary_relation} WHERE universe_id="
                             f"{qs(research['composition']['universe_id'])} ORDER BY factor_id,process_id,label_id,"
                             "sample_mode,segment_id,period_id")
    by_factor = {factor_id: [] for factor_id in selected}
    for row in all_summary:
        if row["factor_id"] in by_factor and row["sample_mode"] == "theoretical":
            by_factor[row["factor_id"]].append(row)
    missing = [factor_id for factor_id in selected if not by_factor[factor_id]]
    if missing:
        raise ValueError(f"Factors lack factor_summary evaluation rows: {missing}")
    coverage_path = store.paths.output("l3", "factor_value", "coverage_report.json")
    processing_path = store.paths.output("l3", "factor_processed", "processing_report.json")
    coverage = {row["factor_id"]: row for row in _read_json(coverage_path)["summary"]}
    processing = {row["factor_id"]: row for row in _read_json(processing_path)["summary"]}
    correlation_relation = store.output_relation("l4", "factor_correlation")
    correlation_rows = store.rows(f"SELECT * FROM {correlation_relation}")
    trials = read_trial_log(store)
    sources = _sources(store)
    output = store.paths.output("l4", "factor_report")
    output.mkdir(parents=True, exist_ok=True)
    summary_rows = []
    for factor_id in selected:
        rows = by_factor[factor_id]
        spec = registry[factor_id]
        history = [record for record in trials if record["factor_id"] == factor_id
                   and record["factor_version"] == spec["version"]]
        if not history:
            raise ValueError(f"No trial record for {factor_id} version {spec['version']}")
        if factor_id not in coverage or factor_id not in processing:
            raise ValueError(f"Coverage/preprocessing report incomplete for {factor_id}")
        trial = history[-1]
        full_rows = [row for row in rows if row["segment_id"] == "full" and row["period_id"] == "in_sample"]
        if not full_rows:
            full_rows = [row for row in rows if row["segment_id"] == "full" and row["period_id"] == "full"]
        primary_process_rows = [row for row in full_rows if row["process_id"] == "ws_z"]
        correlations = []
        for row in correlation_rows:
            if row["factor_a"] == factor_id or row["factor_b"] == factor_id:
                correlations.append({**row, "peer": row["factor_b"] if row["factor_a"] == factor_id else row["factor_a"]})
        correlations.sort(key=lambda row: (-abs(row["mean_correlation"]), row["peer"]))
        payload = {
            "factor": {"factor_id": factor_id, **spec},
            "sample_status": "in_sample_and_out_of_sample" if any(
                row["period_id"] == "out_of_sample" for row in rows) else "in_sample_only",
            "query_scope": {"universe_id": research["composition"]["universe_id"],
                            "sample_mode": "theoretical", "main_process_id": "ws_z"},
            "coverage": coverage[factor_id], "preprocessing": processing[factor_id],
            "ic_decay": sorted(primary_process_rows, key=_horizon),
            "process_comparison": sorted(full_rows, key=lambda row: (_horizon(row), row["process_id"])),
            "quantile_performance": sorted(primary_process_rows, key=_horizon),
            "annual": sorted(
                [row for row in rows if row["process_id"] == "ws_z" and row["segment_id"] == "full"
                 and row["period_id"].startswith("year:")],
                key=lambda row: (row["period_id"], _horizon(row))),
            "segments": sorted([row for row in rows if row["process_id"] == "ws_z"
                                and row["segment_id"] != "full" and row["period_id"] == row["segment_id"]],
                               key=lambda row: (_horizon(row), row["segment_id"])),
            "correlations": correlations,
            "out_of_sample": sorted([row for row in rows if row["segment_id"] == "full"
                                     and row["period_id"] == "out_of_sample"],
                                    key=lambda row: (_horizon(row), row["process_id"])),
            "trial": trial, "sources": sources,
        }
        (output / f"{factor_id}.json").write_text(json_text(payload), encoding="utf-8")
        (output / f"{factor_id}.md").write_text(_factor_markdown(payload), encoding="utf-8")
        summary_rows.append(_summary_flat(factor_id, spec, trial, rows, coverage[factor_id],
                                          payload["sample_status"] != "in_sample_only"))
    summary_rows.sort(key=lambda row: row["factor_id"])
    (output / "summary.json").write_text(json_text(
        {"query_scope": {"process_id": "ws_z", "sample_mode": "theoretical",
                         "segment_id": "full", "period_id": "in_sample"},
         "rows": summary_rows, "sources": sources}), encoding="utf-8")
    (output / "summary.md").write_text(_summary_markdown(summary_rows, sources), encoding="utf-8")
    fieldnames = list(summary_rows[0]) if summary_rows else []
    with (output / "summary.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(summary_rows)
    fingerprint = hashlib.sha256(json_text({"research": research, "registry": registry}).encode("utf-8")).hexdigest()
    files = {}
    for path in sorted(output.iterdir()):
        if path.is_file() and path.name != "manifest.json":
            files[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = {"code_hash": code_identity(), "research_registry_sha256": fingerprint,
                "factors": selected, "sources": sources, "files": files}
    store.write_json(("l4", "factor_report", "manifest.json"), manifest)
    return {"factors": len(selected), "files": len(files), "manifest": manifest,
            "summary_rows": summary_rows}
