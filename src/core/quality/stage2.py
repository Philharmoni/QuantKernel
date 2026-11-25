"""Step 10 unified Stage 2 quality gate.

Current-file integrity and non-incremental lineage are strict. Incrementally
merged tables intentionally carry a mixture of historical upstream hashes;
their current bytes/schema/keys/rows are still verified and raw access remains
forbidden. Acknowledged data limitations are reported separately from failures.
"""
import hashlib
import json
import logging
from pathlib import Path
import re

from core.config.paths import PROJECT_ROOT
from core.config.settings import load_settings
from core.data.store import date_sql, json_text, qi, qs, stored_artifact_sha256
from core.factor.entry import (check_factor_label_isolation, check_no_raw_access,
                               check_stage2_entry)
from core.factor.registry import factor_map
from core.factor.truncate import check_truncation_invariance
from core.evaluation.trial import read_trial_log
from core.quality.checks import file_sha256
from core.quality.verification import verification_metadata, verification_mode


ARTIFACTS = {
    "l1/daily_calendar": ["trade_date"],
    "l1/daily_adjusted_price": ["trade_date", "ts_code"],
    "l1/daily_stock_state": ["trade_date", "ts_code"],
    "l1/daily_trade_status": ["trade_date", "ts_code"],
    "l1/financial_available": ["source_table", "record_id"],
    "l1/quarterly_financial": ["source_table", "ts_code", "metric", "end_date", "available_date"],
    "l1/ttm_financial": ["source_table", "ts_code", "metric", "end_date", "available_date"],
    "l1/index_member_available": ["ts_code", "l1_code", "in_date"],
    "l1/index_weight_available": ["index_code", "con_code", "trade_date"],
    "l1/index_daily_available": ["ts_code", "trade_date"],
    "l2/research_universe_config": ["universe_id"],
    "l2/daily_research_universe": ["trade_date", "ts_code", "universe_id"],
    "l3/daily_industry": ["trade_date", "ts_code"],
    "l3/daily_index_member": ["trade_date", "index_code", "con_code"],
    "l3/daily_benchmark_return": ["trade_date", "benchmark_id", "method"],
    "l3/forward_return": ["trade_date", "ts_code", "label_id"],
    "l3/factor_value": ["trade_date", "ts_code", "factor_id"],
    "l3/factor_processed": ["trade_date", "ts_code", "factor_id", "universe_id", "process_id"],
    "l3/factor_composite_value": ["trade_date", "ts_code", "factor_id"],
    "l4/factor_ic_daily": ["trade_date", "factor_id", "universe_id", "process_id",
                           "label_id", "sample_mode", "segment_id"],
    "l4/factor_quantile_return": ["trade_date", "factor_id", "universe_id", "process_id",
                                  "label_id", "sample_mode", "segment_id", "rebalance_offset", "group_id"],
    "l4/factor_summary": ["factor_id", "universe_id", "process_id", "label_id",
                          "sample_mode", "segment_id", "period_id"],
    "l4/factor_correlation": ["universe_id", "process_id", "factor_a", "factor_b"],
    "l4/factor_ic_realized": ["signal_date", "factor_id", "universe_id", "process_id", "label_id"],
    "l4/factor_composite_weight": ["trade_date", "composite_id", "component_id"],
    "l4/factor_trial_log": ["trial_id"],
}
INCREMENTAL_MIXED = {"l3/factor_value", "l3/factor_processed", "l4/factor_ic_daily",
                     "l4/factor_quantile_return", "l4/factor_summary", "l4/factor_trial_log"}
REPEAT_SPECS = {
    "step7": (("_repeat_step7_c", "_repeat_step7_d"),
              [("l4", name) for name in ("factor_ic_daily", "factor_quantile_return", "factor_summary")]),
    "step8": (("_repeat_step8_a", "_repeat_step8_b"),
              [("l4", "factor_correlation"), ("l4", "factor_ic_realized"),
               ("l4", "factor_composite_weight"), ("l3", "factor_composite_value"),
               ("l3", "factor_value"), ("l3", "factor_processed")]),
}
REPEAT_STEP9_ROOTS = ("_repeat_step9_a", "_repeat_step9_b")


def _failure(check, table, detail):
    return {"check": check, "table": table, "detail": detail}


def check_architecture():
    raw = check_no_raw_access()
    labels = check_factor_label_isolation()
    failures = []
    if raw:
        failures.append(_failure("raw_table_access_forbidden", "stage2_source", raw))
    if labels:
        failures.append(_failure("factor_label_isolation", "core.factor", labels))
    return {"passed": not failures, "failures": failures,
            "scanned_stage2_families": ["industry", "benchmark", "label", "factor", "preprocess", "evaluation"]}


def check_layering(store, artifacts=None, deep_primary_keys=False, verification="standard",
                   verify_files: bool | None = None):
    mode = verification_mode(verification)
    verify_files = mode == "audit" if verify_files is None else verify_files
    deep_primary_keys = deep_primary_keys or mode == "audit"
    artifacts = artifacts or ARTIFACTS
    failures, historical = [], {}
    tables = {}
    for artifact, expected_key in artifacts.items():
        layer, name = artifact.split("/")
        try:
            manifest_path = store.paths.output(layer, name, "manifest.json")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            relation = store.output_relation(layer, name)
            actual_sha = (stored_artifact_sha256(store.paths.output(layer, name))
                          if verify_files else manifest["sha256"])
            if actual_sha != manifest["sha256"]:
                failures.append(_failure("file_sha256", artifact,
                                         {"manifest": manifest["sha256"], "actual": actual_sha}))
            if manifest["primary_key"] != expected_key:
                failures.append(_failure("primary_key", artifact, manifest["primary_key"]))
            schema = store.columns(relation)
            if schema != manifest["schema"]:
                failures.append(_failure("manifest_schema", artifact,
                                         {"manifest": manifest["schema"], "actual": schema}))
            rows = store.db.execute(f"SELECT count(*) FROM {relation}").fetchone()[0]
            if rows != manifest["rows"]:
                failures.append(_failure("manifest_rows", artifact,
                                         {"manifest": manifest["rows"], "actual": rows}))
            # L3 manifests may legitimately declare raw source lineage even
            # though the Stage 2 builder reads only L1/L2. L4 research products
            # must remain entirely downstream and therefore declare none.
            if layer == "l4" and manifest.get("raw_tables"):
                failures.append(_failure("raw_tables_must_be_empty", artifact, manifest["raw_tables"]))
            stale = []
            for upstream, expected_sha in manifest.get("upstream", {}).items():
                upstream_layer, upstream_name = upstream.split("/")
                upstream_path = store.paths.output(upstream_layer, upstream_name, "manifest.json")
                if not upstream_path.is_file():
                    failures.append(_failure("missing_upstream", artifact, upstream))
                    continue
                current = json.loads(upstream_path.read_text(encoding="utf-8"))["sha256"]
                if current != expected_sha:
                    if artifact in INCREMENTAL_MIXED:
                        stale.append(upstream)
                    else:
                        failures.append(_failure("stale_upstream", artifact,
                                                 {"upstream": upstream, "expected": expected_sha, "actual": current}))
            if stale:
                historical[artifact] = sorted(stale)
            if deep_primary_keys:
                fields = ",".join(qi(field) for field in expected_key)
                duplicate = store.db.execute(f"SELECT 1 FROM {relation} GROUP BY {fields} "
                                             "HAVING count(*)>1 LIMIT 1").fetchone()
                null_condition = " OR ".join(f"{qi(field)} IS NULL" for field in expected_key)
                null = store.db.execute(f"SELECT 1 FROM {relation} WHERE {null_condition} LIMIT 1").fetchone()
                if duplicate:
                    failures.append(_failure("duplicate_primary_key", artifact, expected_key))
                if null:
                    failures.append(_failure("null_primary_key", artifact, expected_key))
            tables[artifact] = {"rows": rows, "sha256": actual_sha, "primary_key": expected_key,
                                "file_hash_verified": verify_files,
                                "lineage": "incremental_mixed" if artifact in INCREMENTAL_MIXED else "strict"}
        except Exception as error:
            failures.append(_failure("unreadable_artifact", artifact, str(error)))
    return {"passed": not failures, "failures": failures, "tables": tables,
            "historical_upstream_references": historical,
            "deep_primary_keys": deep_primary_keys, "file_hash_verified": verify_files,
            **verification_metadata(mode)}


def check_pit(store, config_root=None):
    research = load_settings("factor_research.yaml", config_root)
    failures, observations = [], {}

    def scalar(relation, expression):
        return store.db.execute(f"SELECT {expression} FROM {relation}").fetchone()[0]

    financial = store.output_relation("l1", "financial_available")
    observations["financial_effective_after_available"] = scalar(
        financial, "count(*) FILTER(WHERE effective_ann_date>available_date)")
    labels = store.output_relation("l3", "forward_return")
    coverage_end = date_sql(qs(research["sample"]["quote_coverage_end"]))
    label_checks = {
        "invalid_actual_interval": "ret IS NOT NULL AND (entry_date IS NULL OR exit_date IS NULL "
                                   "OR exit_date<entry_date "
                                   "OR (label_id LIKE 'ret_o2o_%' AND entry_date<=trade_date) "
                                   "OR (label_id LIKE 'ret_c2c_%' AND entry_date<>trade_date))",
        "nonfinite_return": "ret IS NOT NULL AND NOT isfinite(ret)",
        "missing_null_reason": "ret IS NULL AND exception_flag IS NULL",
        "return_beyond_coverage": f"ret IS NOT NULL AND (beyond_data_end OR exit_date>{coverage_end})",
    }
    for name, condition in label_checks.items():
        observations[name] = scalar(labels, f"count(*) FILTER(WHERE {condition})")
    industry = store.output_relation("l3", "daily_industry")
    allowed = ", ".join(qs(value) for value in research["industry"]["pit_statuses"])
    observations["invalid_industry_status"] = scalar(
        industry, f"count(*) FILTER(WHERE industry_pit_status NOT IN ({allowed}))")
    observations["industry_backfilled_rows"] = scalar(
        industry, "count(*) FILTER(WHERE industry_pit_status='snapshot_backfilled')")
    weights = store.output_relation("l4", "factor_composite_weight")
    in_sample_end = date_sql(qs(research["sample"]["in_sample_end"]))
    observations["weights_not_realized"] = scalar(
        weights, "count(*) FILTER(WHERE composite_id='composite_icir_60' AND last_exit_date>=trade_date)")
    observations["weights_out_of_sample_training"] = scalar(
        weights, f"count(*) FILTER(WHERE composite_id='composite_icir_60' AND last_exit_date>{in_sample_end})")
    realized = store.output_relation("l4", "factor_ic_realized")
    observations["realized_ic_beyond_training_cutoff"] = scalar(
        realized, f"count(*) FILTER(WHERE max_exit_date>{in_sample_end})")
    for name in ("financial_effective_after_available", "invalid_actual_interval", "nonfinite_return",
                 "missing_null_reason", "return_beyond_coverage", "invalid_industry_status",
                 "weights_not_realized", "weights_out_of_sample_training", "realized_ic_beyond_training_cutoff"):
        if observations[name]:
            failures.append(_failure("pit_watermark", name, observations[name]))
    return {"passed": not failures, "failures": failures, "observations": observations}


def check_oos_status(store):
    registry = factor_map()
    history = read_trial_log(store)
    failures = []
    oos_counts = {}
    for record in history:
        if record.get("oos_accessed"):
            key = (record["factor_id"], record["factor_version"])
            oos_counts[key] = oos_counts.get(key, 0) + 1
    for key, count in oos_counts.items():
        if count > 1:
            failures.append(_failure("repeated_out_of_sample_access", str(key), count))
    for factor_id, spec in registry.items():
        if not any(record["factor_id"] == factor_id and record["factor_version"] == spec["version"]
                   for record in history):
            failures.append(_failure("missing_trial_record", factor_id, spec["version"]))
    summary = store.output_relation("l4", "factor_summary")
    oos_rows = store.rows(f"SELECT factor_id,count(*) AS n FROM {summary} "
                          "WHERE period_id='out_of_sample' GROUP BY factor_id")
    oos_by_factor = {row["factor_id"]: row["n"] for row in oos_rows}
    formally_accessed = {factor_id for factor_id, _ in oos_counts}
    for factor_id in registry:
        if factor_id not in formally_accessed and oos_by_factor.get(factor_id, 0):
            failures.append(_failure("unrecorded_out_of_sample_rows", factor_id, oos_by_factor[factor_id]))
    report_path = store.paths.output("l4", "factor_report", "summary.json")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report_rows = {row["factor_id"]: row for row in report["rows"]}
    if set(report_rows) != set(registry):
        failures.append(_failure("report_factor_set", "factor_report",
                                 {"missing": sorted(set(registry) - set(report_rows)),
                                  "extra": sorted(set(report_rows) - set(registry))}))
    for factor_id, row in report_rows.items():
        expected = "in_sample_and_out_of_sample" if oos_by_factor.get(factor_id, 0) else "in_sample_only"
        if row["sample_status"] != expected:
            failures.append(_failure("report_sample_status", factor_id,
                                     {"expected": expected, "actual": row["sample_status"]}))
    report_manifest = json.loads(store.paths.output("l4", "factor_report", "manifest.json").read_text())
    for source, metadata in report_manifest["sources"].items():
        layer, name = source.split("/")
        current = json.loads(store.paths.output(layer, name, "manifest.json").read_text())
        if current["sha256"] != metadata["sha256"]:
            failures.append(_failure("stale_report_source", source, metadata["sha256"]))
    for filename, expected_sha in report_manifest["files"].items():
        actual = file_sha256(store.paths.output("l4", "factor_report", filename))
        if actual != expected_sha:
            failures.append(_failure("report_file_sha256", filename, actual))
    return {"passed": not failures, "failures": failures,
            "trial_records": len(history), "formal_oos_factors": sorted(formally_accessed),
            "out_of_sample_summary_rows": sum(oos_by_factor.values()),
            "report_file_count": len(report_manifest["files"])}


def check_repeatability(store, verification="standard"):
    mode = verification_mode(verification)
    failures, comparisons = [], {}
    stage1_executed = mode != "fast"
    if stage1_executed:
        stage1_path = store.paths.output("verification", "repeatability.json")
        try:
            stage1 = json.loads(stage1_path.read_text(encoding="utf-8"))
            comparisons["stage1"] = {"differences": stage1["differences"]}
            if stage1["differences"]:
                failures.append(_failure("stage1_repeatability", "verification/stage1/repeatability", stage1["differences"]))
        except Exception as error:
            failures.append(_failure("stage1_repeatability", "verification/stage1/repeatability", str(error)))
    if mode == "audit":
        for step, (roots, artifacts) in REPEAT_SPECS.items():
            for layer, name in artifacts:
                try:
                    manifests = [json.loads(store.paths.factor_root.joinpath(
                        root, "factor_research", layer, name, "manifest.json").read_text()) for root in roots]
                    same = manifests[0]["rows"] == manifests[1]["rows"] and manifests[0]["sha256"] == manifests[1]["sha256"]
                    comparisons[f"{step}/{layer}/{name}"] = {"rows": manifests[0]["rows"], "identical": same}
                    if not same:
                        failures.append(_failure("repeatability", f"{step}/{layer}/{name}", "row or sha mismatch"))
                except Exception as error:
                    failures.append(_failure("repeatability", f"{step}/{layer}/{name}", str(error)))
        try:
            roots = [store.paths.factor_root / name / "factor_research" / "l4" / "factor_report"
                     for name in REPEAT_STEP9_ROOTS]
            manifests = [json.loads((root / "manifest.json").read_text()) for root in roots]
            same = manifests[0] == manifests[1]
            for filename, digest in manifests[0]["files"].items():
                actual = [file_sha256(root / filename) for root in roots]
                same = same and actual == [digest, digest]
            comparisons["step9/factor_report"] = {"files": len(manifests[0]["files"]), "identical": same}
            if not same:
                failures.append(_failure("repeatability", "step9/factor_report", "manifest or file sha mismatch"))
        except Exception as error:
            failures.append(_failure("repeatability", "step9/factor_report", str(error)))
    return {"passed": not failures, "failures": failures, "comparisons": comparisons,
            "stage1_executed": stage1_executed, "stage2_executed": mode == "audit",
            "status": "executed" if mode == "audit" else "stage2_repeat_not_requested",
            **verification_metadata(mode)}


def check_documentation(path=None):
    path = Path(path or PROJECT_ROOT / "docs" / "STAGE2_EXECUTION.md")
    try:
        text = path.read_text(encoding="utf-8")
        present = {int(value) for value in re.findall(r"^## Step\s+(\d+)", text, re.M)}
        missing = sorted(set(range(11)) - present)
        return {"passed": not missing, "missing_steps": missing, "path": str(path),
                "failures": [] if not missing else [_failure("documentation_steps", str(path), missing)]}
    except Exception as error:
        return {"passed": False, "missing_steps": list(range(11)), "path": str(path),
                "failures": [_failure("documentation_steps", str(path), str(error))]}


def _boundaries(store):
    capabilities = store.rules["capabilities"]
    return [
        {"id": "industry_pit_history", "status": capabilities["industry_pit_history"]["status"],
         "impact": "真实行业仅当前快照；strict 与 backfilled 必须分开披露"},
        {"id": "official_index_verification", "status": capabilities["official_index_verification"]["status"],
         "impact": "沪深300/中证500官方日线与权重核验挂起，自建基准不是官方复刻"},
        {"id": "limit_events_complete", "status": store.rules["trade_status"]["limit_events_complete"],
         "impact": "一字涨停可交易口径仅在已配置覆盖范围内使用"},
    ]


def _markdown(report):
    lines = ["# Stage 2 统一质量门禁", "",
             f"总体结果：**{'通过' if report['passed'] else '失败'}**。", "",
             "| 检查 | 结果 | 失败数 |", "|---|---|---:|"]
    for name, check in report["checks"].items():
        lines.append(f"| {name} | {'通过' if check['passed'] else '失败'} | {len(check.get('failures', []))} |")
    lines += ["", "## 已披露边界（不作为通过项掩盖）", ""]
    for item in report["boundaries"]:
        lines.append(f"- `{item['id']}` = `{item['status']}`：{item['impact']}")
    lines += ["", "## 失败明细", ""]
    if not report["failures"]:
        lines.append("无。")
    for failure in report["failures"]:
        lines.append(f"- `{failure['check']}` / `{failure['table']}`：`{json_text(failure['detail'])}`")
    lines.append("")
    return "\n".join(lines)


def run_stage2_quality_gate(store, truncation_dates=None, deep_primary_keys=False, docs_path=None,
                            config_root=None, verification="standard", entry_result=None):
    mode = verification_mode(verification)
    research = load_settings("factor_research.yaml", config_root)
    dates = truncation_dates if truncation_dates is not None else research["quality_gate"]["truncation_dates"]
    checks = {"architecture": check_architecture()}
    try:
        entry = entry_result or check_stage2_entry(store, config_root, verification=mode)
        checks["stage1_entry"] = {"passed": True, "failures": [], "bundle_sha256": entry["bundle_sha256"],
                                  "file_hash_verified": entry["file_hash_verified"],
                                  **verification_metadata(entry["verification"])}
    except Exception as error:
        checks["stage1_entry"] = {"passed": False, "failures": [_failure("stage2_entry", "stage1", str(error))]}
    checks["layering_lineage"] = check_layering(
        store, deep_primary_keys=deep_primary_keys, verification=mode)
    checks["pit"] = check_pit(store, config_root)
    if dates:
        logging.info("Unified truncation gate: %s", dates)
        truncation = check_truncation_invariance(store, dates, config_root=config_root)
        store.write_json(("verification", "step10_truncation.json"), truncation)
        checks["truncation"] = {"passed": truncation["passed"], "failures": [
            _failure("truncation_invariance", "factor_value", truncation["mismatches"][:5])
        ] if not truncation["passed"] else [], "compared_values": truncation["compared_values"],
            "dates": truncation["dates"]}
    else:
        checks["truncation"] = {"passed": False, "failures": [_failure(
            "truncation_invariance", "factor_value", "no truncation date was executed")]}
    checks["repeatability"] = check_repeatability(store, verification=mode)
    checks["out_of_sample"] = check_oos_status(store)
    checks["documentation"] = check_documentation(docs_path)
    failures = [failure for check in checks.values() for failure in check.get("failures", [])]
    report = {"passed": not failures, "code_hash": store.code_hash, "config_hash": store.config_hash,
              "research_registry_sha256": hashlib.sha256(json_text(
                  {"research": research, "registry": factor_map(config_root)}).encode("utf-8")).hexdigest(),
              "checks": checks, "failures": failures, "boundaries": _boundaries(store),
              **verification_metadata(mode)}
    store.write_json(("verification", "stage2_quality_gate.json"), report)
    store.output("verification", "stage2_quality_gate.md").write_text(_markdown(report), encoding="utf-8")
    return report
