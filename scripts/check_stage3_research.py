"""Verify Step 7 design research identities, scope and independent repeat."""
import json
import math

import _bootstrap  # noqa: F401
from core.config.paths import PROJECT_ROOT
from core.strategy.benchmark_build import fetch_dicts
from core.strategy.contract import file_sha256, load_stage3_contract
from core.strategy.inputs import SnapshotInputs
from core.strategy.research_publish import TABLE_KEYS, default_step7_run_id, publish_step7
from core.strategy.verification import repeat_metadata, resolve_repeat, table_repeat_mismatches
from core.quality.verification import verification_mode


def main():
    mode = verification_mode()
    config = load_stage3_contract()["config"]
    root = PROJECT_ROOT / config["delivery"]["root"]
    run_id = default_step7_run_id()
    run_root = root / "runs" / run_id
    manifest = json.loads((run_root / "manifest.json").read_text(encoding="utf-8"))
    repeat_root = root / "verification" / (run_id + "_repeat")
    with SnapshotInputs(root / "inputs" / config["delivery"]["active_input_bundle"], verification=mode) as inputs:
        def builder():
            from core.strategy.research_build import Step7Builder
            return Step7Builder(inputs, root / "runs" / config["delivery"]["active_step3_run"],
                root / "runs" / config["delivery"]["active_step4_run"],
                root / "runs" / config["delivery"]["active_step5_run"], config)
        repeat, repeat_status = resolve_repeat(repeat_root, lambda: publish_step7(
            builder(), repeat_root, repeat_root.name), mode)
        db = inputs.db
        nav = f"read_parquet('{run_root}/l5/account_nav_daily/part-00000.parquet')"
        performance = f"read_parquet('{run_root}/l5/performance/part-00000.parquet')"
        monthly = f"read_parquet('{run_root}/l5/monthly_return/part-00000.parquet')"
        attribution = f"read_parquet('{run_root}/l5/attribution/part-00000.parquet')"
        execution = f"read_parquet('{run_root}/l5/execution_summary/part-00000.parquet')"
        exposure = f"read_parquet('{run_root}/l5/exposure_daily/part-00000.parquet')"
        trials = f"read_parquet('{run_root}/l5/research_trial/part-00000.parquet')"
        monthly_rows = fetch_dicts(db, f"SELECT * FROM {monthly}")
        design_rows = {row["account_id"]: row for row in fetch_dicts(db,
            f"SELECT * FROM {performance} WHERE period_id='design'")}
        monthly_gap = 0.0
        for account_id, expected in design_rows.items():
            compound = 1.0
            for row in monthly_rows:
                if row["account_id"] == account_id:
                    compound *= 1.0 + float(row["return"])
            monthly_gap = max(monthly_gap, abs(compound - 1.0 - float(expected["cumulative_return"])))
        observations = {
            "table_rows": {name: entry["rows"] for name, entry in manifest["tables"].items()},
            "repeat_hash_mismatches": table_repeat_mismatches(manifest, repeat, TABLE_KEYS),
            "nav_identity_abs": db.execute(f"""SELECT max(greatest(abs(reference_nav_cny-total_cash-reference_position_value_cny),
                abs(stress_nav_cny-total_cash-stress_position_value_cny))) FROM {nav}""").fetchone()[0],
            "calendar_inconsistent_days": db.execute(f"""SELECT count(*) FROM (
                SELECT trade_date,count(DISTINCT account_id) n FROM {nav} GROUP BY trade_date) WHERE n<>9""").fetchone()[0],
            "performance_rebuild_abs": db.execute(f"""WITH last AS (
                SELECT account_id,reference_nav_cny,row_number() OVER(PARTITION BY account_id ORDER BY trade_date DESC) rn FROM {nav})
                SELECT max(abs(p.cumulative_return-(l.reference_nav_cny/{float(config['objective']['initial_capital_cny'])}-1)))
                FROM {performance} p JOIN last l USING(account_id) WHERE p.period_id='design' AND l.rn=1""").fetchone()[0],
            "monthly_compound_abs": monthly_gap,
            "attribution_identity_abs": db.execute(f"""SELECT max(abs(right_cumulative_return-left_cumulative_return-cumulative_return_gap))
                FROM {attribution}""").fetchone()[0],
            "execution_cost_identity_abs": db.execute(f"""SELECT abs(total_cost_cny-commission_cny-transfer_cny-tax_cny-impact_cny)
                FROM {execution}""").fetchone()[0],
            "exposure_rule_violations": db.execute(f"""SELECT count(*) FROM {exposure}
                WHERE hhi<0 OR effective_position_count<0 OR top1_weight<0 OR cash_weight<0
                   OR stale_weight<0 OR non_tradable_weight<0""").fetchone()[0],
            "completed_candidate_count": db.execute(f"SELECT count(*) FROM {trials} WHERE status='completed'").fetchone()[0],
            "not_run_candidate_count": db.execute(f"SELECT count(*) FROM {trials} WHERE status<>'completed'").fetchone()[0],
            "forbidden_scope_rows": db.execute(f"""SELECT count(*) FROM {trials}
                WHERE validation_seen OR confirmation_seen OR oos_accessed OR economic_promoted""").fetchone()[0],
            "research_labels_read": "l3/forward_return" in manifest["input_read_log"],
        }
    failures = []
    if observations["repeat_hash_mismatches"] or observations["research_labels_read"]:
        failures.append({"identity_or_permission": observations})
    for name in ("calendar_inconsistent_days", "exposure_rule_violations", "forbidden_scope_rows"):
        if observations[name]:
            failures.append({"check": name, "value": observations[name]})
    for name in ("nav_identity_abs", "performance_rebuild_abs", "monthly_compound_abs",
                 "attribution_identity_abs", "execution_cost_identity_abs"):
        if observations[name] is None or observations[name] > 1e-8:
            failures.append({"check": name, "value": observations[name]})
    if observations["completed_candidate_count"] != len(config["step7"]["candidate_ids_to_run"]):
        failures.append({"check": "completed_candidate_count"})
    result = {"passed": not failures, "step": 7, "run_id": run_id, "failures": failures,
        "observations": observations, "economic_promoted": False, "can_enter_step8": not failures,
        "validation_seen": False, "confirmation_seen": False, "oos_accessed": False,
        "real_share_account_ready": False, "live_ready": False,
        **repeat_metadata(mode, repeat_status)}
    target = run_root / "verification/step7_checks.json"
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    manifest["verification"] = {"passed": result["passed"], "path": "verification/step7_checks.json",
                                "sha256": file_sha256(target)}
    (run_root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
