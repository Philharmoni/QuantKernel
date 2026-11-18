"""Verify Step 13 repeatability, PIT truncation and stale-pressure accounting."""
import json

import _bootstrap  # noqa: F401
from core.config.paths import PROJECT_ROOT
from core.strategy.benchmark_build import fetch_dicts
from core.strategy.contract import file_sha256, load_stage3_contract, parse_ymd
from core.strategy.inputs import SnapshotInputs
from core.strategy.robustness import prefix_comparison
from core.strategy.robustness_build import Step13Builder
from core.strategy.robustness_publish import TABLE_KEYS, default_step13_run_id, publish_step13


def main():
    config = load_stage3_contract()["config"]
    root = PROJECT_ROOT / "data_middle/strategy_research"
    run_id = default_step13_run_id()
    run_root = root / "runs" / run_id
    manifest = json.loads((run_root / "manifest.json").read_text(encoding="utf-8"))
    repeat_root = root / "verification" / (run_id + "_repeat")
    with SnapshotInputs(root / "inputs" / config["delivery"]["active_input_bundle"]) as inputs:
        def builder():
            return Step13Builder(inputs, root / "runs" / config["delivery"]["active_step4_run"],
                root / "runs" / config["delivery"]["active_step5_run"],
                root / "runs" / config["delivery"]["active_step7_run"], config)

        if repeat_root.exists():
            repeat = json.loads((repeat_root / "manifest.json").read_text(encoding="utf-8"))
        else:
            repeat = publish_step13(builder(), repeat_root, repeat_root.name)
        db = inputs.db
        nav = f"read_parquet('{run_root}/l5/robustness_account_nav/part-00000.parquet')"
        stale = f"read_parquet('{run_root}/l5/stale_pressure_daily/part-00000.parquet')"
        synthetic = f"read_parquet('{run_root}/l5/synthetic_position_pressure/part-00000.parquet')"
        registry = f"read_parquet('{run_root}/l5/robustness_registry/part-00000.parquet')"

        def account(robustness_id):
            return fetch_dicts(db, f"""SELECT trade_date,total_cash,reference_position_value_cny,
                stress_position_value_cny,reference_nav_cny,stress_nav_cny FROM {nav}
                WHERE robustness_id=? ORDER BY trade_date""", [robustness_id])

        full, truncated, future = account("full_design"), account("real_truncation"), account("future_quote_perturbation")
        cutoff = parse_ymd(manifest["truncation_date"])
        source = fetch_dicts(db, """SELECT trade_date,total_cash,reference_position_value_cny,
            stress_position_value_cny,reference_nav_cny,stress_nav_cny FROM read_parquet(?)
            WHERE account_id='c0_three_module_fixed__executed_proxy_net'
            AND trade_date BETWEEN '2019-07-01' AND '2021-12-31' ORDER BY trade_date""",
            [str(root / "runs" / config["delivery"]["active_step7_run"] /
                 "l5/account_nav_daily/part-00000.parquet")])
        truncation = prefix_comparison(full, truncated, cutoff)
        future_prefix = prefix_comparison(full, future, cutoff)
        full_after = [row for row in full if row["trade_date"] > cutoff]
        future_after = [row for row in future if row["trade_date"] > cutoff]
        future_changed = sum(left != right for left, right in zip(full_after, future_after))
        future_changed += abs(len(full_after) - len(future_after))
        pit = {row["check_id"]: row for row in fetch_dicts(
            db, f"SELECT * FROM read_parquet('{run_root}/l5/pit_access_audit/part-00000.parquet')")}
        pit_failures = int(not (
            pit["cutoff_read_allowed"]["access_allowed"] and not pit["cutoff_read_allowed"]["blocked"]
            and pit["cutoff_read_allowed"]["provider_calls"] == 1
            and pit["future_read_blocked"]["blocked"] and not pit["future_read_blocked"]["access_allowed"]
            and pit["future_read_blocked"]["provider_calls"] == 0
            and pit["future_read_blocked"]["error_type"] == "PermissionError"))
        observations = {
            "table_rows": {name: entry["rows"] for name, entry in manifest["tables"].items()},
            "repeat_hash_mismatches": [table for table in TABLE_KEYS
                if manifest["tables"][table]["sha256"] != repeat["tables"][table]["sha256"]],
            "full_source_mismatch": int(full != source),
            "truncation_prefix_mismatch": truncation["mismatch_count"],
            "truncation_future_rows": truncation["variant_future_rows"],
            "future_prefix_mismatch": future_prefix["mismatch_count"],
            "future_post_cutoff_changed_rows": future_changed,
            "pit_audit_failures": pit_failures,
            "nav_identity_abs": db.execute(f"""SELECT max(greatest(
                abs(reference_nav_cny-total_cash-reference_position_value_cny),
                abs(stress_nav_cny-total_cash-stress_position_value_cny))) FROM {nav}""").fetchone()[0],
            "stale_day_count_gap": abs(len(full) - db.execute(f"SELECT count(*) FROM {stale}").fetchone()[0]),
            "stale_position_rebuild_abs": db.execute(f"""SELECT max(greatest(
                abs(reference_position_value_cny-source_reference_position_value_cny),
                stress_position_abs_error)) FROM {stale}""").fetchone()[0],
            "stale_nav_rebuild_abs": db.execute(f"SELECT max(stress_nav_abs_error) FROM {stale}").fetchone()[0],
            "synthetic_formula_abs": db.execute(f"""SELECT max(greatest(
                abs(stress_value_cny-reference_value_cny*applied_factor),
                abs(applied_factor-CASE WHEN scenario_non_tradable OR scenario_stale_trade_days>=120 THEN 0.0
                    WHEN scenario_stale_trade_days>=60 THEN 0.5 ELSE 1.0 END))) FROM {synthetic}""").fetchone()[0],
            "synthetic_cash_or_fill_rows": db.execute(
                f"SELECT count(*) FROM {synthetic} WHERE cash_release_cny<>0 OR generated_fill").fetchone()[0],
            "synthetic_lot_coverage_failures": db.execute(f"""SELECT count(*) FROM (
                SELECT valuation_date,ts_code,lot_id,count(DISTINCT scenario_id) n FROM {synthetic}
                GROUP BY ALL HAVING n<>3)""").fetchone()[0],
            "design_date_violations": db.execute(f"""SELECT count(*) FROM {nav}
                WHERE trade_date<'2019-07-01' OR trade_date>'2021-12-31'
                OR (robustness_id='real_truncation' AND trade_date>? )""", [cutoff]).fetchone()[0],
            "promoted_or_r2_rows": db.execute(
                f"SELECT count(*) FROM {registry} WHERE economic_promoted OR r2_backtest_permitted").fetchone()[0],
            "research_labels_read": "l3/forward_return" in manifest["input_read_log"],
        }
    failures = []
    if observations["repeat_hash_mismatches"] or observations["research_labels_read"]:
        failures.append({"identity_or_permission": observations})
    for name in ("full_source_mismatch", "truncation_prefix_mismatch", "truncation_future_rows",
                 "future_prefix_mismatch", "pit_audit_failures", "stale_day_count_gap",
                 "synthetic_cash_or_fill_rows", "synthetic_lot_coverage_failures",
                 "design_date_violations", "promoted_or_r2_rows"):
        if observations[name]:
            failures.append({"check": name, "value": observations[name]})
    if observations["future_post_cutoff_changed_rows"] <= 0:
        failures.append({"check": "future_perturbation_must_change_post_cutoff_account"})
    for name in ("nav_identity_abs", "stale_position_rebuild_abs", "stale_nav_rebuild_abs", "synthetic_formula_abs"):
        if observations[name] is None or observations[name] > 1e-8:
            failures.append({"check": name, "value": observations[name]})
    result = {"passed": not failures, "step": 13, "run_id": run_id, "failures": failures,
        "observations": observations, "accounting_mode": "adjusted_unit_proxy",
        "economic_promoted": False, "can_enter_step14": not failures, "can_enter_step11": False,
        "r2_backtest_permitted": False, "confirmation_seen": False, "oos_accessed": False, "live_ready": False}
    target = run_root / "verification/step13_checks.json"
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    manifest["verification"] = {"passed": result["passed"], "path": "verification/step13_checks.json",
                                "sha256": file_sha256(target)}
    (run_root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
