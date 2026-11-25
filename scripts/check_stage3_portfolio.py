"""Verify Step 4 empty/nonempty consistency and independent full repeat."""
import json

import _bootstrap  # noqa: F401
from core.config.paths import PROJECT_ROOT
from core.strategy.contract import file_sha256, load_stage3_contract
from core.strategy.inputs import SnapshotInputs
from core.strategy.portfolio import TABLE_KEYS, default_step4_run_id
from core.strategy.portfolio_build import Step4Builder, publish_step4
from core.strategy.verification import repeat_metadata, resolve_repeat, table_repeat_mismatches
from core.quality.verification import verification_mode


def main():
    mode = verification_mode()
    config = load_stage3_contract()["config"]
    root = PROJECT_ROOT / config["delivery"]["root"]
    run_id = default_step4_run_id()
    run_root = root / "runs" / run_id
    manifest = json.loads((run_root / "manifest.json").read_text(encoding="utf-8"))
    repeat_root = root / "verification" / (run_id + "_repeat")
    with SnapshotInputs(root / "inputs" / config["delivery"]["active_input_bundle"], verification=mode) as inputs:
        repeat, repeat_status = resolve_repeat(repeat_root, lambda: publish_step4(
            Step4Builder(inputs, root / "runs" / config["delivery"]["active_step3_run"], config),
            repeat_root, repeat_root.name), mode)
        db = inputs.db
        daily = f"read_parquet('{run_root}/l4/portfolio_daily/part-00000.parquet')"
        target_rows = manifest["tables"]["target_portfolio"]["rows"]
        order_rows = manifest["tables"]["order_intent"]["rows"]
        observations = {
            "daily_rows": manifest["tables"]["portfolio_daily"]["rows"],
            "target_rows": target_rows, "order_rows": order_rows,
            "solved_days": db.execute(f"SELECT count(*) FROM {daily} WHERE status='solved'").fetchone()[0],
            "infeasible_days": db.execute(f"SELECT count(*) FROM {daily} WHERE status='infeasible_keep_positions'").fetchone()[0],
            "max_cycle_days": db.execute(f"SELECT count(*) FROM {daily} WHERE solver_cycles=?", [config["portfolio"]["solver_max_cycles"]]).fetchone()[0],
            "inconsistent_zero_state_days": db.execute(f"""SELECT count(*) FROM {daily}
                WHERE stock_target_weight=0 AND (target_position_count<>0 OR order_count<>0)""").fetchone()[0] if target_rows == 0 else 0,
            "repeat_hash_mismatches": table_repeat_mismatches(manifest, repeat, TABLE_KEYS),
            "research_labels_read": "l3/forward_return" in manifest["input_read_log"],
        }
    failures = []
    if observations["inconsistent_zero_state_days"] or observations["repeat_hash_mismatches"] or observations["research_labels_read"]:
        failures.append(observations)
    if target_rows == 0 and order_rows != 0:
        failures.append({"check": "orders_without_targets"})
    result = {"passed": not failures, "step": 4, "run_id": run_id, "failures": failures,
              "observations": observations, "economic_promoted": False,
              "can_enter_step5": target_rows > 0 and order_rows > 0 and observations["infeasible_days"] == 0,
              "live_ready": False, **repeat_metadata(mode, repeat_status)}
    target = run_root / "verification/step4_checks.json"
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    manifest["verification"] = {"passed": result["passed"], "path": "verification/step4_checks.json",
                                "sha256": file_sha256(target)}
    (run_root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
