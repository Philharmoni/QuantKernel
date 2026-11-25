"""Verify Step 12 repeatability, source identity, OLS and design-only scope."""
import json

import _bootstrap  # noqa: F401
from core.config.paths import PROJECT_ROOT
from core.strategy.benchmark_build import fetch_dicts
from core.strategy.contract import file_sha256, load_stage3_contract
from core.strategy.inputs import SnapshotInputs
from core.strategy.sensitivity import relative_ols
from core.strategy.sensitivity_build import Step12Builder
from core.strategy.sensitivity_publish import TABLE_KEYS, default_step12_run_id, publish_step12
from core.strategy.verification import repeat_metadata, resolve_repeat, table_repeat_mismatches
from core.quality.verification import verification_mode


def main():
    mode = verification_mode()
    config = load_stage3_contract()["config"]
    root = PROJECT_ROOT / config["delivery"]["root"]
    run_id = default_step12_run_id()
    run_root = root / "runs" / run_id
    manifest = json.loads((run_root / "manifest.json").read_text(encoding="utf-8"))
    repeat_root = root / "verification" / (run_id + "_repeat")
    with SnapshotInputs(root / "inputs" / config["delivery"]["active_input_bundle"], verification=mode) as inputs:
        def builder():
            return Step12Builder(inputs, root / "runs" / config["delivery"]["active_step4_run"],
                root / "runs" / config["delivery"]["active_step5_run"], root / "runs" / config["delivery"]["active_step7_run"], config)
        repeat, repeat_status = resolve_repeat(repeat_root, lambda: publish_step12(
            builder(), repeat_root, repeat_root.name), mode)
        db = inputs.db
        nav = f"read_parquet('{run_root}/l5/sensitivity_account_nav/part-00000.parquet')"
        performance = f"read_parquet('{run_root}/l5/sensitivity_performance/part-00000.parquet')"
        registry = f"read_parquet('{run_root}/l5/sensitivity_registry/part-00000.parquet')"
        main = fetch_dicts(db, f"SELECT * FROM {nav} WHERE sensitivity_id='main_net' ORDER BY trade_date")
        source = fetch_dicts(db, """SELECT trade_date,total_cash,reference_position_value_cny,stress_position_value_cny,
            reference_nav_cny,stress_nav_cny FROM read_parquet(?) WHERE account_id='c0_three_module_fixed__executed_proxy_net'
            AND trade_date BETWEEN '2019-07-01' AND '2021-12-31' ORDER BY trade_date""",
            [str(root / "runs" / config["delivery"]["active_step7_run"] / "l5/account_nav_daily/part-00000.parquet")])
        main_simple = [{key: row[key] for key in source[0]} for row in main]
        ols_gap = 0.0
        for row in fetch_dicts(db, f"SELECT * FROM read_parquet('{run_root}/l5/relative_ols/part-00000.parquet')"):
            account = row["benchmark_id"].replace("_net", "") + "__executed_proxy_net"
            benchmark = fetch_dicts(db, "SELECT * FROM read_parquet(?) WHERE account_id=? AND trade_date BETWEEN '2019-07-01' AND '2021-12-31' ORDER BY trade_date",
                [str(root / "runs" / config["delivery"]["active_step7_run"] / "l5/account_nav_daily/part-00000.parquet"), account])
            actual = relative_ols(main, benchmark, float(config["objective"]["initial_capital_cny"]))
            ols_gap = max(ols_gap, max(abs(float(row[key]) - float(actual[key])) for key in actual))
        observations = {
            "table_rows": {name: entry["rows"] for name, entry in manifest["tables"].items()},
            "repeat_hash_mismatches": table_repeat_mismatches(manifest, repeat, TABLE_KEYS),
            "main_source_mismatch": int(main_simple != source),
            "nav_identity_abs": db.execute(f"""SELECT max(greatest(abs(reference_nav_cny-total_cash-reference_position_value_cny),
                abs(stress_nav_cny-total_cash-stress_position_value_cny))) FROM {nav}""").fetchone()[0],
            "performance_rebuild_abs": db.execute(f"""WITH last AS (
                SELECT sensitivity_id,reference_nav_cny,row_number() OVER(PARTITION BY sensitivity_id ORDER BY trade_date DESC) rn FROM {nav})
                SELECT max(abs(p.cumulative_return-(l.reference_nav_cny/p.baseline_nav_cny-1)))
                FROM {performance} p JOIN last l USING(sensitivity_id) WHERE p.period_id='design' AND l.rn=1""").fetchone()[0],
            "relative_ols_abs": ols_gap,
            "design_date_violations": db.execute(f"SELECT count(*) FROM {nav} WHERE trade_date<'2019-07-01' OR trade_date>'2021-12-31'").fetchone()[0],
            "promoted_or_r2_rows": db.execute(f"SELECT count(*) FROM {registry} WHERE economic_promoted").fetchone()[0],
            "research_labels_read": "l3/forward_return" in manifest["input_read_log"],
        }
    failures = []
    if observations["repeat_hash_mismatches"] or observations["research_labels_read"]:
        failures.append({"identity_or_permission": observations})
    for name in ("main_source_mismatch", "design_date_violations", "promoted_or_r2_rows"):
        if observations[name]:
            failures.append({"check": name, "value": observations[name]})
    for name in ("nav_identity_abs", "performance_rebuild_abs", "relative_ols_abs"):
        if observations[name] is None or observations[name] > 1e-8:
            failures.append({"check": name, "value": observations[name]})
    result = {"passed": not failures, "step": 12, "run_id": run_id, "failures": failures,
        "observations": observations, "economic_promoted": False, "can_enter_step13": not failures,
        "r2_backtest_permitted": False, "confirmation_seen": False, "oos_accessed": False, "live_ready": False,
        **repeat_metadata(mode, repeat_status)}
    target = run_root / "verification/step12_checks.json"
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    manifest["verification"] = {"passed": result["passed"], "path": "verification/step12_checks.json",
                                "sha256": file_sha256(target)}
    (run_root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
