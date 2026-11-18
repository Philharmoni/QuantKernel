"""Independently repeat and reconcile the Step 5 R1 execution account."""
import json

import _bootstrap  # noqa: F401
from core.config.paths import PROJECT_ROOT
from core.strategy.contract import file_sha256, load_stage3_contract
from core.strategy.execution_build import TABLE_KEYS, Step5Builder, default_step5_run_id, publish_step5
from core.strategy.inputs import SnapshotInputs


def main():
    config = load_stage3_contract()["config"]
    root = PROJECT_ROOT / "data_middle/strategy_research"
    run_id = default_step5_run_id()
    run_root = root / "runs" / run_id
    manifest = json.loads((run_root / "manifest.json").read_text(encoding="utf-8"))
    repeat_root = root / "verification" / (run_id + "_repeat")
    with SnapshotInputs(root / "inputs" / config["delivery"]["active_input_bundle"]) as inputs:
        if repeat_root.exists():
            repeat = json.loads((repeat_root / "manifest.json").read_text(encoding="utf-8"))
        else:
            repeat = publish_step5(Step5Builder(inputs, root / "runs" / config["delivery"]["active_step4_run"], config),
                                   repeat_root, repeat_root.name)
        db = inputs.db
        nav = f"read_parquet('{run_root}/l5/nav_daily/part-00000.parquet')"
        cash = f"read_parquet('{run_root}/l5/cash_ledger/part-00000.parquet')"
        risk = f"read_parquet('{run_root}/l5/risk_daily/part-00000.parquet')"
        fills = f"read_parquet('{run_root}/l5/fill/part-00000.parquet')"
        costs = f"read_parquet('{run_root}/l5/cost_daily/part-00000.parquet')"
        events = f"read_parquet('{run_root}/l5/order_event/part-00000.parquet')"
        eligibility = f"read_parquet('{run_root}/l4/execution_eligibility/part-00000.parquet')"
        lots = f"read_parquet('{run_root}/l5/position_lot/part-00000.parquet')"
        last_nav = db.execute(f"SELECT * FROM {nav} ORDER BY trade_date DESC LIMIT 1").fetchone()
        nav_names = [row[0] for row in db.description]
        last = dict(zip(nav_names, last_nav))
        cash_balances = db.execute(f"SELECT sum(available_delta),sum(sale_restricted_delta),sum(other_frozen_delta) FROM {cash}").fetchone()
        terminal_conservation = db.execute(f"""WITH latest AS (
            SELECT * FROM (SELECT *,row_number() OVER(PARTITION BY order_id ORDER BY event_seq DESC) rn FROM {events})
            WHERE rn=1 AND event_type<>'submitted')
            SELECT max(abs(requested_notional_cny-filled_notional_cny-remaining_notional_cny)) FROM latest""").fetchone()[0]
        observations = {
            "table_rows": {name: entry["rows"] for name, entry in manifest["tables"].items()},
            "repeat_hash_mismatches": [table for table in TABLE_KEYS
                if manifest["tables"][table]["sha256"] != repeat["tables"][table]["sha256"]],
            "research_labels_read": "l3/forward_return" in manifest["input_read_log"],
            "max_nav_identity_abs": db.execute(f"""SELECT max(abs(reference_nav_cny-total_cash-reference_position_value_cny)) FROM {nav}""").fetchone()[0],
            "max_stress_nav_identity_abs": db.execute(f"""SELECT max(abs(stress_nav_cny-total_cash-stress_position_value_cny)) FROM {nav}""").fetchone()[0],
            "cash_reconciliation_abs": max(abs(cash_balances[0] - last["available_cash"]),
                abs(cash_balances[1] - last["sale_restricted_cash"]), abs(cash_balances[2] - last["other_frozen_cash"])),
            "cost_reconciliation_abs": db.execute(f"""SELECT abs((SELECT sum(total_cost_cny) FROM {fills})-(SELECT sum(total_cost) FROM {costs}))""").fetchone()[0],
            "terminal_order_conservation_abs": terminal_conservation,
            "negative_cash_days": db.execute(f"SELECT count(*) FROM {nav} WHERE available_cash<0 OR sale_restricted_cash<0 OR other_frozen_cash<0").fetchone()[0],
            "negative_lot_rows": db.execute(f"SELECT count(*) FROM {lots} WHERE units<0 OR sellable_units<0 OR frozen_units<0").fetchone()[0],
            "account_rule_violations": db.execute(f"""SELECT sum(oversell_count+t_plus_one_violation_count+negative_cash_count) FROM {risk}""").fetchone()[0],
            "fills_without_eligibility": db.execute(f"""SELECT count(*) FROM {fills} f JOIN {eligibility} e
                ON f.execution_date=e.execution_date AND f.ts_code=e.ts_code
                WHERE (f.side='BUY' AND NOT e.buy_eligible) OR (f.side='SELL' AND NOT e.sell_eligible)""").fetchone()[0],
            "non_proxy_fills": db.execute(f"SELECT count(*) FROM {fills} WHERE fill_model<>'proxy_not_observed'").fetchone()[0],
            "quantity_rule_violations": db.execute(f"""SELECT count(*) FROM {fills}
                WHERE share_equivalent<=0 OR (full_position_odd_lot AND side<>'SELL') OR (NOT full_position_odd_lot AND (
                    (quantity_rule_id='default_min100_step100' AND (share_equivalent<100-1e-7
                        OR abs(share_equivalent/100-round(share_equivalent/100))>1e-7))
                    OR (quantity_rule_id='star_min200_step1' AND (share_equivalent<200-1e-7
                        OR abs(share_equivalent-round(share_equivalent))>1e-7))
                    OR quantity_rule_id NOT IN ('default_min100_step100','star_min200_step1')))""").fetchone()[0],
            "r2_ready_rows": db.execute(f"SELECT count(*) FROM {nav} WHERE real_share_account_ready").fetchone()[0],
        }
    numeric_zero = [name for name, value in observations.items() if name.endswith(("_abs", "_days", "_rows", "_violations"))
                    and name not in {"table_rows", "r2_ready_rows"} and value is not None]
    numeric_zero += ["fills_without_eligibility", "non_proxy_fills"]
    failures = []
    if observations["repeat_hash_mismatches"] or observations["research_labels_read"]:
        failures.append({"identity_or_permission": observations})
    for name in numeric_zero:
        if observations[name] > 1e-7:
            failures.append({"check": name, "value": observations[name]})
    if observations["r2_ready_rows"]:
        failures.append({"check": "r2_must_remain_blocked"})
    result = {"passed": not failures, "step": 5, "run_id": run_id, "failures": failures,
              "observations": observations, "accounting_mode": "adjusted_unit_proxy",
              "economic_promoted": False, "can_enter_step6": not failures and manifest["tables"]["fill"]["rows"] > 0,
              "real_share_account_ready": False, "live_ready": False}
    target = run_root / "verification/step5_checks.json"
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    manifest["verification"] = {"passed": result["passed"], "path": "verification/step5_checks.json",
                                "sha256": file_sha256(target)}
    (run_root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
