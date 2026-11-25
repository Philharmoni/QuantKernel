"""Verify Step 6 determinism, truncation, future perturbation and NAV identities."""
import json

import _bootstrap  # noqa: F401
from core.config.paths import PROJECT_ROOT
from core.strategy.benchmark_build import Step6Builder, fetch_dicts
from core.strategy.benchmark_publish import TABLE_KEYS, default_step6_run_id, publish_step6
from core.strategy.verification import repeat_metadata, resolve_repeat, table_repeat_mismatches
from core.quality.verification import verification_mode
from core.strategy.contract import file_sha256, load_stage3_contract, parse_ymd
from core.strategy.inputs import SnapshotInputs


def sorted_rows(rows, fields):
    return sorted(rows, key=lambda row: tuple(row[field] for field in fields))


def main():
    mode = verification_mode()
    config = load_stage3_contract()["config"]
    root = PROJECT_ROOT / config["delivery"]["root"]
    run_id = default_step6_run_id()
    run_root = root / "runs" / run_id
    manifest = json.loads((run_root / "manifest.json").read_text(encoding="utf-8"))
    repeat_root = root / "verification" / (run_id + "_repeat")
    cutoff = parse_ymd(config["step6"]["truncation_date"])
    with SnapshotInputs(root / "inputs" / config["delivery"]["active_input_bundle"], verification=mode) as inputs:
        def builder():
            return Step6Builder(inputs, root / "runs" / config["delivery"]["active_step3_run"],
                root / "runs" / config["delivery"]["active_step4_run"],
                root / "runs" / config["delivery"]["active_step5_run"], config)
        repeat, repeat_status = resolve_repeat(repeat_root, lambda: publish_step6(
            builder(), repeat_root, repeat_root.name), mode)
        truncated = builder().build(end_date=cutoff)
        perturbed = builder().build(perturb_after=cutoff)
        db = inputs.db
        nav_path = run_root / "l5/account_nav_daily/part-00000.parquet"
        target_path = run_root / "l5/benchmark_target/part-00000.parquet"
        full_nav = fetch_dicts(db, "SELECT * FROM read_parquet(?) WHERE trade_date<=? ORDER BY account_id,trade_date",
                               [str(nav_path), cutoff])
        full_targets = fetch_dicts(db, "SELECT * FROM read_parquet(?) WHERE planned_execution_date<=?",
                                   [str(target_path), cutoff])
        trunc_nav = sorted_rows([row for row in truncated["account_nav_daily"] if row["trade_date"] <= cutoff],
                                ["account_id", "trade_date"])
        trunc_targets = sorted_rows([row for row in truncated["benchmark_target"] if row["planned_execution_date"] <= cutoff],
                                    ["benchmark_id", "signal_date", "ts_code"])
        full_targets = sorted_rows(full_targets, ["benchmark_id", "signal_date", "ts_code"])
        perturb_nav = sorted_rows([row for row in perturbed["account_nav_daily"] if row["trade_date"] <= cutoff],
                                  ["account_id", "trade_date"])
        nav = f"read_parquet('{nav_path}')"
        performance = f"read_parquet('{run_root}/l5/performance/part-00000.parquet')"
        attribution = f"read_parquet('{run_root}/l5/attribution/part-00000.parquet')"
        observations = {
            "table_rows": {name: entry["rows"] for name, entry in manifest["tables"].items()},
            "repeat_hash_mismatches": table_repeat_mismatches(manifest, repeat, TABLE_KEYS),
            "truncated_nav_prefix_mismatch": int(full_nav != trunc_nav),
            "truncated_target_prefix_mismatch": int(full_targets != trunc_targets),
            "future_quote_prefix_mismatch": int(full_nav != perturb_nav),
            "nav_identity_abs": db.execute(f"""SELECT max(greatest(abs(reference_nav_cny-total_cash-reference_position_value_cny),
                abs(stress_nav_cny-total_cash-stress_position_value_cny))) FROM {nav}""").fetchone()[0],
            "calendar_inconsistent_days": db.execute(f"""SELECT count(*) FROM (
                SELECT trade_date,count(DISTINCT account_id) n FROM {nav} GROUP BY trade_date) WHERE n<>9""").fetchone()[0],
            "performance_rebuild_abs": db.execute(f"""WITH last AS (
                SELECT account_id,reference_nav_cny,row_number() OVER(PARTITION BY account_id ORDER BY trade_date DESC) rn FROM {nav})
                SELECT max(abs(p.cumulative_return-(l.reference_nav_cny/{float(config['objective']['initial_capital_cny'])}-1)))
                FROM {performance} p JOIN last l USING(account_id) WHERE l.rn=1""").fetchone()[0],
            "attribution_identity_abs": db.execute(f"""SELECT max(abs(right_cumulative_return-left_cumulative_return-cumulative_return_gap))
                FROM {attribution}""").fetchone()[0],
            "non_r1_rows": db.execute(f"SELECT count(*) FROM {nav} WHERE accounting_mode<>'adjusted_unit_proxy'").fetchone()[0],
            "research_labels_read": "l3/forward_return" in manifest["input_read_log"],
        }
    failures = []
    zero_checks = ("truncated_nav_prefix_mismatch", "truncated_target_prefix_mismatch", "future_quote_prefix_mismatch",
                   "calendar_inconsistent_days", "non_r1_rows")
    if observations["repeat_hash_mismatches"] or observations["research_labels_read"]:
        failures.append({"identity_or_permission": observations})
    for name in zero_checks:
        if observations[name]:
            failures.append({"check": name, "value": observations[name]})
    for name in ("nav_identity_abs", "performance_rebuild_abs", "attribution_identity_abs"):
        if observations[name] is None or observations[name] > 1e-8:
            failures.append({"check": name, "value": observations[name]})
    result = {"passed": not failures, "step": 6, "run_id": run_id, "failures": failures,
        "observations": observations, "small_window": config["step6"]["small_window"],
        "economic_promoted": False, "can_enter_step7": not failures,
        "real_share_account_ready": False, "live_ready": False,
        **repeat_metadata(mode, repeat_status)}
    target = run_root / "verification/step6_checks.json"
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    manifest["verification"] = {"passed": result["passed"], "path": "verification/step6_checks.json",
                                "sha256": file_sha256(target)}
    (run_root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
